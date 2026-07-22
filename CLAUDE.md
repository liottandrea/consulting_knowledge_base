# Consulting KB — Wiki Schema & Agent Operating Manual

> **SYNC REQUIREMENT:** `CLAUDE.md` and `AGENTS.md` are byte-identical copies.
> Any edit to one MUST be applied to the other in the same change. Different
> agents read different filenames by convention; both must see the same schema.

This repo is a **compounding wiki**, not a chunk-retrieval RAG store. Raw source
documents (decks, reports, docs) are extracted to `/processed/`, then an LLM
integrates their knowledge into a cross-linked wiki under `/wiki/`. Agents query
the wiki (via qmd hybrid search / the `search_kb` MCP tool), read pages, and
synthesise answers. Knowledge accumulates and cross-references over time.

Three layers: **sources** (immutable, may live anywhere — OneDrive, local, NAS) →
**wiki** (LLM-generated, readable in Obsidian) → **agents** (query the wiki).

---

## Wiki structure

```
/wiki
  index.md          catalogue of every page (update on every ingest)
  log.md            append-only chronological record of wiki operations
  /sources/         one summary page per ingested source document
  /entities/        one page per named entity (client, company, person, tool, ...)
  /concepts/        one page per concept, framework, or method
  /engagements/     one page per client engagement or project
  /synthesis/       cross-source analysis pages (filed from good query answers)
```

**Create a new page** when a distinct entity/concept/engagement first appears and
has no page. **Update the existing page** when new information touches something
already covered. Prefer updating over creating near-duplicates; if two pages
describe the same thing, merge them and leave a note in `log.md`.

---

## Page types and frontmatter schema

Every wiki page begins with YAML frontmatter. Minimum for all pages:

```yaml
type: source | entity | concept | engagement | synthesis
title: <Human Title Case name — NO baked "(type)" qualifier>
created_at: <UTC ISO8601>
updated_at: <UTC ISO8601>
category: <one value from the Category vocabulary below>
sources: [<display_name (location)>, ...]   # documents that contributed
tags: [<tag>, ...]                            # from the Tag taxonomy below only
relations: [ {<relation>: "[[slug]]"}, ... ]  # typed edges — see Relations
```

Additional fields by type:

- **entity** — `entity_type` (one value from the vocabulary below); optional
  `aliases: [<other names/spellings>, ...]` (all variant names fold into ONE page).
- **concept** — `domain` (one value from the vocabulary below).
- **source** — `display_name`, `location`, `relative_path` (copied from the
  ingested document's frontmatter). A source page summarises one document.

### Controlled vocabularies (use ONLY these values)

- **entity_type**: `client · company · partner · person · product · platform ·
  tool · framework · regulation · team · market`
- **concept domain**: `strategy · delivery · technology · data · commercial ·
  market · governance`
- **category** (exactly one per page): `ai-governance · genai-agentic ·
  forecasting-demand · data-platform-mlops · retail-cpg · insurance-finance ·
  energy · healthcare-public · procurement · marketing-analytics ·
  knowledge-graph · delivery-methodology · sales-proposal · company-capability`
- **tags** (0–8 per page, from this taxonomy only — facets, not freeform):
  sectors — `retail · cpg · insurance · finance · energy · healthcare ·
  public-sector · telecom · logistics · pharma`;
  capabilities — `ai-governance · genai · agentic-ai · mlops · forecasting ·
  demand-forecasting · causal-inference · knowledge-graph · data-platform ·
  data-quality · nlp · computer-vision · recommendation · optimization ·
  analytics · rag`;
  delivery/commercial — `poc · proposal · roadmap · workshop · case-study ·
  accelerator · methodology · discovery · migration`;
  governance — `eu-ai-act · iso-42001 · nist-ai-rmf · risk-management ·
  responsible-ai`.

Do not invent new vocabulary values. If nothing fits, pick the closest and note
the gap in `wiki/log.md` (the schema is co-evolved — see Hard rules).

### Relations (typed graph edges)

`relations` is a list of single-key maps, each `{<relation>: "[[target-slug]]"}`.
The target MUST be an existing wiki page (link-only-if-exists — see
Cross-referencing). Relation vocabulary:

`client_of · engaged_by · delivered_by · uses · integrates_with · part_of ·
competes_with · authored_by · produces · governs · successor_of`

```yaml
relations:
  - client_of: "[[our-firm]]"          # this client is served by our firm
  - uses: "[[vera-model]]"
  - part_of: "[[acme-capital]]"
```

One real-world thing = one page. Never create a second page for a name variant;
add the variant to `aliases` on the canonical page instead.

---

## Source references (hard rule)

Always cite documents by `display_name` and `location`, **never** by raw
filesystem path. Correct:

> Client X Q3 2026 Strategy (OneDrive Main), slide 4

Not:

> /Users/.../OneDrive/consulting/sources/clients/acme/Q3-strategy.pptx · slide 4

This applies everywhere: page bodies, the frontmatter `sources` list, and
cross-references. `display_name` and `location` are in every processed
document's frontmatter — use them.

**A source citation is plain text, NEVER a `[[wikilink]]`.** Write
`Acme Prism OnePager (Local)` — do not write `[[Acme Prism OnePager (Local)]]`.
Wikilinks point only to other wiki pages (see Cross-referencing). Source
documents are not wiki pages (their summary lives under `/sources/`, which you
may link to by its slug, e.g. `[[acme-prism-onepager]]`).

---

## Ingest workflow

When a new source is processed, integrate it into the wiki:

1. Read the source's processed markdown from `/processed/` (chunk markers carry
   slide/heading/page context).
2. Identify which existing entity, concept, and engagement pages it touches
   (check `wiki/index.md`).
3. Update those pages to integrate the new information. Note contradictions
   explicitly (see below) — do not silently overwrite. Fold name variants into
   the canonical page's `aliases` — never create a near-duplicate entity.
4. Create new pages for entities/concepts/engagements not yet in the wiki, each
   with full frontmatter (title, category, entity_type/domain, tags, relations).
5. Append a line to `wiki/log.md`.

Do **not** write `wiki/index.md` — it is regenerated automatically from page
frontmatter after every ingest. A single source typically touches **8–15 wiki
pages**. Cite the source by `display_name (location)` on every claim it supports,
and give every new page a `category`, valid `tags`, and at least one `relation`.

---

## Query workflow

1. Search `wiki/index.md` first to identify relevant pages.
2. Read those pages.
3. Synthesise an answer with citations by `display_name (location)`.
4. If the answer represents durable, reusable knowledge, file it into
   `wiki/synthesis/` as a new page (with full frontmatter) so it compounds.

---

## Lint workflow

Periodically health-check the wiki (`wiki_lint.py`). Look for:

- **Orphan pages** — no inbound `[[wikilinks]]`/relations from any other page.
- **Broken links** — a `[[slug]]` or relation target that resolves to no file.
- **Vocabulary violations** — `entity_type`/`domain`/`category`/`tags` values
  outside the controlled vocabularies; missing `title`/`category`.
- **Duplicate pages** — two pages for the same real-world thing (merge into one
  canonical page + `aliases`).
- **Baked-qualifier titles** — a title ending in "(type)".
- **Stale claims** — pages not updated after a newer source superseded them.

Lint findings are written to `wiki/log.md`.

---

## Contradiction handling

When new information contradicts an existing claim, **do not silently
overwrite**. Add a dated note explaining what changed and why the newer source
supersedes (or does not). Preserve the prior claim in context. Never delete
prior content without a dated note. Example:

> **2026-07-01 (Client X Q3 2026 Strategy, OneDrive Main):** revenue target
> revised to €40M, superseding the €35M figure from the FY25 plan (older source).

---

## Cross-referencing

Link to other wiki pages with `[[slug]]`, where `slug` is the **target page's
filename without `.md`** — a lowercase, hyphenated form of its title
(`Acme Prism` → `[[acme-prism]]`, `AI Governance` → `[[ai-governance]]`). Name new
page files with the same slug convention so links resolve.

**Link-only-if-exists (hard rule).** Only emit a `[[slug]]` — in a page body OR
as a `relations` target — when that page already exists or you are creating it in
the same operation. Never link to a page you are not creating; write the name as
**plain text** instead. Broken links are a lint failure, not a "to-do".

Express structured connections as **typed `relations`** in frontmatter (see
Relations), not just prose links — this builds the queryable graph. Use prose
`[[links]]` for incidental mentions. Do **not** wikilink a source citation; that
is plain text (see Source references). Every entity, concept, and engagement
should have at least one inbound link or relation (avoid orphans). Obsidian
resolves `[[slug]]` links (and frontmatter relation links) natively in the graph
(open the `wiki/` folder as a vault; enable Dataview + Graph view).

---

## Multi-source awareness

Sources come from multiple directories (OneDrive, local inbox, NAS) configured
in `sources.json`. The wiki does not care where a file physically lives — it
cares about `display_name` and `location`. Never embed filesystem paths in wiki
content.

---

## Hard rules

- **Raw source files are immutable.** Never modify a source document, regardless
  of which directory it lives in. The LLM writes the wiki; the human curates
  sources and asks questions.
- **Wiki writes stay inside `/wiki/`.** Never write outside it during ingest.
- **This schema is co-evolved** by human and LLM as the domain matures. When it
  changes, update **both** `CLAUDE.md` and `AGENTS.md` together (see top).
