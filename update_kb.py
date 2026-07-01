"""Stage 2 — Manifest & incremental update.

The single command to run after dropping new files into sources/:

    uv run python update_kb.py

It walks sources/, diffs every file against the latest state derived from
manifest.json (by SHA256 of file *content*, never mtime), and ingests only the
files that are new or changed. Files that have disappeared from sources/ are
recorded as deletions. A clear summary is printed at the end.

manifest.json is an APPEND-ONLY event log: a flat, chronological JSON array of
events, one appended per change. Each event:

    {
      "path":          "<filename relative to sources/>",
      "event":         "ingested" | "updated" | "deleted",
      "hash":          "<sha256 of content>",
      "timestamp":     "<UTC ISO8601>",
      "chunks":        <int>,
      "doc_type":      "pptx" | "pdf" | "docx",
      "previous_hash": "<prior hash>"        # only on "updated"
    }

The current state of a file is its most recent event. Past events are never
modified or removed — history is the point.

Vector-store sync (upserting changed chunks, removing deleted ones from Chroma)
is Stage 3's job: embed.py consumes these same manifest events. update_kb.py
only ingests to processed/ and records events; it does not embed yet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ingest
import wiki_ingest
from kb_common import (
    MANIFEST_PATH,
    PROCESSED_DIR,
    log_event,
    sha256_file,
    utc_now_iso,
)


def _relative_path(path: Path, source_entry: dict) -> str:
    """Path relative to its source root (stable across machines / mount points)."""
    root = Path(source_entry["path"])
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _rel(path: Path, source_entry: dict) -> str:
    """Namespaced manifest key: "{source_name}/{relative_path}".

    Prefixing with the source name keeps keys unique when the same relative path
    exists under two different source roots (e.g. OneDrive vs local inbox), while
    staying stable across machines (never an absolute path).
    """
    return f"{source_entry['name']}/{_relative_path(path, source_entry)}"


def load_events() -> list[dict]:
    """Read the append-only event list. Returns [] if missing or unreadable."""
    if not MANIFEST_PATH.exists():
        return []
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def append_events(new_events: list[dict]) -> None:
    """Append events to manifest.json, preserving all prior history."""
    events = load_events()
    events.extend(new_events)
    MANIFEST_PATH.write_text(
        json.dumps(events, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def latest_state(events: list[dict]) -> dict[str, dict]:
    """Most recent event per path (later events overwrite earlier in list order)."""
    state: dict[str, dict] = {}
    for e in events:
        p = e.get("path")
        if p:
            state[p] = e
    return state


def _processed_md_for(rel: str) -> Path:
    """Where ingest.py writes the markdown for a given source (full name + .md)."""
    return PROCESSED_DIR / f"{Path(rel).name}.md"


def update() -> int:
    events = load_events()
    state = latest_state(events)
    # "Alive" = files whose most recent event is not a deletion.
    alive = {p: e for p, e in state.items() if e.get("event") != "deleted"}

    sources = ingest._iter_sources([])  # (path, source_entry) across all roots
    # Namespaced key -> (path, source_entry) for every discovered source file.
    src_by_rel = {_rel(p, e): (p, e) for p, e in sources}

    new_events: list[dict] = []
    counts = {"new": 0, "changed": 0, "unchanged": 0, "deleted": 0, "failed": 0}
    warnings_by_file: list[tuple[str, list[str]]] = []

    # --- New / changed / unchanged -----------------------------------------
    for rel, (path, entry) in sorted(src_by_rel.items()):
        content_hash = sha256_file(path)
        prev = state.get(rel)
        prev_alive = bool(prev) and prev.get("event") != "deleted"

        if prev_alive and prev.get("hash") == content_hash:
            counts["unchanged"] += 1
            continue

        try:
            result = ingest.ingest_file(path, entry)
        except Exception as exc:  # ingest already logged the traceback
            counts["failed"] += 1
            warnings_by_file.append((rel, [f"INGEST FAILED: {exc}"]))
            continue
        if result.skipped:
            continue

        # Human-readable location fields, mirrored from the frontmatter, so the
        # manifest audit trail is readable without decoding paths.
        loc_fields = {
            "location": entry["name"],
            "display_name": ingest._display_name(path.stem),
            "relative_path": _relative_path(path, entry),
        }

        if prev_alive:
            new_events.append({
                "path": rel,
                "event": "updated",
                "hash": result.content_hash,
                "timestamp": utc_now_iso(),
                "chunks": result.chunk_count,
                "doc_type": result.doc_type,
                "previous_hash": prev.get("hash"),
                **loc_fields,
            })
            counts["changed"] += 1
        else:
            new_events.append({
                "path": rel,
                "event": "ingested",
                "hash": result.content_hash,
                "timestamp": utc_now_iso(),
                "chunks": result.chunk_count,
                "doc_type": result.doc_type,
                **loc_fields,
            })
            counts["new"] += 1

        if result.warnings:
            warnings_by_file.append((rel, result.warnings))

    # --- Deletions: alive in the manifest but gone from all source roots ----
    for rel, prev in sorted(alive.items()):
        if rel in src_by_rel:
            continue
        new_events.append({
            "path": rel,
            "event": "deleted",
            "hash": prev.get("hash"),
            "timestamp": utc_now_iso(),
            "chunks": prev.get("chunks"),
            "doc_type": prev.get("doc_type"),
            # Carry the readable fields forward from the prior event.
            "location": prev.get("location"),
            "display_name": prev.get("display_name"),
            "relative_path": prev.get("relative_path"),
        })
        counts["deleted"] += 1
        # Drop the stale processed markdown so it can't be re-ingested.
        md = _processed_md_for(rel)
        if md.exists():
            md.unlink()

    if new_events:
        append_events(new_events)

    # --- Phase 2: wiki integration for new/changed files -------------------
    wiki_results = _wiki_integrate(new_events)

    log_event({
        "stage": "update",
        "event": "summary",
        **counts,
        "events_appended": len(new_events),
        "warning_files": len(warnings_by_file),
        "wiki": wiki_results,
    })

    _print_summary(counts, warnings_by_file, len(new_events), wiki_results)
    return 0


def _wiki_integrate(new_events: list[dict]) -> dict:
    """Phase 2: fold each new/changed source into the wiki via the local LLM.

    Manifest events are already written by the time this runs, so a wiki failure
    (most commonly: Ollama not running) never loses ingest progress — the user
    can re-run wiki_ingest.py per file. If Ollama is unreachable the whole phase
    is skipped with a prominent warning.
    """
    todo = [e for e in new_events if e.get("event") in ("ingested", "updated")]
    result = {"integrated": 0, "skipped": 0, "failed": 0}
    if not todo:
        return result

    for e in todo:
        md = _processed_md_for(e["path"])
        if not md.exists():
            result["failed"] += 1
            log_event({"stage": "update", "event": "wiki_skip",
                       "source": e.get("display_name"), "reason": f"missing {md.name}"})
            continue
        res = wiki_ingest.ingest_to_wiki(md, dry_run=False)
        if res.get("error"):
            # Ollama unreachable -> skip the rest of the phase (same failure for all).
            if "not reachable" in res["error"].lower():
                remaining = len(todo) - (result["integrated"] + result["failed"])
                result["skipped"] += remaining
                print("\n" + "!" * 60)
                print("  WIKI PHASE SKIPPED — Ollama not reachable.")
                print(f"  {res['error']}")
                print("  Manifest events are saved. After starting Ollama, run:")
                print("     uv run python wiki_ingest.py processed/<filename>.md")
                print("!" * 60)
                break
            result["failed"] += 1
            print(f"  ⚠ wiki integration failed for {e.get('display_name')}: {res['error']}")
        else:
            result["integrated"] += 1

    return result


def _print_summary(
    counts: dict[str, int],
    warnings_by_file: list[tuple[str, list[str]]],
    appended: int,
    wiki: dict | None = None,
) -> None:
    print("\n" + "=" * 60)
    print("KB UPDATE SUMMARY")
    print("=" * 60)
    print(f"  new       : {counts['new']}")
    print(f"  changed   : {counts['changed']}")
    print(f"  deleted   : {counts['deleted']}")
    print(f"  unchanged : {counts['unchanged']}")
    if counts["failed"]:
        print(f"  FAILED    : {counts['failed']}")

    if warnings_by_file:
        print("\n  ⚠  WARNINGS")
        for rel, warns in warnings_by_file:
            for w in warns:
                print(f"     • {rel}: {w}")

    print(f"\n  {appended} event(s) appended to {MANIFEST_PATH.name}")
    if wiki is not None:
        print(f"  wiki      : {wiki['integrated']} integrated, "
              f"{wiki['skipped']} skipped, {wiki['failed']} failed")
    print("=" * 60)


if __name__ == "__main__":
    raise SystemExit(update())
