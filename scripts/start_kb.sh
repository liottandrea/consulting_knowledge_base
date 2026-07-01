#!/bin/bash
# Consulting KB launcher (local mode, for Claude Code).
#
# One click: make sure Ollama is up, ingest any new/changed source documents and
# fold them into the wiki, refresh the qmd search index, then report ready. You
# then "use it" by asking Claude — Claude Code launches the MCP search server on
# demand over stdio (see HOWTO.md).
#
# This is the logic behind "Start Consulting KB.app". Run it directly too:
#     ./scripts/start_kb.sh

set -uo pipefail

# --- Locate the repo (this script lives in <repo>/scripts) -------------------
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || { echo "Cannot cd to repo: $REPO"; exit 1; }

OLLAMA_URL="${OLLAMA_HOST:-http://localhost:11434}"
WIKI_MODEL="${WIKI_MODEL:-qwen3.6:35b}"

say()  { printf "\n\033[1;36m▸ %s\033[0m\n" "$1"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[1;33m⚠\033[0m %s\n" "$1"; }
die()  { printf "  \033[1;31m✗ %s\033[0m\n" "$1"; pause; exit 1; }
pause(){ printf "\n\033[2mPress any key to close…\033[0m\n"; read -rsn1 _ 2>/dev/null || true; }

echo "================================================================"
echo "  Consulting KB — starting (local mode)"
echo "  repo:  $REPO"
echo "  model: $WIKI_MODEL"
echo "================================================================"

# --- Prerequisites -----------------------------------------------------------
command -v uv  >/dev/null 2>&1 || die "uv not found. Install: https://docs.astral.sh/uv/"
command -v qmd >/dev/null 2>&1 || die "qmd not found. Install: npm install -g @tobilu/qmd"

# --- 1. Ollama up? (needed to build the wiki) --------------------------------
say "Checking Ollama"
if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    ok "Ollama is running"
else
    warn "Ollama not reachable — trying to start it"
    if command -v ollama >/dev/null 2>&1; then
        (ollama serve >/dev/null 2>&1 &)
        for _ in $(seq 1 30); do
            sleep 1
            curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1 && break
        done
    fi
    if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
        ok "Ollama started"
    else
        warn "Ollama still not reachable — the wiki build will be skipped"
        warn "Start it manually (\`ollama serve\`) and re-run to build the wiki."
    fi
fi

# --- 2. Ingest new/changed sources + build the wiki --------------------------
say "Updating the knowledge base (ingest + wiki)"
WIKI_MODEL="$WIKI_MODEL" uv run python update_kb.py || warn "update_kb.py reported issues (see above)"

# --- 3. Refresh the qmd search index over the wiki ---------------------------
say "Refreshing the search index (qmd)"
if ! qmd collection list 2>/dev/null | grep -q "wiki"; then
    qmd collection add ./wiki --name wiki >/dev/null 2>&1 && ok "registered wiki collection"
fi
qmd update >/dev/null 2>&1 && ok "index updated"
qmd embed  2>&1 | grep -viE "^⏵|Downloading|Gathering|^\[2K" | tail -1

# --- 3b. Warm the search daemon (keeps models resident for fast queries) -----
say "Warming the search daemon"
qmd mcp --http --daemon --port 8181 >/dev/null 2>&1 || true
# One query through the wrapper loads the models into the daemon so the first
# real question from Claude is already fast (not a cold model load).
QMD_RERANK=1 uv run python -c "import mcp_server; mcp_server.search('warmup', top_k=1)" \
    >/dev/null 2>&1 && ok "daemon warm on :8181" || warn "daemon warm-up skipped"

# --- 4. Ensure the MCP server is registered with Claude Code -----------------
say "Claude Code MCP registration"
if command -v claude >/dev/null 2>&1; then
    if claude mcp list 2>/dev/null | grep -q "consult-kb"; then
        ok "consult-kb already registered"
    else
        claude mcp add consult-kb -- uv --directory "$REPO" run python mcp_server.py \
            >/dev/null 2>&1 && ok "registered consult-kb with Claude Code" \
            || warn "could not auto-register — see HOWTO.md for the manual command"
    fi
else
    warn "claude CLI not found — register manually (see HOWTO.md)"
fi

echo
echo "================================================================"
ok "Ready. Ask Claude, e.g.:  \"search_kb: what is Acme Prism?\""
echo "   (Claude Code launches the search server automatically.)"
echo "================================================================"
pause
