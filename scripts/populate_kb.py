"""Resumable bulk backfill: ingest source docs and build the wiki, one file at a
time, committing after each.

`update_kb.py` runs ingest + wiki as one monolithic pass — fine for a handful of
new files, fragile for a 300-document backfill (an interruption loses progress
and half-finished files aren't retried). This driver processes files
individually: ingest → append manifest event → wiki-integrate → record done,
per file. It can be stopped and re-run at any time; completed files are skipped.

    # pilot: just two client folders
    uv run python scripts/populate_kb.py --filter "OneDrive Projects/acme-corp,OneDrive Projects/globex"
    # see what would run, change nothing
    uv run python scripts/populate_kb.py --filter "OneDrive Projects/globex" --dry-run
    # full backfill (resumable; run overnight)
    uv run python scripts/populate_kb.py --model qwen3.6:35b

It reuses the existing pipeline functions unchanged (ingest.ingest_file,
update_kb manifest helpers, wiki_ingest.ingest_to_wiki) — no pipeline changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# This script lives in <repo>/scripts; put the repo root on the path so the
# pipeline modules import whether run as `scripts/populate_kb.py` or from root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest
import update_kb
import wiki_ingest
from kb_common import LOGS_DIR, log_event, sha256_file, utc_now_iso

STATE_PATH = LOGS_DIR / "populate_state.json"


# --- done-set state (resumability) -------------------------------------------
def _load_done() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        return set(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("done", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_done(done: set[str]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"done": sorted(done)}, indent=0) + "\n",
                          encoding="utf-8")


# --- selection ---------------------------------------------------------------
def _select(filters: list[str], limit: int | None) -> list[tuple[Path, dict]]:
    """All configured source files, optionally narrowed by substring filter on
    the namespaced key, capped at `limit`."""
    files = ingest._iter_sources([])  # (path, source_entry), honours sources.json
    out: list[tuple[Path, dict]] = []
    for path, entry in files:
        key = update_kb._rel(path, entry)
        if filters and not any(f in key for f in filters):
            continue
        out.append((path, entry))
    out.sort(key=lambda pe: update_kb._rel(pe[0], pe[1]))
    return out[:limit] if limit else out


# --- per-file processing -----------------------------------------------------
def _process_one(path: Path, entry: dict, model: str, state: dict) -> dict:
    """Ingest (if changed) then wiki-integrate one file. Returns a result dict.

    Reuses the existing processed markdown when the content hash is unchanged, so
    a resumed run doesn't re-run Docling on files it already extracted.
    """
    key = update_kb._rel(path, entry)
    content_hash = sha256_file(path)
    prev = state["manifest"].get(key)
    prev_alive = bool(prev) and prev.get("event") != "deleted"
    md = update_kb._processed_md_for(key)

    # Ingest unless the processed markdown already matches this content hash.
    if not (prev_alive and prev.get("hash") == content_hash and md.exists()):
        result = ingest.ingest_file(path, entry)
        if result.skipped:
            return {"status": "skipped", "reason": result.reason}
        event = {
            "path": key,
            "event": "updated" if prev_alive else "ingested",
            "hash": result.content_hash,
            "timestamp": utc_now_iso(),
            "chunks": result.chunk_count,
            "doc_type": result.doc_type,
            "location": entry["name"],
            "display_name": ingest._display_name(path.stem),
            "relative_path": update_kb._relative_path(path, entry),
        }
        if prev_alive:
            event["previous_hash"] = prev.get("hash")
        update_kb.append_events([event])          # per-file manifest commit
        state["manifest"][key] = event

    # Wiki integration for this one file.
    res = wiki_ingest.ingest_to_wiki(md, dry_run=False, model=model)
    if res.get("error"):
        return {"status": "failed", "reason": res["error"]}
    return {"status": "ok", "pages_written": res["pages_written"],
            "pages_updated": res["pages_updated"], "rejections": res["rejections"]}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Resumable bulk KB backfill")
    ap.add_argument("--filter", default="",
                    help="comma-separated substrings; only keys containing one are processed")
    ap.add_argument("--limit", type=int, default=None, help="cap number of files this run")
    ap.add_argument("--model", default=wiki_ingest.DEFAULT_MODEL, help="Ollama model")
    ap.add_argument("--dry-run", action="store_true", help="list what would run; change nothing")
    args = ap.parse_args(argv)

    filters = [f.strip() for f in args.filter.split(",") if f.strip()]
    selected = _select(filters, args.limit)
    if not selected:
        print("No matching source files.")
        return 0

    done = _load_done()
    manifest = update_kb.latest_state(update_kb.load_events())
    # Files already done AND unchanged are skipped.
    todo = []
    for path, entry in selected:
        key = update_kb._rel(path, entry)
        prev = manifest.get(key)
        if key in done and prev and prev.get("hash") == sha256_file(path):
            continue
        todo.append((path, entry))

    print(f"Selected {len(selected)} file(s); {len(selected) - len(todo)} already done; "
          f"{len(todo)} to process. Model: {args.model}")
    if args.dry_run:
        for path, entry in todo:
            print(f"  would process: {update_kb._rel(path, entry)}")
        return 0

    state = {"manifest": manifest}
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    for i, (path, entry) in enumerate(todo, 1):
        key = update_kb._rel(path, entry)
        print(f"populate: [ {i}/{len(todo)} ] {key}", flush=True)
        try:
            res = _process_one(path, entry, args.model, state)
        except Exception as exc:  # never let one document abort the batch
            counts["failed"] += 1
            log_event({"stage": "populate", "event": "failed", "key": key, "reason": repr(exc)})
            print(f"    ✗ {exc}", flush=True)
            continue
        if res["status"] == "ok":
            counts["ok"] += 1
            done.add(key)
            _save_done(done)                       # commit progress after each file
            print(f"    ✓ {res['pages_written']} written, {res['pages_updated']} updated"
                  + (f", {res['rejections']} rejected" if res['rejections'] else ""), flush=True)
        elif res["status"] == "skipped":
            counts["skipped"] += 1
            print(f"    - skipped ({res['reason']})", flush=True)
        else:
            counts["failed"] += 1
            log_event({"stage": "populate", "event": "failed", "key": key, "reason": res["reason"]})
            print(f"    ✗ {res['reason']}", flush=True)
            # Ollama down → stop early; nothing more will succeed this run.
            if "not reachable" in res["reason"].lower():
                print("\nOllama unreachable — stopping. Start it and re-run to resume.")
                break

    log_event({"stage": "populate", "event": "summary", **counts,
               "selected": len(selected), "todo": len(todo)})
    print(f"\nDone: {counts['ok']} integrated, {counts['skipped']} skipped, "
          f"{counts['failed']} failed.")
    if counts["ok"]:
        print("Refresh search:  qmd update && qmd embed   (or just run the launcher)")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
