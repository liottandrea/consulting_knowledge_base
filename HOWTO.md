# How to use the Consulting KB

A plain-English guide. The KB reads your source documents (decks, reports, docs),
builds a cross-linked **wiki** from them, and lets you ask **Claude** questions
that are answered from that wiki with citations.

---

## One-time setup (per machine)

You need three things installed:

1. **uv** (Python runner) — https://docs.astral.sh/uv/
2. **Ollama** + the writing model — https://ollama.com
   ```bash
   ollama pull qwen3.6:35b
   ```
3. **qmd** (the search engine) — needs Node.js:
   ```bash
   npm install -g @tobilu/qmd
   ```

Then, in the project folder:

```bash
uv sync                                        # install Python deps
cp sources.example.json sources.json           # tell it where your documents live
#   edit sources.json — set the folder paths (OneDrive, a local folder, etc.)
uv run python manage_sources.py check           # confirm it finds your documents

./scripts/make_launcher.sh                       # create "Start Consulting KB.app"
```

Finally, register the KB with Claude Code (one line, once):

```bash
claude mcp add consult-kb -- \
  uv --directory "$(pwd)" run python mcp_server.py
```

---

## Everyday use

### 1. Add documents
Drop new `.pptx` / `.pdf` / `.docx` files into any folder listed in
`sources.json`. (Sources are never modified — the KB only reads them.)

### 2. Double-click **Start Consulting KB**
Find **"Start Consulting KB.app"** in the project folder (or drag it to your
Dock) and double-click it. A Terminal window opens and, on each click, it:

1. makes sure Ollama is running,
2. ingests any **new or changed** documents and folds them into the wiki,
3. refreshes the search index,
4. confirms it's registered with Claude Code, then prints **Ready**.

> First launch only: macOS may block it. Right-click the app → **Open** →
> **Open** to clear the one-time Gatekeeper warning.

Leave it when it says *Ready* (press a key to close the window).

### 3. Ask Claude
In Claude Code, just ask — Claude uses the `search_kb` tool automatically:

> *"What does Acme Prism do?"*
> *"Summarise the Globex Logistics engagement and its savings."*
> *"Which frameworks does our AI governance work cover?"*

Answers come from the wiki, cited by document name and location. To browse the
wiki yourself, open the project folder in **Obsidian**.

---

## Adding or checking source folders

```bash
uv run python manage_sources.py list                       # show configured folders
uv run python manage_sources.py add --name "OneDrive Main" --path "/Users/you/OneDrive/…" --recurse
uv run python manage_sources.py check                       # verify + count documents
```

### Loading a lot of documents at once

For a big first load (a whole OneDrive folder), the app's one-click refresh can
take many hours. Instead use the resumable backfill — it does one document at a
time and can be stopped/restarted safely:

```bash
# try a couple of folders first, review the wiki in Obsidian, then do the rest
uv run python scripts/populate_kb.py --filter "OneDrive Projects/acme-corp,OneDrive Projects/globex"
uv run python scripts/populate_kb.py            # everything (leave it running / overnight)
```

If it stops (or you close the laptop), just run the same command again — it picks
up where it left off. When it's done, double-click the app once to refresh search.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| App says "Ollama not reachable, wiki build skipped" | Start Ollama (open the app, or `ollama serve`), then click again. |
| "model … not found" during the wiki step | `ollama pull qwen3.6:35b`, or set another model: `export WIKI_MODEL=<name>`. |
| macOS won't open the app | Right-click → **Open** the first time (Gatekeeper). |
| `qmd: command not found` | `npm install -g @tobilu/qmd` (needs Node.js). |
| Claude can't find `search_kb` | Re-run the `claude mcp add …` line above, then restart Claude Code. |
| Changed a document but answers are stale | Double-click the app again — it re-ingests changed files and re-indexes. |

## What's happening under the hood

`Start Consulting KB.app` just runs [`scripts/start_kb.sh`](scripts/start_kb.sh),
which calls `update_kb.py` (ingest + wiki) and refreshes the qmd index. The
search server (`mcp_server.py`) is launched by Claude Code itself, on demand, when
you ask a question — so there's no separate server to keep running. For the full
architecture, see [README.md](README.md).
