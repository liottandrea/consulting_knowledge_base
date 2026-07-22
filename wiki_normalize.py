"""One-off wiki cleanup: deterministic vocab/link normalization, plus an
LLM-assisted duplicate-entity merge proposal.

Deterministic pass (safe — coercion + link cleanup only, no merges/deletes of
distinct content):

    uv run python wiki_normalize.py                  # dry-run report
    uv run python wiki_normalize.py --apply           # apply the fixes

LLM merge proposal (review-gated: nothing is merged or deleted until a human
reviews/edits logs/normalize_proposal.json and re-runs with --apply-proposal):

    uv run python wiki_normalize.py --propose-merges
    uv run python wiki_normalize.py --apply-proposal logs/normalize_proposal.json

Every applied change (vocab fix, stub deletion, merge) is logged to
wiki/log.md; merges get a dated note per CLAUDE.md's contradiction-handling
convention.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

import wiki_ingest
import wiki_lint
import wiki_ontology
from kb_common import LOGS_DIR, log_event, utc_now_iso

WIKI_DIR = wiki_lint.WIKI_DIR
PROPOSAL_PATH = LOGS_DIR / "normalize_proposal.json"

_STUB_MARKER = "_Stub created by wiki_lint to resolve a broken [[wikilink]]."


def _find_page(slug: str) -> Path | None:
    for p in wiki_lint._pages():
        if p.stem.lower() == slug.strip().lower():
            return p
    return None


def _is_stub(fm: dict, body: str) -> bool:
    return fm.get("tags") == ["stub"] or _STUB_MARKER in body


# --- deterministic pass -------------------------------------------------------
def deterministic_pass(apply: bool) -> dict:
    """Coerce vocab/titles, delete stub + source-duplicate concept pages, and
    resolve dangling links wiki-wide. Returns a change report; only writes to
    disk when apply=True."""
    changes = {"vocab_fixed": [], "stubs_deleted": [],
              "source_dupe_concepts_deleted": [], "links_rewritten": 0}

    # 1. Vocab / title coercion (shared logic with wiki_lint --fix).
    for p in wiki_lint._pages():
        folder = p.relative_to(WIKI_DIR).parts[0]
        fm, body = wiki_ontology.read_page(p)
        if not fm:
            continue
        new_fm, _notes = wiki_ontology.normalize_page_frontmatter(fm, p.stem, folder)
        if new_fm != fm:
            changes["vocab_fixed"].append(p.relative_to(WIKI_DIR).as_posix())
            if apply:
                wiki_ontology.write_page(p, new_fm, body)

    # 2. Delete stub pages, rewriting inbound links to plain text first.
    stub_slugs = []
    for p in wiki_lint._pages():
        fm, body = wiki_ontology.read_page(p)
        if _is_stub(fm, body):
            stub_slugs.append(p.stem)
    for slug in stub_slugs:
        p = _find_page(slug)
        if p is None:
            continue
        changes["stubs_deleted"].append(p.relative_to(WIKI_DIR).as_posix())
        if apply:
            for other in wiki_lint._pages():
                if other == p:
                    continue
                ofm, obody = wiki_ontology.read_page(other)
                new_body = wiki_ontology.rewrite_link_target(obody, slug, None)
                if new_body != obody:
                    wiki_ontology.write_page(other, ofm, new_body)
            p.unlink()

    # 3. Concept pages that duplicate a source page's title -> delete, repoint.
    sources_dir = WIKI_DIR / "sources"
    concepts_dir = WIKI_DIR / "concepts"
    source_titles: dict[str, str] = {}
    if sources_dir.exists():
        for sp in sources_dir.glob("*.md"):
            sfm, _ = wiki_ontology.read_page(sp)
            title = str(sfm.get("title") or sp.stem).strip().lower()
            source_titles[title] = sp.stem
    if concepts_dir.exists():
        for cp in list(concepts_dir.glob("*.md")):
            cfm, _ = wiki_ontology.read_page(cp)
            title = str(cfm.get("title") or cp.stem).strip().lower()
            target_slug = source_titles.get(title)
            if not target_slug:
                continue
            changes["source_dupe_concepts_deleted"].append(cp.relative_to(WIKI_DIR).as_posix())
            if apply:
                for other in wiki_lint._pages():
                    if other == cp:
                        continue
                    ofm, obody = wiki_ontology.read_page(other)
                    new_body = wiki_ontology.rewrite_link_target(obody, cp.stem, target_slug)
                    if new_body != obody:
                        wiki_ontology.write_page(other, ofm, new_body)
                cp.unlink()

    # 4. Wiki-wide link/relation resolution sweep against the now-current page set.
    known = {p.stem.lower() for p in wiki_lint._pages()}
    for p in wiki_lint._pages():
        fm, body = wiki_ontology.read_page(p)
        new_body = wiki_ontology.rewrite_unresolved_links(body, known)
        new_relations = fm.get("relations")
        if "relations" in fm:
            new_relations = wiki_ontology.filter_relations(fm.get("relations"), known)
        rel_changed = "relations" in fm and new_relations != fm.get("relations")
        if new_body != body or rel_changed:
            changes["links_rewritten"] += 1
            if apply:
                if rel_changed:
                    fm["relations"] = new_relations
                wiki_ontology.write_page(p, fm, new_body)

    if apply:
        summary = (
            f"{utc_now_iso()} wiki_normalize deterministic pass: "
            f"{len(changes['vocab_fixed'])} vocab/title-fixed, "
            f"{len(changes['stubs_deleted'])} stub(s) deleted, "
            f"{len(changes['source_dupe_concepts_deleted'])} source-duplicate concept(s) "
            f"deleted, {changes['links_rewritten']} page(s) with links resolved.\n"
        )
        with open(wiki_lint.WIKI_LOG_MD, "a", encoding="utf-8") as fh:
            fh.write(summary)
        wiki_ingest._rebuild_index()

    return changes


def _print_changes(changes: dict, applied: bool) -> None:
    verb = "Applied" if applied else "Would apply (dry-run; pass --apply to write)"
    print(f"\n{verb}:")
    print(f"  vocab/title fixed         : {len(changes['vocab_fixed'])}")
    for rel in changes["vocab_fixed"]:
        print(f"     • {rel}")
    print(f"  stub pages deleted        : {len(changes['stubs_deleted'])}")
    for rel in changes["stubs_deleted"]:
        print(f"     • {rel}")
    print(f"  source-dupe concepts del  : {len(changes['source_dupe_concepts_deleted'])}")
    for rel in changes["source_dupe_concepts_deleted"]:
        print(f"     • {rel}")
    print(f"  pages with links resolved : {changes['links_rewritten']}")


# --- LLM-assisted merge proposal (review-gated) -------------------------------
def _similar(a: str, b: str) -> bool:
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.6:
        return True
    wa, wb = a.split(), b.split()
    return bool(wa and wb and wa[0] == wb[0] and len(wa[0]) > 3)


def _candidate_clusters() -> list[list[str]]:
    """Group entity/concept pages whose title or aliases look like the same
    real-world thing. Heuristic only — the LLM proposal step decides for real."""
    items: list[tuple[str, str, list[str]]] = []
    for folder in ("entities", "concepts"):
        d = WIKI_DIR / folder
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            fm, _ = wiki_ontology.read_page(p)
            title = str(fm.get("title") or p.stem)
            aliases = [str(a) for a in (fm.get("aliases") or [])]
            items.append((p.stem, title.strip().lower(), [a.strip().lower() for a in aliases]))

    clusters: list[list[str]] = []
    used: set[str] = set()
    for i, (slug_a, title_a, aliases_a) in enumerate(items):
        if slug_a in used:
            continue
        names_a = {title_a, *aliases_a}
        group = [slug_a]
        for slug_b, title_b, aliases_b in items[i + 1:]:
            if slug_b in used:
                continue
            names_b = {title_b, *aliases_b}
            if any(_similar(na, nb) for na in names_a for nb in names_b):
                group.append(slug_b)
                used.add(slug_b)
        if len(group) > 1:
            used.add(slug_a)
            clusters.append(group)
    return clusters


def _propose_merges(model: str) -> int:
    clusters = _candidate_clusters()
    if not clusters:
        print("No candidate duplicate clusters found.")
        return 0

    digest_parts = []
    for group in clusters:
        digest_parts.append("CLUSTER:")
        for slug in group:
            p = _find_page(slug)
            if p is None:
                continue
            fm, body = wiki_ontology.read_page(p)
            digest_parts.append(
                f"  [[{slug}]] title={fm.get('title')!r} entity_type={fm.get('entity_type')} "
                f"domain={fm.get('domain')} aliases={fm.get('aliases')}\n"
                f"    {body.strip()[:400]}"
            )
    digest = "\n".join(digest_parts)

    messages = [
        {"role": "system", "content": (wiki_ingest.ROOT / "CLAUDE.md").read_text(encoding="utf-8")},
        {"role": "user", "content": (
            "These candidate clusters were grouped by a title-similarity heuristic and MAY "
            "be duplicate pages describing the same real-world entity/concept, or MAY be "
            "genuinely distinct things that merely share a name prefix — decide per cluster. "
            "For each cluster that IS true duplicates, propose a canonical slug (usually the "
            "most complete/general page), the union of aliases (include the losing pages' "
            "titles), a single controlled category, and any relations. Respond with ONE JSON "
            "object and nothing else:\n"
            '{"clusters": [{"canonical": "<slug>", "members": ["<slug>", ...], '
            '"aliases": ["<name>", ...], "category": "<category>", '
            '"relations": [{"<relation>": "[[slug]]"}]}]}\n'
            "Omit any cluster you judge NOT to be true duplicates.\n\n"
            f"===== CANDIDATE CLUSTERS =====\n{digest}"
        )},
    ]
    raw = wiki_ingest._call_ollama(messages, model)
    try:
        proposal = wiki_ingest._extract_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"  ✗ model did not return valid JSON: {exc}", file=sys.stderr)
        return 1

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    PROPOSAL_PATH.write_text(json.dumps(proposal, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print(f"  ✓ wrote {len(proposal.get('clusters', []))} candidate merge(s) to {PROPOSAL_PATH}")
    print("  Review and edit the file, then run --apply-proposal to merge. Nothing changed yet.")
    return 0


def _apply_proposal(path: Path, model: str) -> int:
    if not path.exists():
        print(f"  ✗ proposal file not found: {path}", file=sys.stderr)
        return 1
    proposal = json.loads(path.read_text(encoding="utf-8"))
    clusters = proposal.get("clusters") or []
    if not clusters:
        print("No clusters in proposal.")
        return 0

    for cluster in clusters:
        canonical = cluster.get("canonical")
        members = cluster.get("members") or []
        member_pages = [(slug, _find_page(slug)) for slug in members]
        member_pages = [(slug, p) for slug, p in member_pages if p is not None]
        if not canonical or len(member_pages) < 2:
            print(f"  - skipping cluster (need canonical + >=2 resolvable members): {cluster}")
            continue
        folder = member_pages[0][1].relative_to(WIKI_DIR).parts[0]

        bodies = []
        for slug, p in member_pages:
            fm, body = wiki_ontology.read_page(p)
            bodies.append(f"--- [[{slug}]] (title={fm.get('title')!r}) ---\n{body}")
        messages = [
            {"role": "system", "content": (wiki_ingest.ROOT / "CLAUDE.md").read_text(encoding="utf-8")},
            {"role": "user", "content": (
                f"Merge these {len(member_pages)} wiki pages describing the same real-world "
                f"thing into ONE page at wiki/{folder}/{canonical}.md. Preserve every distinct "
                "fact and citation from every member page; note contradictions with a dated "
                "note rather than dropping either claim. Respond with ONE JSON object and "
                'nothing else: {"content": "<full merged page content, with YAML frontmatter>"}'
                f"\n\n{chr(10).join(bodies)}"
            )},
        ]
        raw = wiki_ingest._call_ollama(messages, model)
        try:
            merged = wiki_ingest._extract_json(raw)
            content = merged["content"]
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"  ✗ skipping cluster {canonical}: {exc}", file=sys.stderr)
            continue

        fm, body = wiki_ontology.split_frontmatter(content)
        fm, body = wiki_ontology.recover_embedded_frontmatter(fm, body)
        aliases = set(fm.get("aliases") or []) | set(cluster.get("aliases") or [])
        title_lower = str(fm.get("title", "")).strip().lower()
        aliases = {a for a in aliases if str(a).strip().lower() != title_lower}
        for slug, p in member_pages:
            mfm, _ = wiki_ontology.read_page(p)
            if mfm.get("title"):
                aliases.add(str(mfm["title"]))
        fm["aliases"] = sorted(aliases)
        if cluster.get("category"):
            coerced = wiki_ontology.coerce_category(cluster["category"])
            if coerced:
                fm["category"] = coerced
        if cluster.get("relations"):
            fm["relations"] = (fm.get("relations") or []) + cluster["relations"]
        fm, _notes = wiki_ontology.normalize_page_frontmatter(fm, canonical, folder)

        canonical_path = WIKI_DIR / folder / f"{canonical}.md"
        wiki_ontology.write_page(canonical_path, fm, body)

        # Repoint inbound links to every merged-away member, then delete them.
        for slug, p in member_pages:
            if slug == canonical:
                continue
            for other in wiki_lint._pages():
                if other == p:
                    continue
                ofm, obody = wiki_ontology.read_page(other)
                new_body = wiki_ontology.rewrite_link_target(obody, slug, canonical)
                if new_body != obody:
                    wiki_ontology.write_page(other, ofm, new_body)
            p.unlink()

        note = (f"\n{utc_now_iso()} **merge**: {', '.join(s for s, _ in member_pages)} "
               f"merged into [[{canonical}]] (aliases: {', '.join(fm['aliases'])}) via "
               "`wiki_normalize.py --apply-proposal`.\n")
        with open(wiki_lint.WIKI_LOG_MD, "a", encoding="utf-8") as fh:
            fh.write(note)
        print(f"  ✓ merged {members} -> [[{canonical}]]")

    wiki_ingest._rebuild_index()
    log_event({"stage": "wiki", "event": "normalize_apply_proposal", "clusters": len(clusters)})
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="One-off wiki cleanup: vocab normalization + duplicate merge")
    ap.add_argument("--apply", action="store_true",
                    help="apply the deterministic pass (default: dry-run report only)")
    ap.add_argument("--propose-merges", action="store_true",
                    help="ask the LLM for a duplicate-entity merge proposal; writes "
                         f"{PROPOSAL_PATH}, changes nothing")
    ap.add_argument("--apply-proposal", metavar="PATH",
                    help="apply an already-reviewed merge proposal JSON (destructive)")
    ap.add_argument("--model", default=wiki_ingest.DEFAULT_MODEL, help="Ollama model")
    args = ap.parse_args(argv)

    if args.apply_proposal:
        return _apply_proposal(Path(args.apply_proposal), args.model)
    if args.propose_merges:
        return _propose_merges(args.model)

    changes = deterministic_pass(apply=args.apply)
    _print_changes(changes, applied=args.apply)
    log_event({"stage": "wiki", "event": "normalize", "apply": args.apply,
              **{k: (len(v) if isinstance(v, list) else v) for k, v in changes.items()}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
