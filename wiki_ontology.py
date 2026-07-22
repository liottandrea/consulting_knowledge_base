"""Single source of truth for the wiki ontology + normalization helpers.

The controlled vocabularies here MUST match the "Controlled vocabularies" and
"Relations" sections of CLAUDE.md / AGENTS.md. wiki_ingest (write-time enforcement),
wiki_normalize (one-off cleanup), and wiki_lint (health checks) all import from
here so there is exactly one definition of "valid".
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# --- Controlled vocabularies -------------------------------------------------
ENTITY_TYPES = frozenset({
    "client", "company", "partner", "person", "product", "platform",
    "tool", "framework", "regulation", "team", "market",
})
CONCEPT_DOMAINS = frozenset({
    "strategy", "delivery", "technology", "data", "commercial", "market", "governance",
})
CATEGORIES = frozenset({
    "ai-governance", "genai-agentic", "forecasting-demand", "data-platform-mlops",
    "retail-cpg", "insurance-finance", "energy", "healthcare-public", "procurement",
    "marketing-analytics", "knowledge-graph", "delivery-methodology",
    "sales-proposal", "company-capability",
})
TAGS = frozenset({
    # sectors
    "retail", "cpg", "insurance", "finance", "energy", "healthcare",
    "public-sector", "telecom", "logistics", "pharma",
    # capabilities
    "ai-governance", "genai", "agentic-ai", "mlops", "forecasting",
    "demand-forecasting", "causal-inference", "knowledge-graph", "data-platform",
    "data-quality", "nlp", "computer-vision", "recommendation", "optimization",
    "analytics", "rag",
    # delivery / commercial
    "poc", "proposal", "roadmap", "workshop", "case-study", "accelerator",
    "methodology", "discovery", "migration",
    # governance
    "eu-ai-act", "iso-42001", "nist-ai-rmf", "risk-management", "responsible-ai",
})
RELATIONS = frozenset({
    "client_of", "engaged_by", "delivered_by", "uses", "integrates_with",
    "part_of", "competes_with", "authored_by", "produces", "governs", "successor_of",
})
MAX_TAGS = 8

# --- Synonym / coercion maps (invalid value -> controlled value | None=drop) --
ENTITY_TYPE_SYNONYMS = {
    "site": "client", "division": "company", "company | tool": "company",
    "company | division": "company", "vendor": "partner", "supplier": "partner",
    "solution": "product", "application": "product", "app": "product",
    "model": "tool", "service": "product", "organization": "company",
    "organisation": "company", "individual": "person", "regulator": "regulation",
    "standard": "framework", "methodology": "framework",
}
DOMAIN_SYNONYMS = {
    "procurement": "commercial", "process": "delivery", "operations": "delivery",
    "finance": "commercial", "compliance": "governance", "risk": "governance",
    "analytics": "technology", "ml": "technology", "ai": "technology",
}
TAG_SYNONYMS = {
    "ai": "genai", "gen-ai": "genai", "generative-ai": "genai",
    "agentic": "agentic-ai", "agent": "agentic-ai", "llm": "genai",
    "ml": "mlops", "machine-learning": "mlops", "ml-ops": "mlops",
    "demand": "demand-forecasting", "forecast": "forecasting",
    "causal": "causal-inference", "kg": "knowledge-graph",
    "governance": "ai-governance", "responsible": "responsible-ai",
    "euaiact": "eu-ai-act", "iso42001": "iso-42001", "nist": "nist-ai-rmf",
    "case-studies": "case-study", "pocs": "poc", "proof-of-concept": "poc",
    "cpg-retail": "retail", "consumer-goods": "cpg", "fmcg": "cpg",
    "data": "data-platform", "data-eng": "data-platform", "opt": "optimization",
}

_TITLE_QUALIFIER_RE = re.compile(r"\s*\(([^)]+)\)\s*$")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
# Qualifier words we strip from titles (they belong in entity_type/domain, not the name)
_QUALIFIER_WORDS = ENTITY_TYPES | CONCEPT_DOMAINS | {"company", "tool", "client", "person"}


# --- Page frontmatter I/O ----------------------------------------------------
_UNQUOTED_COLON_VALUE_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w]*):[ \t]+(?P<value>(?!['\"\[{|>-]).*:.*)$"
)


def _requote_colon_values(block: str) -> str:
    """Best-effort repair for a common write-time mistake: an unquoted scalar
    value containing ": " (e.g. `title: Absence IQ: Predicting X`), which is
    invalid YAML (parses as a nested mapping) and makes yaml.safe_load raise.
    Wraps just the offending values in quotes so the rest of the block still
    parses; never invoked when the block already parses cleanly."""
    out = []
    for line in block.split("\n"):
        m = _UNQUOTED_COLON_VALUE_RE.match(line)
        if m:
            value = m.group("value").replace('"', '\\"')
            out.append(f'{m.group("indent")}{m.group("key")}: "{value}"')
        else:
            out.append(line)
    return "\n".join(out)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body str) for raw page text."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end]
            try:
                fm = yaml.safe_load(block) or {}
            except yaml.YAMLError:
                try:
                    fm = yaml.safe_load(_requote_colon_values(block)) or {}
                except yaml.YAMLError:
                    fm = {}
            return (fm if isinstance(fm, dict) else {}), text[end + 4:].lstrip("\n")
    return {}, text


_FM_KV_LINE_RE = re.compile(r"^[A-Za-z_][\w]*:(\s|$)")


def recover_embedded_frontmatter(fm: dict, body: str) -> tuple[dict, str]:
    """Defend against a common LLM-merge failure mode: the model emits a
    SECOND, unfenced key:value block at the top of the body (no leading
    '---'), so split_frontmatter only captured the first (often thinner)
    block and left the real fields sitting unparsed in the body. If the body
    starts with lines that look like frontmatter and run up to a blank line
    followed by a heading, parse that block as YAML and merge it into fm
    (real values win over the outer block's), returning the body with that
    block stripped. No-op if the body doesn't match this shape."""
    lines = body.split("\n")
    if not lines or not _FM_KV_LINE_RE.match(lines[0]):
        return fm, body
    end = None
    for i, line in enumerate(lines):
        if line.startswith("# "):
            end = i
            break
    if end is None:
        return fm, body
    block = "\n".join(lines[:end]).strip()
    try:
        embedded = yaml.safe_load(block)
    except yaml.YAMLError:
        return fm, body
    if not isinstance(embedded, dict):
        return fm, body
    merged = dict(fm)
    merged.update(embedded)
    return merged, "\n".join(lines[end:]).strip()


def read_page(path: Path) -> tuple[dict, str]:
    """Return (frontmatter dict, body str) for a wiki page."""
    return split_frontmatter(Path(path).read_text(encoding="utf-8"))


def write_page(path: Path, fm: dict, body: str) -> None:
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    Path(path).write_text(f"---\n{fm_yaml}\n---\n\n{body.strip()}\n", encoding="utf-8")


# --- Normalizers -------------------------------------------------------------
def title_case(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def normalize_title(title: str | None, slug: str) -> str:
    """A human Title-Case name with no baked '(type)' qualifier."""
    t = (title or "").strip()
    if not t:
        return title_case(slug)
    # Strip a trailing "(qualifier)" when it's a type/domain word (repeatedly).
    while True:
        m = _TITLE_QUALIFIER_RE.search(t)
        if not m or m.group(1).strip().lower() not in _QUALIFIER_WORDS:
            break
        t = t[: m.start()].strip()
    # Slug-style title (all lowercase, hyphenated, no spaces) -> Title Case.
    if t and t == t.lower() and ("-" in t or "_" in t) and " " not in t:
        t = title_case(t)
    return t or title_case(slug)


def coerce_entity_type(v) -> str | None:
    if not v:
        return None
    s = str(v).strip().lower()
    if s in ENTITY_TYPES:
        return s
    return ENTITY_TYPE_SYNONYMS.get(s)


def coerce_domain(v) -> str | None:
    if not v:
        return None
    s = str(v).strip().lower()
    if s in CONCEPT_DOMAINS:
        return s
    return DOMAIN_SYNONYMS.get(s)


def coerce_category(v) -> str | None:
    if not v:
        return None
    s = str(v).strip().lower()
    return s if s in CATEGORIES else None


def normalize_tags(tags) -> list[str]:
    """Map to the controlled taxonomy, drop unknowns, dedupe, cap at MAX_TAGS."""
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for t in tags:
        s = str(t).strip().lower().replace(" ", "-")
        s = TAG_SYNONYMS.get(s, s)
        if s in TAGS and s not in out:
            out.append(s)
    return out[:MAX_TAGS]


def normalize_page_frontmatter(fm: dict, slug: str, folder: str) -> tuple[dict, list[str]]:
    """Coerce title/entity_type/domain/category/tags/relations to the controlled
    vocab. Returns (normalized fm, notes) — notes flag values that had to be
    defaulted because the model's value wasn't coercible, so a human can review
    the original claim in wiki/log.md rather than have it silently disappear.
    """
    fm = dict(fm)
    notes: list[str] = []
    fm["title"] = normalize_title(fm.get("title"), slug)

    if folder == "entities":
        orig = fm.get("entity_type")
        coerced = coerce_entity_type(orig)
        if coerced is None and orig:
            notes.append(f"{slug}: entity_type '{orig}' not in vocab, defaulted to 'company'")
        fm["entity_type"] = coerced or "company"
    elif folder == "concepts":
        orig = fm.get("domain")
        coerced = coerce_domain(orig)
        if coerced is None and orig:
            notes.append(f"{slug}: domain '{orig}' not in vocab, defaulted to 'strategy'")
        fm["domain"] = coerced or "strategy"

    orig_cat = fm.get("category")
    coerced_cat = coerce_category(orig_cat)
    if coerced_cat is None and orig_cat:
        notes.append(f"{slug}: category '{orig_cat}' not in vocab, defaulted to 'company-capability'")
    fm["category"] = coerced_cat or "company-capability"

    fm["tags"] = normalize_tags(fm.get("tags"))

    relations = fm.get("relations")
    if isinstance(relations, list):
        fm["relations"] = [
            r for r in relations
            if isinstance(r, dict) and len(r) == 1 and next(iter(r)) in RELATIONS
        ]

    return fm, notes


def extract_wikilinks(text: str) -> list[str]:
    """All [[slug]] targets in a string (slug part only, lowercased)."""
    return [m.group(1).strip().lower() for m in _WIKILINK_RE.finditer(text or "")]


def relation_target_slug(target) -> str | None:
    """Slug from a relation target which may be '[[slug]]' or a bare slug."""
    if not target:
        return None
    m = _WIKILINK_RE.search(str(target))
    return (m.group(1) if m else str(target)).strip().lower()


def rewrite_unresolved_links(text: str, known_slugs) -> str:
    """Replace any [[slug]] wikilink whose target isn't in known_slugs with
    plain text (link-only-if-exists, per CLAUDE.md)."""
    known = {s.lower() for s in known_slugs}

    def _sub(m: re.Match) -> str:
        slug = m.group(1).strip()
        return m.group(0) if slug.lower() in known else slug

    return _WIKILINK_RE.sub(_sub, text or "")


def rewrite_link_target(text: str, old_slug: str, new_slug: str | None) -> str:
    """Repoint every [[old_slug]] reference to [[new_slug]], or to plain text
    (the bare slug text) if new_slug is None. Used when merging or deleting a
    page so inbound links stay valid."""
    old = old_slug.strip().lower()

    def _sub(m: re.Match) -> str:
        target = m.group(1).strip()
        if target.lower() != old:
            return m.group(0)
        return f"[[{new_slug}]]" if new_slug else target

    return _WIKILINK_RE.sub(_sub, text or "")


def filter_relations(relations, known_slugs) -> list:
    """Keep only relations whose key is in RELATIONS and target resolves."""
    known = {s.lower() for s in known_slugs}
    out = []
    for r in relations or []:
        if not (isinstance(r, dict) and len(r) == 1):
            continue
        key, target = next(iter(r.items()))
        if key not in RELATIONS:
            continue
        tgt = relation_target_slug(target)
        if tgt and tgt in known:
            out.append(r)
    return out
