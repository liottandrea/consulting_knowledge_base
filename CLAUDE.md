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
title: <human-readable page title>
created_at: <UTC ISO8601>
updated_at: <UTC ISO8601>
sources: [<display_name>, ...]   # documents that contributed to this page
tags: [<tag>, ...]
```

Additional fields by type:

- **entity** — `entity_type: client | company | person | tool | framework | market`
- **concept** — `domain: strategy | delivery | technology | commercial | market`
- **source** — `display_name`, `location`, `relative_path` (copied from the
  ingested document's frontmatter). A source page summarises one document.

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
   explicitly (see below) — do not silently overwrite.
4. Create new pages for entities/concepts/engagements not yet in the wiki.
5. Update `wiki/index.md` (add new pages; refresh hooks).
6. Append a line to `wiki/log.md`.

A single source typically touches **8–15 wiki pages**. Cite the source by
`display_name (location)` on every claim it supports.

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

- **Orphan pages** — no inbound `[[wikilinks]]` from any other page.
- **Broken links** — `[[page-name]]` that resolves to no file.
- **Stale claims** — pages not updated after a newer source superseded them.
- **Missing concept pages** — a concept referenced across pages but with no page.
- **Missing cross-references** — entities/concepts mentioned but not linked.

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

Every entity, concept, and engagement mentioned in a page body should link to
its wiki page when one exists — and every page should carry at least one
inbound link from another page (avoid orphans). Do **not** wikilink a source
citation; that is plain text (see Source references). Obsidian is the human
reading interface and resolves `[[slug]]` links natively (open the project root
as a vault; enable Dataview + Graph view).

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
