"""Ingestion helpers: turn an upload or a published Google Sheet URL into a
local snapshot the pipeline can read.

Dependency-free by design — stdlib ``urllib`` only, plus the pandas/openpyxl the
pipeline already requires. A published Google Sheet (File → Share → Publish to
web, or a link-shared sheet) exports as CSV with no Google account, API key, or
OAuth. Every fetch is written to a dated snapshot under ``data/ingest`` so each
run pins exactly the bytes it analyzed (reproducible + auditable).

The pipeline reads local ``csv`` / ``excel_sheet`` sources already; this module
just resolves a Sheet URL down to one of those, so nothing downstream changes.
"""

from __future__ import annotations

import datetime
import html
import os
import re
import urllib.parse
import urllib.request
from typing import Optional


DEFAULT_SNAPSHOT_DIR = os.path.join("data", "ingest")
_GSHEET_HOST = "docs.google.com"
_SPREADSHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9\-_]+)")
_GID_RE = re.compile(r"gid=(\d+)")
_FETCH_TIMEOUT = 30  # seconds


def normalize_gsheet_url(url: str) -> str:
    """Return a CSV-export URL for a Google Sheets link.

    Handles the three shapes a user pastes:

    - **Edit / view** (``/spreadsheets/d/<id>/edit#gid=N``) -> rewritten to
      ``/export?format=csv&gid=N`` (the single-tab CSV export).
    - **Publish-to-web** (``/spreadsheets/d/e/<token>/pubhtml?gid=N&single=true``,
      the HTML embed) -> rewritten to ``.../pub?output=csv&gid=N&single=true``.
      An already-CSV ``/pub?output=csv`` link is left as-is.
    - **Non-Google http(s)** URL -> passed through (a raw CSV endpoint works too).

    ``&amp;`` entities from a copied "embed" URL are decoded first. Raises
    ValueError for a non-http(s) scheme (blocks ``file://`` and friends).
    """
    raw = html.unescape(url.strip())
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Sheet URL must be http(s): {url!r}")
    if _GSHEET_HOST not in parsed.netloc:
        return raw  # some other http(s) CSV endpoint — trust the caller

    query = urllib.parse.parse_qs(parsed.query)
    gid: Optional[str] = None
    frag = _GID_RE.search(parsed.fragment or "")
    if frag:
        gid = frag.group(1)
    elif "gid" in query:
        gid = query["gid"][0]

    # Publish-to-web link (…/pub or …/pubhtml). Force the CSV variant.
    if "/pub" in parsed.path:
        if query.get("output") == ["csv"]:
            return raw
        path = parsed.path.replace("/pubhtml", "/pub")
        params = [("output", "csv")]
        if gid is not None:
            params.append(("gid", gid))
        if "single" in query:
            params.append(("single", "true"))
        new_query = urllib.parse.urlencode(params)
        return urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, path, "", new_query, "")
        )

    if query.get("format") == ["csv"]:
        return raw  # already an /export?format=csv link

    match = _SPREADSHEET_ID_RE.search(parsed.path)
    if not match:
        raise ValueError(f"Could not find a spreadsheet id in URL: {url!r}")
    sheet_id = match.group(1)

    export = f"https://{_GSHEET_HOST}/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid is not None:
        export += f"&gid={gid}"
    return export


def snapshot_path(
    name: str,
    snapshot_dir: str = DEFAULT_SNAPSHOT_DIR,
    ext: str = ".csv",
    now: Optional[datetime.datetime] = None,
) -> str:
    """Build a dated snapshot path ``<dir>/<YYYYmmdd_HHMMSS>_<safe_name><ext>``.

    Creates the directory if missing. ``now`` is injectable for deterministic
    tests.
    """
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"\W+", "_", str(name)).strip("_") or "sheet"
    os.makedirs(snapshot_dir, exist_ok=True)
    return os.path.join(snapshot_dir, f"{stamp}_{safe}{ext}")


def fetch_csv(url: str, dest_path: str, timeout: int = _FETCH_TIMEOUT) -> str:
    """GET ``url`` and write the raw bytes to ``dest_path``. Returns the path."""
    request = urllib.request.Request(url, headers={"User-Agent": "fv-analysis/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        data = response.read()
    with open(dest_path, "wb") as handle:
        handle.write(data)
    return dest_path


def ingest_gsheet(
    url: str,
    name: str,
    snapshot_dir: str = DEFAULT_SNAPSHOT_DIR,
    now: Optional[datetime.datetime] = None,
    timeout: int = _FETCH_TIMEOUT,
) -> str:
    """Fetch a published Sheet to a dated CSV snapshot; return the snapshot path.

    The URL is normalized to its CSV-export form first, so an ``/edit`` link
    works as well as a ``/pub?output=csv`` link.
    """
    csv_url = normalize_gsheet_url(url)
    dest = snapshot_path(name, snapshot_dir, ".csv", now)
    return fetch_csv(csv_url, dest, timeout=timeout)


def snapshot_local(
    src_path: str,
    snapshot_dir: str = DEFAULT_SNAPSHOT_DIR,
    now: Optional[datetime.datetime] = None,
) -> str:
    """Copy an uploaded local file to a dated snapshot; return the new path.

    Preserves the original extension so an ``.xlsx`` upload stays an Excel
    workbook. Keeps an audit copy of exactly what each run ingested.
    """
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"Upload not found: {src_path!r}")
    _, ext = os.path.splitext(src_path)
    name = os.path.splitext(os.path.basename(src_path))[0]
    dest = snapshot_path(name, snapshot_dir, ext or ".csv", now)
    with open(src_path, "rb") as source, open(dest, "wb") as handle:
        handle.write(source.read())
    return dest
