"""Source-document de-duplication: detect copy/(1) artifacts and superseded
draft versions among ingested sources, and clean them up on approval.

    uv run python source_dedup.py                                        # dry-run report
    uv run python source_dedup.py --propose                              # write logs/source_dedup_proposal.json
    uv run python source_dedup.py --apply logs/source_dedup_proposal.json # destructive cleanup

Source files themselves (OneDrive/NAS/local) are never touched — only the
matching wiki/sources page and processed/*.md artifact are removed, plus a
"deleted" manifest event is appended. After --apply, add the printed exclude
globs to sources.json (see sources.example.json) so the removed files aren't
re-ingested on the next backfill.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import update_kb
import wiki_ingest
import wiki_lint
import wiki_ontology
from kb_common import LOGS_DIR, PROCESSED_DIR, log_event, utc_now_iso

WIKI_DIR = wiki_lint.WIKI_DIR
PROPOSAL_PATH = LOGS_DIR / "source_dedup_proposal.json"

# Trailing "copy"/"(1)"/"(1)(1)"/"_1"/"-1" artifact markers.
_ARTIFACT_TAIL_RE = re.compile(r"(\bcopy\b|\(\d+\)|[_-]1)\s*$", re.IGNORECASE)
_ARTIFACT_STRIP_RE = re.compile(r"(\s*-?\s*copy\b|\s*\(\d+\)|\s*[_-]1)\s*$", re.IGNORECASE)
_VERSION_RE = re.compile(r"^(.*?)[\s_-]v(\d+(?:\.\d+)?)$", re.IGNORECASE)
_DISTINCT_RE = re.compile(r"\bweek\s*\d+\b|\bissue\s*\d+\b|\d{4}-\d{2}-\d{2}|\b\d{8}\b", re.IGNORECASE)


def _normalize_base(stem: str) -> str:
    """Strip artifact and version suffixes so near-duplicate filenames collapse
    to one group key (e.g. 'Report copy (1)' and 'Report v3' -> 'report')."""
    name = stem.lower().strip()
    while True:
        new = _ARTIFACT_STRIP_RE.sub("", name).strip()
        if new == name:
            break
        name = new
    m = _VERSION_RE.match(name)
    if m:
        name = m.group(1).strip()
    return name


def _version_of(stem: str) -> float | None:
    m = _VERSION_RE.match(stem.strip())
    if not m:
        return None
    try:
        return float(m.group(2))
    except ValueError:
        return None


def _load_alive_manifest() -> dict[str, dict]:
    events = update_kb.load_events()
    state = update_kb.latest_state(events)
    return {k: e for k, e in state.items() if e.get("event") != "deleted"}


def _group_key(entry: dict) -> tuple[str, str]:
    """Directory-scoped normalized base name — avoids grouping same-named
    files that live in unrelated client folders."""
    rel = entry.get("relative_path") or entry.get("path", "")
    p = Path(rel)
    return str(p.parent), _normalize_base(p.stem)


def detect() -> list[dict]:
    """Returns a list of {"keep": key, "remove": [key, ...], "reason": str}."""
    alive = _load_alive_manifest()
    groups: dict[tuple[str, str], list[str]] = {}
    for key, entry in alive.items():
        groups.setdefault(_group_key(entry), []).append(key)

    raw: list[tuple[str, str, str]] = []  # (keep, remove, reason)
    for (_parent, _base), keys in groups.items():
        if len(keys) < 2:
            continue
        infos = []
        for key in keys:
            entry = alive[key]
            stem = Path(entry.get("relative_path") or entry.get("path", "")).stem
            infos.append({
                "key": key, "stem": stem,
                "is_distinct": bool(_DISTINCT_RE.search(stem)),
                "is_artifact": bool(_ARTIFACT_TAIL_RE.search(stem)),
                "version": _version_of(stem),
            })

        # Artifact tier: copy/(N)/_1 siblings of a non-artifact original.
        plain = [i for i in infos if not i["is_artifact"]]
        artifacts = [i for i in infos if i["is_artifact"]]
        removed_keys: set[str] = set()
        if plain and artifacts:
            keep = min(plain, key=lambda i: len(i["stem"]))
            for a in artifacts:
                raw.append((keep["key"], a["key"], "artifact"))
                removed_keys.add(a["key"])

        # Version tier: among remaining, non-distinct, versioned members, keep
        # the highest version.
        remaining = [i for i in infos if i["key"] not in removed_keys and not i["is_distinct"]]
        versioned = [i for i in remaining if i["version"] is not None]
        if len(versioned) > 1:
            best = max(versioned, key=lambda i: i["version"])
            for v in versioned:
                if v["key"] != best["key"]:
                    raw.append((best["key"], v["key"], "version"))

    grouped: dict[tuple[str, str], list[str]] = {}
    for keep, remove, reason in raw:
        grouped.setdefault((keep, reason), []).append(remove)
    return [{"keep": k, "remove": sorted(v), "reason": r} for (k, r), v in grouped.items()]


def _print_report(proposals: list[dict]) -> None:
    if not proposals:
        print("No duplicate/superseded sources detected.")
        return
    print(f"\n{len(proposals)} removal group(s):")
    for item in proposals:
        print(f"  keep {item['keep']!r} ({item['reason']}), remove:")
        for r in item["remove"]:
            print(f"     • {r}")


# --- apply (review-gated, destructive) ----------------------------------------
def _find_source_page(display_name: str) -> Path | None:
    d = WIKI_DIR / "sources"
    if not display_name or not d.exists():
        return None
    needle = f"display_name: {display_name}"
    for p in d.glob("*.md"):
        if needle in p.read_text(encoding="utf-8"):
            return p
    return None


def apply_proposal(path: Path) -> int:
    if not path.exists():
        print(f"  ✗ proposal file not found: {path}", file=sys.stderr)
        return 1
    proposal = json.loads(path.read_text(encoding="utf-8"))
    alive = _load_alive_manifest()
    new_events = []
    exclude_hints: set[str] = set()

    for item in proposal:
        for remove_key in item.get("remove", []):
            entry = alive.get(remove_key)
            if entry is None:
                print(f"  - skip {remove_key}: not found in the alive manifest (already removed?)")
                continue

            page = _find_source_page(entry.get("display_name", ""))
            if page is not None:
                slug = page.stem
                for other in wiki_lint._pages():
                    if other == page:
                        continue
                    ofm, obody = wiki_ontology.read_page(other)
                    new_body = wiki_ontology.rewrite_link_target(obody, slug, None)
                    if new_body != obody:
                        wiki_ontology.write_page(other, ofm, new_body)
                page.unlink()

            processed_md = update_kb._processed_md_for(remove_key)
            if processed_md.exists():
                processed_md.unlink()

            new_events.append({
                "path": remove_key,
                "event": "deleted",
                "timestamp": utc_now_iso(),
                "reason": f"source_dedup: {item.get('reason')} of {item.get('keep')}",
            })
            stem = Path(entry.get("relative_path") or remove_key).stem
            m = re.search(r"(\bcopy\b|\(\d+\)|[_-]1$|[\s_-]v\d+(?:\.\d+)?$)", stem, re.IGNORECASE)
            if m:
                exclude_hints.add(f"*{m.group(1).strip()}*")
            print(f"  ✓ removed {remove_key} ({item.get('reason')}, keep {item.get('keep')})")

    if new_events:
        update_kb.append_events(new_events)
        note = (f"\n{utc_now_iso()} **source dedup**: removed {len(new_events)} "
               "superseded/duplicate source(s) via `source_dedup.py --apply` "
               f"({', '.join(sorted({e['path'] for e in new_events}))}).\n")
        with open(wiki_lint.WIKI_LOG_MD, "a", encoding="utf-8") as fh:
            fh.write(note)
        wiki_ingest._rebuild_index()

    log_event({"stage": "wiki", "event": "source_dedup_apply", "removed": len(new_events)})
    if exclude_hints:
        print("\nTo prevent re-ingestion, add to the affected sources.json entry's \"exclude\" "
             f"list: {sorted(exclude_hints)} (see sources.example.json).")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Detect and clean up duplicate/superseded source documents")
    ap.add_argument("--propose", action="store_true",
                    help=f"write the detected groups to {PROPOSAL_PATH} for review; changes nothing")
    ap.add_argument("--apply", metavar="PATH",
                    help="apply an already-reviewed proposal JSON (destructive)")
    args = ap.parse_args(argv)

    if args.apply:
        return apply_proposal(Path(args.apply))

    proposals = detect()
    if args.propose:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        PROPOSAL_PATH.write_text(json.dumps(proposals, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
        print(f"  ✓ wrote {len(proposals)} removal group(s) to {PROPOSAL_PATH}")
        print("  Review and edit the file, then run --apply to clean up. Nothing changed yet.")
        return 0

    _print_report(proposals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
