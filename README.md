# Consulting KB — Compounding Wiki

A local knowledge base over consulting source documents (slides, reports, decks)
that **synthesises** knowledge across documents rather than retrieving raw chunks.
All processing — extraction, wiki synthesis, search — runs on this machine; no
document content is sent to any external API.

## Architecture — three layers

```
sources  →  wiki  →  agents
```

1. **Sources** — raw input files (`.pptx` / `.pdf` / `.docx`), immutable, living
   anywhere: OneDrive, a local inbox, a NAS. Configured in `sources.json`.
2. **Wiki** — LLM-generated markdown under `/wiki/`: cross-linked pages for
   entities, concepts, engagements, sources, and syntheses. Compounds over time,
   readable/editable in **Obsidian**. A local model (Ollama) writes it.
3. **Agents** — query the wiki through **qmd** hybrid search, exposed as the
   `search_kb` MCP tool. Any MCP-compatible agent works (Claude Code, Cursor,
   Windsurf, Codex, Gemini CLI, Aider, …).

The pipeline: **ingest** a source to chunked markdown → record an append-only
**manifest** event → the LLM **integrates** the source into the wiki → agents
**query** the wiki. `CLAUDE.md` / `AGENTS.md` tell the LLM how the wiki is
structured; they are the operating manual for the whole system.

## Layout

```
sources.json          source-directory config (gitignored — machine-specific)
sources.example.json  template to copy on a new machine
manage_sources.py     add/list/remove/check source directories

sources/              a local source root (gitignored)
processed/            docling markdown + frontmatter, one file per source (gitignored)
logs/                 pipeline.jsonl structured logs (gitignored)
manifest.json         append-only per-file event log (gitignored)

wiki/                 LLM-generated wiki (Obsidian vault); tracked stubs only
  index.md            catalogue of every page
  log.md              append-only record of wiki operations
  sources/ entities/ concepts/ engagements/ synthesis/

CLAUDE.md / AGENTS.md wiki schema + agent operating manual (identical copies)

ingest.py             extract + chunk (Docling). doc_type fixed by extension.
update_kb.py          diff sources vs manifest, ingest changes, build the wiki.
wiki_ingest.py        integrate one processed source into the wiki (Ollama).
wiki_lint.py          health-check the wiki (orphans, broken links, leaks, …).
embed.py              processed-markdown parser (shared by wiki_ingest).
mcp_server.py         MCP server: search_kb + kb_info, backed by qmd.
kb_common.py          shared paths, hashing, JSONL logger, source-dir config.
```

## Prerequisites

- **Python 3.12** + [uv](https://docs.astral.sh/uv/).
- **Ollama** with a generative model for wiki synthesis:
  ```bash
  ollama pull qwen3.6:35b      # default; or set WIKI_MODEL to another model
  ollama serve                 # start the local server
  ```
- **qmd** (Node CLI — hybrid search over the wiki):
  ```bash
  npm install -g @tobilu/qmd   # or: bun install -g @tobilu/qmd
  ```
  qmd embeds locally (GGUF models auto-downloaded on first `embed`/`query` to
  `~/.cache/qmd`). No API keys.
- **Obsidian** (optional, human reading interface) — open the project root as a
  vault; enable Dataview + Graph view.

## Getting started (new machine)

```bash
git clone <repo> && cd consult_kb
uv sync                                       # install Python deps

cp sources.example.json sources.json          # then edit paths for this machine
uv run python manage_sources.py check          # verify dirs resolve + count files

ollama pull qwen3:32b && ollama serve          # (in another shell)
npm install -g @tobilu/qmd

uv run python update_kb.py                     # ingest sources + build the wiki
qmd collection add ./wiki --name wiki          # index the wiki for search
qmd update && qmd embed
```

Then open the project root in Obsidian to browse the wiki.

## One-click launcher (macOS)

For day-to-day use there's a double-clickable app that refreshes the KB (ingest +
wiki + search index) and confirms it's ready. Build it once per machine, then
double-click it whenever you add documents:

```bash
./scripts/make_launcher.sh        # creates "Start Consulting KB.app"
```

See **[HOWTO.md](HOWTO.md)** for the plain-English usage guide (setup, daily
workflow, troubleshooting). The app runs [`scripts/start_kb.sh`](scripts/start_kb.sh).

## Two-phase update

`update_kb.py` runs both phases in one command:

```bash
uv run python update_kb.py
```

1. **Ingest** — diffs each source (by SHA256 of content, not mtime) against the
   manifest and ingests only new/changed files to `processed/`. Deletions are
   recorded. Manifest keys are namespaced `"{source}/{relative_path}"`.
2. **Wiki integration** — for each new/changed file, a local model (Ollama)
   folds the content into the wiki (updating/creating pages, cross-linking).
   If Ollama is not running, this phase is skipped with a warning; the manifest
   is already saved, so you can integrate later:
   ```bash
   uv run python wiki_ingest.py processed/<filename>.md
   uv run python wiki_ingest.py processed/<filename>.md --dry-run   # preview
   ```

After a wiki change, refresh the search index: `qmd update && qmd embed`.

## Managing source directories

```bash
uv run python manage_sources.py list
uv run python manage_sources.py add --name "OneDrive Main" --path "/Users/..." --recurse
uv run python manage_sources.py add            # interactive
uv run python manage_sources.py remove "OneDrive Main"
uv run python manage_sources.py check          # verify + count files per source
```

Each `sources.json` entry: `name` (label used in citations), `path` (absolute or
repo-relative), `recurse` (walk subdirectories). Missing dirs are skipped with a
logged warning — a OneDrive folder need not be synced on every machine.

## Bulk backfill (large corpora)

`update_kb.py` runs ingest + wiki in one pass — fine for a few new files. For a
large backfill (hundreds of documents, hours of local synthesis) use the
**resumable** driver, which processes one file at a time and commits after each,
so it survives interruptions and skips already-done files on re-run:

```bash
# preview what would run (change nothing)
uv run python scripts/populate_kb.py --filter "OneDrive Projects/globex" --dry-run
# pilot a couple of folders first, review the wiki, then run the full backfill
uv run python scripts/populate_kb.py --filter "OneDrive Projects/acme-corp,OneDrive Projects/globex"
uv run python scripts/populate_kb.py            # everything (resumable; run overnight)
```

`--limit N` caps a run; `--model` overrides `WIKI_MODEL`. After it finishes,
refresh search with `qmd update && qmd embed` (or just run the launcher).

## Lint

```bash
uv run python wiki_lint.py            # deterministic report
uv run python wiki_lint.py --fix      # + safe auto-fixes (stub broken links, fill frontmatter)
uv run python wiki_lint.py --llm      # + LLM findings (missing concept pages, contradictions)
```

Checks: orphan pages, broken `[[wikilinks]]`, missing frontmatter, raw
filesystem-path leaks (should be `display_name` instead), and stale pages.
Findings are logged; the `--llm` pass appends to `wiki/log.md`.

## The `search_kb` tool

```
search_kb(query, doc_type?, client?, date_from?, date_to?, top_k=5)
```

Returns the most relevant wiki excerpts (pages, not raw chunks) with citations
and a relevance score. The `doc_type`/`client`/`date_*` filters are accepted for
backwards compatibility but not applied server-side — qmd searches the
synthesised wiki, so narrow the query text instead. `kb_info()` returns the wiki
catalogue (`wiki/index.md`).

Run the server over stdio (how a local MCP client launches it):

```bash
uv run python mcp_server.py
```

Register with Claude Code:

```bash
claude mcp add consult-kb -- \
  uv --directory /path/to/consult_kb run python mcp_server.py
```

Any MCP-compatible agent can use it. `CLAUDE.md` and `AGENTS.md` are both
maintained (identical) so agents that read either filename see the same schema.

## Docker

A single `mcp-server` service serves qmd hybrid search over the wiki (Chroma is
gone). Ingest/update/wiki-build and re-indexing run as one-off commands.

```bash
docker compose up -d                                       # start mcp-server
docker compose run --rm mcp-server python update_kb.py     # ingest + build wiki
docker compose run --rm mcp-server sh -c "qmd update && qmd embed"   # re-index
docker compose logs -f mcp-server
```

MCP server is reachable at `http://localhost:8765/mcp` (streamable-http). The
wiki, `sources.json`, and the qmd index/models are mounted from the host.

## How chunking works

- **doc_type is decided once, from the file extension** (in `ingest.py`) and
  written to frontmatter; later stages never re-infer it.
- **Slides** (`pptx`): one chunk per slide (title + body + speaker notes + table
  text + image OCR). Near-empty slides merge forward.
- **Prose** (`pdf`/`docx`): H≤2 headings are hard chunk boundaries; the section
  heading is prepended to every chunk for context. Tables are kept atomic
  (rendered as markdown); images are OCR'd (RapidOCR/onnxruntime).
- Processed frontmatter carries `display_name`, `location`, `source_root`,
  `relative_path` — so the wiki and citations use readable names, never paths.

## Configuration (env)

| Var | Default | Purpose |
|-----|---------|---------|
| `WIKI_MODEL` | `qwen3.6:35b` | Ollama model for wiki synthesis. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API base URL. |
| `QMD_MODE` | `query` | `query` (hybrid) / `search` (BM25) / `vsearch` (vector). |
| `QMD_RERANK` | _(unset)_ | Enable qmd's LLM reranker for better ordering. With the daemon (below) the model stays warm, so repeat queries stay ~sub-2s. |
| `QMD_NO_DAEMON` | _(unset)_ | Force the qmd CLI per call instead of the warm daemon. By default the server auto-starts a `qmd mcp --http --daemon` on `:8181` and queries it, so models stay resident across calls. |
| `QMD_DAEMON_PORT` | `8181` | Port for the persistent qmd daemon. |
| `QMD_INDEX` / `QMD_COLLECTION` | `index` / `wiki` | qmd index + collection to search. |
| `WIKI_DIR` | `./wiki` | Wiki directory (used by `kb_info`). |
| `KB_MANIFEST` | `./manifest.json` | Manifest path (Docker keeps it under `logs/`). |
| `MCP_TRANSPORT` | `stdio` | `streamable-http` for the Docker service. |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | HTTP bind for the MCP service. |
| `HF_HOME` | _(default HF cache)_ | Tokenizer cache location (ingest). |

## Dependencies

`uv.lock` is the canonical lock; local installs (`uv sync`) and the Docker image
both use it. `requirements.txt` is a generated pip-equivalent export — regenerate
after any dependency change:

```bash
uv export --no-hashes --no-dev --no-emit-project -o requirements.txt
```

qmd is a Node package (`@tobilu/qmd`), installed separately via npm — it is not a
Python dependency. (`opencv-python-headless` is swapped in only inside the Docker
image — see the Dockerfile — so it is absent from the lockfile and requirements.)
