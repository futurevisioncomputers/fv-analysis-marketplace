"""Shared Claude client for the analysis agents.

Stdlib only (urllib) — no `anthropic` SDK, matching the project's no-extra-deps
rule. This is the single place the reasoning agents (Problem Definition, EDA,
Insights, Recommendation) reach the LLM. Computational agents (Data Engineer,
Analyst, Visualization, Monitoring) deliberately do NOT use it — their numbers
stay deterministic so the LLM can never fabricate a metric.

Design contract for callers:
- LLM is an *augmentation*, never a dependency. Every caller wraps a call in
  `try: ... except LLMUnavailable: <deterministic fallback>`. With no key, a
  placeholder key, or any API/parse failure, the agent runs exactly as before.
- The LLM reasons over numbers the agent already computed; it does not invent
  them. Callers pass the real findings/metrics in the prompt and treat the reply
  as prose/labels layered on top of deterministic data.

PII boundary: callers must mask personal data before passing samples in. Helpers
`mask_text` and `sanitize_records` are provided; column *names* are safe to send
(they describe the schema, not a person).

Auth: ANTHROPIC_API_KEY from the environment, or a project-root .env.
Model: claude-opus-4-8 (latest Opus) — strongest at schema-aware reasoning.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-opus-4-8"
ANTHROPIC_VERSION = "2023-06-01"
REQUEST_TIMEOUT = 60  # seconds
MAX_SAMPLE_ROWS = 8

# Phone runs — bare 10-digit and formatted / international (+91, spaces,
# brackets, hyphens). Guarded by a
# digit count at substitution time so dates / pincodes / amounts are left alone.
_FORMATTED_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{8,14}\d")
_MIN_PHONE_DIGITS = 10
_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w\-]+\.[\w.\-]+")


def _redact_phone(match: "re.Match[str]") -> str:
    run = match.group(0)
    if sum(ch.isdigit() for ch in run) >= _MIN_PHONE_DIGITS:
        return "[mobile]"
    return run
_PII_COL_RE = re.compile(
    r"name|mobile|phone|email|guardian|father|address|dob|birth|aadhaar|aadhar",
    re.IGNORECASE,
)


class LLMUnavailable(RuntimeError):
    """Raised when no usable API key is configured or the API call/parse fails.

    Callers MUST catch this and fall back to deterministic behaviour."""


# ------------------------------------------------------------------ key loading

def _load_key() -> Optional[str]:
    """ANTHROPIC_API_KEY from the environment, else a project-root .env."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    if not os.path.exists(env_path):
        return None
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _is_placeholder_key(key: str) -> bool:
    """True for the scaffolded .env stub or an obviously non-real key."""
    k = key.strip().strip('"').strip("'")
    if not k:
        return True
    low = k.lower()
    if "replace" in low or "your" in low or "xxxx" in low or low == "sk-ant-":
        return True
    return not (k.startswith("sk-ant-") and len(k) > 24)


def available() -> bool:
    """Cheap check: is a real key configured? Lets an agent skip building a
    prompt when the LLM could not possibly answer (still safe to just call and
    catch LLMUnavailable, but this avoids the wasted work)."""
    key = _load_key()
    return bool(key) and not _is_placeholder_key(key)


# -------------------------------------------------------------------- PII masks

def mask_text(value: Any) -> str:
    """Scrub mobile-number and email patterns out of a free-text value."""
    s = "" if value is None else str(value)
    s = _FORMATTED_PHONE_RE.sub(_redact_phone, s)
    s = _EMAIL_RE.sub("[email]", s)
    return s


def sanitize_records(
    columns: Sequence[str], rows: Sequence[Sequence[Any]],
    max_rows: int = MAX_SAMPLE_ROWS,
) -> List[Dict[str, str]]:
    """Mask PII before sample rows leave the process. Columns whose *name* signals
    personal data have every value replaced with `[masked]`; other cells still get
    mobile/email patterns scrubbed."""
    pii_cols = {i for i, c in enumerate(columns) if _PII_COL_RE.search(str(c))}
    out: List[Dict[str, str]] = []
    for row in list(rows)[:max_rows]:
        record: Dict[str, str] = {}
        for i, col in enumerate(columns):
            raw = row[i] if i < len(row) else ""
            record[str(col)] = "[masked]" if i in pii_cols else mask_text(raw)
        out.append(record)
    return out


# ----------------------------------------------------------------- completions

def complete_text(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> str:
    """Single-turn completion -> raw assistant text. Raises LLMUnavailable on no
    key / API error / empty reply."""
    key = _load_key()
    if not key or _is_placeholder_key(key):
        raise LLMUnavailable(
            "Set a real ANTHROPIC_API_KEY in .env to enable LLM reasoning."
        )

    payload: Dict[str, Any] = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LLMUnavailable(f"Claude API error {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMUnavailable(f"Could not reach Claude API: {exc}") from exc

    text = _extract_text(data)
    if not text.strip():
        raise LLMUnavailable("Claude returned an empty reply.")
    return text


def complete_json(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> Any:
    """Completion that must yield JSON. Returns the first parsable JSON object or
    array from the reply (tolerates code fences / surrounding prose). Raises
    LLMUnavailable if nothing parsable comes back."""
    text = complete_text(prompt, system=system, max_tokens=max_tokens,
                         temperature=temperature)
    obj = _first_json_value(text)
    if obj is None:
        raise LLMUnavailable("Claude returned no parsable JSON.")
    return obj


def _extract_text(payload: Dict[str, Any]) -> str:
    parts = payload.get("content") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _first_json_value(text: str) -> Optional[Any]:
    """Find the first balanced {...} or [...] in text and json.loads it."""
    best: Optional[int] = None
    opener = "{"
    for ch in ("{", "["):
        idx = text.find(ch)
        if idx >= 0 and (best is None or idx < best):
            best = idx
            opener = ch
    if best is None:
        return None
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(best, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[best:i + 1])
                except json.JSONDecodeError:
                    return None
    return None
