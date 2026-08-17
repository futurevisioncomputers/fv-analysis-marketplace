"""Run session: the disk state that makes the pipeline resumable and stageable.

The pipeline used to be one Python process that ran stages 1->8 and returned.
That shape cannot pause: there is nowhere to stop, show the operator what a
stage produced, and wait for a decision. Claude Code's shell is non-interactive,
so `input()` is not an option either.

A Session moves that state onto disk. Each stage is its own CLI invocation:
load the session, run one stage, persist the result, exit. The caller (a skill,
the orchestrator in `--auto` mode, or the web service) decides what happens
between stages. Three properties fall out:

  - **Resumable.** A crashed or abandoned run picks up from the last completed
    stage instead of recomputing from the CSV.
  - **Individually callable.** `/fv-eda` can run on its own; missing
    prerequisites are back-filled from the same state (see `missing_prereqs`).
  - **Auditable.** Every artifact a stage wrote is recorded with the stage that
    produced it, so a number in the report can be traced to a file on disk.

Layout::

    <session_dir>/
        session.json          accumulated state - the only thing that matters
        artifacts/
            canonical.parquet     masked frame from the Data Engineer
            cleaned.csv           operator-downloadable copy
            features.parquet      after Feature Engineering
            report.html           final report
        monitoring_registry.json  KPI hooks, persisted across runs

Only `session.json` is authoritative. Artifacts are reproducible from it plus
the source data, so deleting `artifacts/` costs time, never correctness.

PII note: `canonical.parquet` holds the MASKED frame (Agent 2 hashes names,
mobiles, email, DOB before it is written). `cleaned.csv` is the same masked
frame, which is why it is safe to hand to an operator. The raw upload is
snapshotted under `data/ingest/` — gitignored, and never copied in here.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional

JsonDict = Dict[str, Any]

SESSION_FILE = "session.json"
ARTIFACT_DIR = "artifacts"
REGISTRY_FILE = "monitoring_registry.json"

# Windows refuses a file that is mid-replace, and it refuses with
# PermissionError (ERROR_ACCESS_DENIED) rather than anything that reads like
# "try again". One process running stages front to back never notices. The web
# service does: it answers a status poll on an HTTP thread while a worker
# thread is saving, and one poll in a few hundred died with
#     PermissionError: ... runs\<id>\session\session.json
# which surfaced as a 500 on a run that was working perfectly. The costlier
# half of the same race is the write side: a reader holding the target open
# denies `os.replace` DELETE access, so the save fails and the stage that just
# finished is never recorded — work done, run still reporting `pending`.
#
# Two defences, because they cover different cases:
#
# 1. A process-wide lock. Reader and writer are threads of ONE process in the
#    web service, so serialising the file touch removes that race outright
#    rather than making it rarer. The file is small and the hold is sub-
#    millisecond, which is nothing beside a stage.
# 2. A bounded retry, for the case the lock cannot see: a second process (a
#    CLI run against a session the service is also watching). The write is
#    atomic, so the loser only has to wait for the swap to land. ~0.4s total,
#    then the error is raised — silently returning stale state would be worse
#    than a visible failure.
_IO_LOCK = threading.RLock()
_SWAP_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.25)

# Stage order. The key is what the CLI and the skills use; `label` is what the
# operator sees at a checkpoint. `requires` drives prerequisite back-fill.
#
# Stage numbers keep the historical 1..8 rail (with 2.5 and 4.5 inserted) so
# existing report/UI code that renders the rail does not have to change.
STAGES: List[JsonDict] = [
    {"key": "problem",    "n": "1",   "label": "Problem Definition", "requires": []},
    {"key": "schema",     "n": "2.5", "label": "Schema / Source Plan",
     "requires": ["problem"]},
    {"key": "clean",      "n": "2",   "label": "Data Engineer",
     "requires": ["problem", "schema"]},
    # Optional because it is approval-gated: it proposes derived columns and
    # builds nothing until an operator picks. A non-interactive `--auto` run
    # has nobody to ask, so it skips rather than stalling on a pause that will
    # never be answered. `--stage features` runs it deliberately.
    {"key": "features",   "n": "2.7", "label": "Feature Engineering",
     "requires": ["clean"], "optional": True},
    {"key": "eda",        "n": "3",   "label": "EDA",          "requires": ["clean"]},
    {"key": "analyst",    "n": "4",   "label": "Analyst",      "requires": ["clean", "eda"]},
    {"key": "predict",    "n": "4.5", "label": "Prediction",   "requires": ["clean"],
     "optional": True},
    {"key": "visualize",  "n": "5",   "label": "Visualization", "requires": ["analyst"]},
    {"key": "insights",   "n": "6",   "label": "Insights",      "requires": ["analyst"]},
    {"key": "recommend",  "n": "6.5", "label": "Recommendation", "requires": ["insights"]},
    {"key": "monitor",    "n": "7",   "label": "Monitoring",    "requires": ["clean"]},
    {"key": "report",     "n": "8",   "label": "Report Writer",
     "requires": ["analyst", "insights"]},
]

STAGE_BY_KEY: Dict[str, JsonDict] = {s["key"]: s for s in STAGES}
STAGE_KEYS: List[str] = [s["key"] for s in STAGES]

# Stages that are skipped unless explicitly asked for. Prediction is
# honesty-gated and slow, and most questions do not need a model.
OPTIONAL_STAGES = {s["key"] for s in STAGES if s.get("optional")}


class SessionError(RuntimeError):
    """Raised for unusable session state — bad key, corrupt file, no source."""


def _read_json(target: str) -> JsonDict:
    """Read a JSON file, retrying while another thread is replacing it.

    See `_SWAP_RETRY_DELAYS`. A JSONDecodeError is NOT retried — that is a
    genuinely corrupt file, and re-reading it just delays the report.
    """
    last: Optional[OSError] = None
    for delay in (0.0,) + _SWAP_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            with _IO_LOCK:
                with open(target, encoding="utf-8") as fh:
                    return json.load(fh)
        except PermissionError as exc:
            last = exc
    raise last  # type: ignore[misc]


def _replace(tmp: str, target: str) -> None:
    """`os.replace`, retrying while another process has the target open."""
    last: Optional[OSError] = None
    for delay in (0.0,) + _SWAP_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            with _IO_LOCK:
                os.replace(tmp, target)
            return
        except PermissionError as exc:
            last = exc
    raise last  # type: ignore[misc]


class Session:
    """Accumulated state for one analysis run, persisted to a directory."""

    def __init__(self, path: str, state: Optional[JsonDict] = None):
        self.path = os.path.abspath(path)
        self.state: JsonDict = state if state is not None else self._blank()

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def create(
        cls,
        path: Optional[str] = None,
        *,
        question: str = "",
        data_sources: Optional[List[Mapping[str, Any]]] = None,
        csv_path: str = "",
    ) -> "Session":
        """Start a fresh session. `path` defaults to a dated directory."""
        if not path:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join("fv_sessions", f"{stamp}_{uuid.uuid4().hex[:6]}")
        session = cls(path)
        session.state["question"] = question
        session.state["csv_path"] = csv_path
        session.state["data_sources"] = [dict(s) for s in (data_sources or [])]
        os.makedirs(session.artifact_dir, exist_ok=True)
        session.save()
        return session

    @classmethod
    def load(cls, path: str) -> "Session":
        """Reopen an existing session. Raises if the directory is not one."""
        target = os.path.join(os.path.abspath(path), SESSION_FILE)
        if not os.path.exists(target):
            raise SessionError(
                f"No session at {path!r} (expected {SESSION_FILE}). "
                "Start one with Session.create()."
            )
        try:
            state = _read_json(target)
        except json.JSONDecodeError as exc:
            raise SessionError(f"Corrupt session file {target}: {exc}") from exc
        return cls(path, state)

    @classmethod
    def open_or_create(cls, path: Optional[str], **kwargs) -> "Session":
        """Resume when the directory already holds a session, else start one."""
        if path and os.path.exists(os.path.join(path, SESSION_FILE)):
            return cls.load(path)
        return cls.create(path, **kwargs)

    @staticmethod
    def _blank() -> JsonDict:
        return {
            "version": 1,
            "created_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "updated_at": None,
            "question": "",
            "csv_path": "",
            "data_sources": [],
            # stage key -> {status, summary, finished_at, result}
            "stages": {},
            # artifact name -> {path, stage, written_at}
            "artifacts": {},
            # Operator decisions taken at checkpoints, kept for the audit trail.
            "decisions": [],
            "errors": [],
        }

    # ------------------------------------------------------------------ paths

    @property
    def artifact_dir(self) -> str:
        return os.path.join(self.path, ARTIFACT_DIR)

    @property
    def registry_path(self) -> str:
        """Monitoring hook registry. Lives beside the session so thresholds
        persist across runs on the same data."""
        return os.path.join(self.path, REGISTRY_FILE)

    def artifact_path(self, name: str) -> str:
        return os.path.join(self.artifact_dir, name)

    # ------------------------------------------------------------ persistence

    def save(self) -> None:
        """Write session.json atomically.

        A stage can take minutes; a half-written state file after an interrupt
        would strand the run with no way to resume. Write to a temp file in the
        same directory and replace, so the file is either the old state or the
        new one, never a truncated mix.
        """
        os.makedirs(self.path, exist_ok=True)
        self.state["updated_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
        target = os.path.join(self.path, SESSION_FILE)
        fd, tmp = tempfile.mkstemp(dir=self.path, prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, indent=2, default=str)
            _replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ----------------------------------------------------------------- stages

    def begin(self, key: str) -> None:
        """Mark a stage as running, before its agent is called.

        Written to disk immediately and on purpose: a watcher — the studio UI,
        a tail on the file — can only show which agent is working if the fact
        is durable before the work starts. A crash mid-stage also leaves
        `running` behind rather than silence, which is the truth.
        """
        if key not in STAGE_BY_KEY:
            raise SessionError(
                f"Unknown stage {key!r}. Known: {', '.join(STAGE_KEYS)}")
        entry = self.state["stages"].get(key) or {}
        entry.update({
            "status": "running",
            "started_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "finished_at": None,
            "duration_ms": None,
        })
        self.state["stages"][key] = entry
        self.save()

    def record(
        self,
        key: str,
        result: Any,
        *,
        status: str = "done",
        summary: str = "",
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Store a stage's output and mark it complete.

        `duration_ms` is computed from the `started_at` that `begin` wrote, so
        a stage recorded without one (a direct call, an older session) simply
        has no duration rather than a fabricated zero.
        """
        if key not in STAGE_BY_KEY:
            raise SessionError(
                f"Unknown stage {key!r}. Known: {', '.join(STAGE_KEYS)}")
        previous = self.state["stages"].get(key) or {}
        finished = dt.datetime.now()
        started_at = previous.get("started_at")
        duration_ms: Optional[int] = None
        if started_at:
            try:
                started = dt.datetime.fromisoformat(started_at)
                duration_ms = max(
                    0, int((finished - started).total_seconds() * 1000))
            except ValueError:
                duration_ms = None
        self.state["stages"][key] = {
            "status": status,
            "summary": summary,
            "started_at": started_at,
            # Millisecond precision on both ends, or the duration carries up to
            # a second of truncation error — which on stages that take a few
            # hundred ms is most of the measurement.
            "finished_at": finished.isoformat(timespec="milliseconds"),
            "duration_ms": duration_ms,
            "metrics": dict(metrics or {}),
            "result": result,
        }
        self.save()

    def result(self, key: str, default: Any = None) -> Any:
        """The stored output of a completed stage."""
        entry = self.state["stages"].get(key)
        if not entry or entry.get("status") not in ("done", "skipped"):
            return default
        return entry.get("result", default)

    def status(self, key: str) -> Optional[str]:
        entry = self.state["stages"].get(key)
        return entry.get("status") if entry else None

    def is_done(self, key: str) -> bool:
        return self.status(key) == "done"

    def missing_prereqs(self, key: str) -> List[str]:
        """Prerequisite stages not yet completed, in run order.

        This is what lets a mid-pipeline skill be called on a bare CSV:
        `/fv-eda` asks for its missing prerequisites and back-fills them before
        running, instead of failing or — worse — running on absent state.
        Transitive, so requesting `report` returns the whole unfinished chain.
        """
        if key not in STAGE_BY_KEY:
            raise SessionError(
                f"Unknown stage {key!r}. Known: {', '.join(STAGE_KEYS)}")
        needed: List[str] = []
        seen = set()

        def walk(stage_key: str) -> None:
            for req in STAGE_BY_KEY[stage_key]["requires"]:
                if req in seen:
                    continue
                seen.add(req)
                walk(req)
                if not self.is_done(req):
                    needed.append(req)

        walk(key)
        return needed

    def next_stage(self) -> Optional[str]:
        """The next non-optional stage that has not run. None when complete."""
        for key in STAGE_KEYS:
            if key in OPTIONAL_STAGES:
                continue
            if not self.state["stages"].get(key):
                return key
        return None

    def progress(self) -> List[JsonDict]:
        """One row per stage for rendering the checkpoint rail.

        Carries `requires` and timing as well as status, so a caller can draw
        the pipeline — its dependency edges, which agent is working, and how
        long each took — without reaching into the state file itself.
        """
        rows = []
        total = sum(
            (self.state["stages"].get(s["key"]) or {}).get("duration_ms") or 0
            for s in STAGES)
        for stage in STAGES:
            entry = self.state["stages"].get(stage["key"]) or {}
            duration = entry.get("duration_ms")
            rows.append({
                "n": stage["n"],
                "key": stage["key"],
                "label": stage["label"],
                "requires": list(stage["requires"]),
                "optional": stage["key"] in OPTIONAL_STAGES,
                "status": entry.get("status", "pending"),
                "summary": entry.get("summary", ""),
                "started_at": entry.get("started_at"),
                "finished_at": entry.get("finished_at"),
                "duration_ms": duration,
                # Share of the run this agent accounted for. None means "not
                # measured"; 0.0 means "measured, and it took no time" — a UI
                # has to be able to tell those apart, so a 0 ms stage keeps a
                # real share rather than falling through to None.
                "duration_share": (
                    (duration / total) if duration is not None and total > 0
                    else None),
                "metrics": entry.get("metrics") or {},
            })
        return rows

    # -------------------------------------------------------------- artifacts

    def add_artifact(self, name: str, path: str, stage: str) -> str:
        """Register a file a stage produced. Returns the recorded path."""
        self.state["artifacts"][name] = {
            "path": os.path.abspath(path),
            "stage": stage,
            "written_at": dt.datetime.now().isoformat(timespec="milliseconds"),
        }
        self.save()
        return path

    def get_artifact(self, name: str) -> Optional[str]:
        """Path to a registered artifact, or None if absent from disk.

        Checks existence rather than trusting the record: an operator may have
        moved or deleted the file between stages, and a stale path would fail
        deep inside pandas instead of here.
        """
        entry = self.state["artifacts"].get(name)
        if not entry:
            return None
        return entry["path"] if os.path.exists(entry["path"]) else None

    # -------------------------------------------------------------- decisions

    def add_decision(self, stage: str, choice: str, detail: str = "") -> None:
        """Record an operator choice made at a checkpoint.

        The report has to be able to say *why* a stage was skipped or a feature
        dropped. Without this the run is not reproducible: the same session
        replayed would take different branches with no record of the first.
        """
        self.state["decisions"].append({
            "stage": stage,
            "choice": choice,
            "detail": detail,
            "at": dt.datetime.now().isoformat(timespec="milliseconds"),
        })
        self.save()

    def add_error(self, message: str) -> None:
        self.state["errors"].append(message)
        self.save()

    # ------------------------------------------------------------------ utils

    def sources(self) -> List[JsonDict]:
        """Normalized source descriptors, falling back to the single CSV."""
        if self.state["data_sources"]:
            return self.state["data_sources"]
        csv_path = self.state.get("csv_path") or ""
        if not csv_path:
            return []
        return [{"name": "primary", "type": "csv", "path": csv_path,
                 "path_or_query": csv_path, "domain": "single"}]

    def reset_from(self, key: str) -> List[str]:
        """Discard `key` and everything downstream of it; return what was cleared.

        Needed when an operator changes an answer at a checkpoint — approving a
        different feature set, or adding a business question. Leaving the later
        stages in place would silently mix results computed against different
        inputs, which is worse than recomputing them.
        """
        if key not in STAGE_BY_KEY:
            raise SessionError(f"Unknown stage {key!r}")
        start = STAGE_KEYS.index(key)
        cleared = []
        for later in STAGE_KEYS[start:]:
            if self.state["stages"].pop(later, None) is not None:
                cleared.append(later)
        self.save()
        return cleared

    def delete_artifacts(self) -> None:
        """Drop the artifact directory. `session.json` alone can rebuild it."""
        if os.path.isdir(self.artifact_dir):
            shutil.rmtree(self.artifact_dir)
        os.makedirs(self.artifact_dir, exist_ok=True)
        self.state["artifacts"] = {}
        self.save()

    def __repr__(self) -> str:
        done = sum(1 for s in self.state["stages"].values()
                   if s.get("status") == "done")
        return (f"<Session {os.path.basename(self.path)} "
                f"{done}/{len(STAGES)} stages done>")
