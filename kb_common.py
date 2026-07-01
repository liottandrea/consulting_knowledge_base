"""Shared utilities for the KB pipeline (paths, hashing, JSONL logging).

Kept deliberately small. Every stage (ingest / update / embed) imports the
same JSONL logger and path constants from here so logging stays consistent and
there is a single source of truth for where things live on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Canonical project paths -------------------------------------------------
ROOT = Path(__file__).resolve().parent
# DEPRECATED: single-directory source root. Source directories are now driven by
# sources.json via load_source_dirs(); this alias remains only for the fallback
# default and any code that has not yet migrated. Do not add new references.
SOURCES_DIR = ROOT / "sources"
# Canonical list of source directories (gitignored — paths are machine-specific).
SOURCES_CONFIG = ROOT / "sources.json"
PROCESSED_DIR = ROOT / "processed"
LOGS_DIR = ROOT / "logs"
# Manifest location is overridable (KB_MANIFEST) so the container can keep it on
# a mounted volume — avoids bind-mounting a single file. Defaults to repo root.
MANIFEST_PATH = Path(os.environ.get("KB_MANIFEST", ROOT / "manifest.json"))
PIPELINE_LOG = LOGS_DIR / "pipeline.jsonl"


def utc_now_iso() -> str:
    """Timezone-aware UTC timestamp, e.g. '2026-06-26T13:00:00.123456+00:00'."""
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, _chunk: int = 1 << 20) -> str:
    """SHA256 over file *content* (not mtime) so re-saves with no edits are no-ops."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


def log_event(event: dict[str, Any]) -> None:
    """Append one JSON object as a line to logs/pipeline.jsonl.

    A 'ts' field is added automatically if absent. Used by all stages.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    event.setdefault("ts", utc_now_iso())
    with open(PIPELINE_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


# --- Source directory configuration -----------------------------------------
def _fallback_source_dirs() -> list[dict[str, Any]]:
    """Single local source root, used when sources.json is absent.

    Keeps the pipeline working on a fresh clone with no config — points at
    ROOT/sources, non-recursive, labelled "Local".
    """
    return [{"name": "Local", "path": str(SOURCES_DIR), "recurse": False}]


def load_source_dirs() -> list[dict[str, Any]]:
    """Return the configured source roots from sources.json.

    Each entry is a dict with:
      * ``name``    — human-readable label (used in frontmatter / citations).
      * ``path``    — absolute path. Relative paths in the config are resolved
                      against ROOT so the same config works from any CWD.
      * ``recurse`` — walk subdirectories recursively when True (default False).

    Falls back to a single ROOT/sources entry if the file is missing. A malformed
    file logs a warning and also falls back, rather than crashing the pipeline.
    """
    if not SOURCES_CONFIG.exists():
        return _fallback_source_dirs()
    try:
        raw = json.loads(SOURCES_CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log_event({"stage": "config", "event": "warning",
                   "warning": f"could not read {SOURCES_CONFIG.name}: {exc!r}; "
                              "using fallback source dir"})
        return _fallback_source_dirs()
    if not isinstance(raw, list) or not raw:
        log_event({"stage": "config", "event": "warning",
                   "warning": f"{SOURCES_CONFIG.name} is empty or not a list; "
                              "using fallback source dir"})
        return _fallback_source_dirs()

    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("path") or not entry.get("name"):
            log_event({"stage": "config", "event": "warning",
                       "warning": f"skipping malformed sources.json entry: {entry!r}"})
            continue
        path = Path(entry["path"])
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        out.append({"name": entry["name"], "path": str(path),
                    "recurse": bool(entry.get("recurse", False))})
    return out or _fallback_source_dirs()
