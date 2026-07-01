"""Wiki health check — deterministic lint plus an optional LLM pass.

    uv run python wiki_lint.py                # deterministic report only
    uv run python wiki_lint.py --fix          # + apply safe automatic fixes
    uv run python wiki_lint.py --llm          # + LLM findings appended to log.md

Deterministic pass (fast, no LLM): orphan pages, broken [[wikilinks]], missing
frontmatter, raw filesystem-path leaks, and stale pages (updated_at older than a
later source ingest that mentions the page title, per wiki/log.md).

LLM pass (--llm): asks the local model to spot missing concept pages,
contradictory claims, and missing cross-references; findings go to wiki/log.md.

--fix applies only SAFE deterministic fixes: stub pages for broken links, and
placeholder frontmatter fields. Raw-path leaks are flagged, never auto-fixed
(a human must choose the right display_name).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from kb_common import ROOT, log_event, utc_now_iso

WIKI_DIR = ROOT / "wiki"
WIKI_LOG_MD = WIKI_DIR / "log.md"
WIKI_INDEX_MD = WIKI_DIR / "index.md"

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_RAW_PATH_RE = re.compile(r"(/Users/|/home/|[A-Za-z]:\\|OneDrive/\S)")
_REQUIRED_FM = ("type", "title", "updated_at", "sources")
_SKIP = {"index.md", "log.md"}


def _pages() -> list[Path]:
    """All wiki content pages (excludes index.md and log.md)."""
    return sorted(p for p in WIKI_DIR.rglob("*.md") if p.name not in _SKIP)


def _read_frontmatter(p: Path) -> tuple[dict, str]:
    text = p.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                return (yaml.safe_load(text[3:end]) or {}), text[end + 4:]
            except yaml.YAMLError:
                return {}, text
    return {}, text


def _slug(name: str) -> str:
    return name.strip().lower()


def lint() -> dict:
    pages = _pages()
    by_slug: dict[str, Path] = {}
    for p in pages:
        by_slug[_slug(p.stem)] = p
        by_slug[_slug(p.name)] = p

    inbound: dict[Path, int] = {p: 0 for p in pages}
    broken: list[tuple[str, str]] = []       # (page, target)
    missing_fm: list[tuple[str, list[str]]] = []
    path_leaks: list[str] = []
    orphans: list[str] = []
    stale: list[tuple[str, str]] = []

    log_text = WIKI_LOG_MD.read_text(encoding="utf-8") if WIKI_LOG_MD.exists() else ""

    for p in pages:
        fm, body = _read_frontmatter(p)
        rel = p.relative_to(WIKI_DIR).as_posix()

        # Missing frontmatter fields
        miss = [k for k in _REQUIRED_FM if not fm.get(k)]
        if miss:
            missing_fm.append((rel, miss))

        # Raw path leak (frontmatter + body)
        if _RAW_PATH_RE.search(p.read_text(encoding="utf-8")):
            path_leaks.append(rel)

        # Wikilinks -> inbound counts + broken links
        for m in _WIKILINK_RE.finditer(body):
            target = _slug(m.group(1))
            if target in by_slug:
                dest = by_slug[target]
                if dest != p:
                    inbound[dest] += 1
            else:
                broken.append((rel, m.group(1).strip()))

        # Stale: title appears in a log line dated after this page's updated_at
        title = str(fm.get("title") or p.stem)
        updated = str(fm.get("updated_at") or "")
        if updated and title:
            for line in log_text.splitlines():
                if title.lower() in line.lower():
                    dm = re.search(r"\d{4}-\d{2}-\d{2}", line)
                    if dm and dm.group(0) > updated[:10]:
                        stale.append((rel, f"log entry {dm.group(0)} > updated_at {updated[:10]}"))
                        break

    for p, n in inbound.items():
        if n == 0:
            orphans.append(p.relative_to(WIKI_DIR).as_posix())

    return {"pages": len(pages), "orphans": orphans, "broken": broken,
            "missing_fm": missing_fm, "path_leaks": path_leaks, "stale": stale,
            "by_slug": by_slug, "broken_targets": sorted({t for _, t in broken})}


def _print_report(r: dict) -> None:
    print("\n" + "=" * 60)
    print("WIKI LINT REPORT")
    print("=" * 60)
    print(f"  pages checked : {r['pages']}")
    print(f"  orphans       : {len(r['orphans'])}")
    for o in r["orphans"]:
        print(f"     • {o}")
    print(f"  broken links  : {len(r['broken'])}")
    for src, tgt in r["broken"]:
        print(f"     • {src} -> [[{tgt}]]")
    print(f"  missing fm    : {len(r['missing_fm'])}")
    for rel, miss in r["missing_fm"]:
        print(f"     • {rel}: {', '.join(miss)}")
    print(f"  raw-path leaks: {len(r['path_leaks'])}  (fix manually — use display_name)")
    for rel in r["path_leaks"]:
        print(f"     • {rel}")
    print(f"  stale pages   : {len(r['stale'])}")
    for rel, why in r["stale"]:
        print(f"     • {rel}: {why}")
    print("=" * 60)


def _fix(r: dict) -> list[str]:
    """Apply safe deterministic fixes; return a list of applied-fix descriptions."""
    applied: list[str] = []

    # Stub pages for broken link targets.
    for target in r["broken_targets"]:
        slug = target.strip().lower().replace(" ", "-")
        stub = WIKI_DIR / "concepts" / f"{slug}.md"
        if stub.exists():
            continue
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(
            f"---\ntype: concept\ntitle: {target}\ncreated_at: {utc_now_iso()}\n"
            f"updated_at: {utc_now_iso()}\nsources: []\ntags: [stub]\ndomain: strategy\n---\n\n"
            f"# {target}\n\n_Stub created by wiki_lint to resolve a broken [[wikilink]]. "
            "Fill in or merge into the correct page._\n",
            encoding="utf-8")
        applied.append(f"created stub {stub.relative_to(WIKI_DIR).as_posix()}")

    # Add missing frontmatter fields with placeholders.
    placeholders = {"type": "concept", "title": None, "updated_at": utc_now_iso(),
                    "sources": []}
    for rel, miss in r["missing_fm"]:
        p = WIKI_DIR / rel
        fm, body = _read_frontmatter(p)
        for k in miss:
            fm.setdefault(k, p.stem if k == "title" else placeholders.get(k))
        new = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip() + "\n---" + body
        p.write_text(new, encoding="utf-8")
        applied.append(f"added frontmatter {miss} to {rel}")

    return applied


def _llm_pass(r: dict) -> None:
    """Ask the local model for higher-order findings; append them to log.md."""
    import wiki_ingest  # reuse the Ollama caller + model default

    index_md = WIKI_INDEX_MD.read_text(encoding="utf-8") if WIKI_INDEX_MD.exists() else "(empty)"
    report = (f"orphans={r['orphans']}\nbroken={r['broken']}\n"
              f"missing_fm={[m[0] for m in r['missing_fm']]}\nstale={r['stale']}")
    messages = [
        {"role": "system", "content": (ROOT / "CLAUDE.md").read_text(encoding="utf-8")},
        {"role": "user", "content":
            "Here is the deterministic lint report and the wiki index. Identify: "
            "(1) concepts mentioned across multiple pages but lacking their own "
            "concept page; (2) contradictory claims across pages; (3) missing "
            "cross-references. Respond as a concise bulleted list of findings.\n\n"
            f"===== LINT REPORT =====\n{report}\n\n===== WIKI INDEX =====\n{index_md}"},
    ]
    try:
        out = wiki_ingest._call_ollama(messages, wiki_ingest.DEFAULT_MODEL)
    except wiki_ingest.OllamaUnreachable as exc:
        print(f"  ✗ LLM pass skipped: {exc}", file=sys.stderr)
        return
    with open(WIKI_LOG_MD, "a", encoding="utf-8") as fh:
        fh.write(f"\n## Lint (LLM) {utc_now_iso()}\n\n{out.strip()}\n")
    print("  ✓ LLM findings appended to wiki/log.md")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Health-check the wiki")
    ap.add_argument("--fix", action="store_true", help="apply safe deterministic fixes")
    ap.add_argument("--llm", action="store_true", help="run the LLM findings pass")
    args = ap.parse_args(argv)

    r = lint()
    _print_report(r)

    applied: list[str] = []
    if args.fix:
        applied = _fix(r)
        print(f"\n  applied {len(applied)} fix(es):")
        for a in applied:
            print(f"     • {a}")

    if args.llm:
        _llm_pass(r)

    log_event({"stage": "wiki", "event": "lint", "pages": r["pages"],
               "orphans": len(r["orphans"]), "broken": len(r["broken"]),
               "missing_fm": len(r["missing_fm"]), "path_leaks": len(r["path_leaks"]),
               "stale": len(r["stale"]), "fixes_applied": len(applied)})
    # Non-zero exit if unresolved structural issues remain (path leaks always manual).
    unresolved = r["path_leaks"] or (r["broken"] and not args.fix)
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
