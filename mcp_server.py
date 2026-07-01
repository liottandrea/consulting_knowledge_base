"""Stage 4 — MCP server (qmd-backed).

Exposes the knowledge base to Claude and any other MCP-compatible agent via a
single `search_kb` tool. The tool name and parameters are unchanged from the
previous Chroma-backed server, so existing agent integrations keep working —
only the retrieval engine underneath has changed.

Retrieval now runs through **qmd** (https://github.com/tobi/qmd), an on-device
hybrid search engine (BM25 + local vector embeddings + optional LLM rerank) over
the markdown files in the /wiki directory. Chroma has been retired.

qmd is a Node CLI, installed separately (it is NOT a Python dependency):

    npm install -g @tobilu/qmd            # or: bun install -g @tobilu/qmd

One-time index setup over the wiki (see README for details):

    qmd collection add ./wiki --name wiki
    qmd update                            # index the markdown
    qmd embed                             # build local vector embeddings

This server shells out to `qmd query --format json` per request and reshapes the
result into the dict the previous `search()` returned. Citations come from the
wiki page content itself (pages cite sources by display_name + location, never
raw filesystem paths — see CLAUDE.md).

    uv run python mcp_server.py           # serve over stdio (how MCP clients launch it)

Env:
    QMD_BIN        qmd executable (default: "qmd")
    QMD_INDEX      qmd index name (default: "index")
    QMD_COLLECTION collection to search (default: "wiki")
    QMD_MODE       "query" (hybrid, default) | "search" (BM25) | "vsearch" (vector)
    QMD_RERANK     if set, enable qmd's LLM reranker in query mode. OFF by default:
                   reranking cold-loads a ~1.3GB model per call (each search spawns
                   a fresh qmd), which makes the first query hang for tens of
                   seconds on CPU. Without it, query is hybrid BM25+vector (RRF)
                   and returns in well under a second.
    WIKI_DIR       wiki directory for kb_info (default: ./wiki)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from kb_common import ROOT, log_event

DEFAULT_TOP_K = 5
MAX_TOP_K = 25

QMD_BIN = os.environ.get("QMD_BIN", "qmd")
QMD_INDEX = os.environ.get("QMD_INDEX", "index")
QMD_COLLECTION = os.environ.get("QMD_COLLECTION", "wiki")
QMD_MODE = os.environ.get("QMD_MODE", "query").lower()
WIKI_DIR = Path(os.environ.get("WIKI_DIR", ROOT / "wiki"))
WIKI_INDEX_MD = WIKI_DIR / "index.md"

# Persistent qmd daemon (keeps the embedding/rerank models warm across calls so
# repeat queries are ~sub-second instead of cold-loading a model each time).
# The wrapper auto-starts it on first use. Set QMD_NO_DAEMON=1 to force the CLI.
QMD_DAEMON_PORT = int(os.environ.get("QMD_DAEMON_PORT", "8181"))
QMD_DAEMON_URL = os.environ.get("QMD_DAEMON_URL", f"http://localhost:{QMD_DAEMON_PORT}/mcp")
USE_DAEMON = not os.environ.get("QMD_NO_DAEMON")


# --- Citation / location -----------------------------------------------------
def _location_from_file(qmd_file: str) -> str:
    """Turn qmd's file reference into a readable wiki-page location.

    qmd returns e.g. 'qmd://wiki/entities/acme.md?index=test'. We surface the
    collection-relative page path ('entities/acme.md'), never an on-disk path.
    """
    ref = qmd_file or ""
    if ref.startswith("qmd://"):
        ref = ref[len("qmd://"):]
    ref = ref.split("?", 1)[0]
    # Drop a leading '<collection>/' prefix for brevity.
    parts = ref.split("/", 1)
    if len(parts) == 2 and parts[0] == QMD_COLLECTION:
        ref = parts[1]
    return ref or "wiki"


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (--- … ---)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _clean_snippet(snippet: str) -> str:
    """Fallback cleaner for qmd's diff-style snippet: drop the '@@ … @@' hunk
    header and any YAML frontmatter lines that leaked into the excerpt."""
    fm_keys = ("type:", "title:", "created_at:", "updated_at:", "sources:", "tags:",
               "domain:", "entity_type:", "display_name:", "location:", "relative_path:")
    out = []
    for ln in snippet.splitlines():
        s = ln.strip()
        if s.startswith("@@") or s == "---" or s.startswith(fm_keys):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _excerpt(rel_path: str, fallback_snippet: str, limit: int = 800) -> str:
    """Readable excerpt for a result: the page body (frontmatter stripped),
    truncated on a word boundary. Falls back to the cleaned qmd snippet if the
    page can't be read."""
    body = ""
    try:
        body = _strip_frontmatter((WIKI_DIR / rel_path).read_text(encoding="utf-8")).strip()
    except OSError:
        pass
    if not body:
        body = _clean_snippet(fallback_snippet)
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0].rstrip() + " …"
    return body


# --- qmd invocation: daemon (warm) with CLI fallback -------------------------
_DAEMON_ROW = re.compile(r"^#(\S+)\s+(\d+)%\s+(.+?)\s+-\s+(.+)$")


def _parse_daemon_text(text: str) -> list[dict]:
    """Parse the qmd daemon's query output lines: '#docid 93% path - Title'."""
    rows: list[dict] = []
    for ln in text.splitlines():
        m = _DAEMON_ROW.match(ln.strip())
        if m:
            docid, pct, path, title = m.groups()
            rows.append({"docid": "#" + docid, "score": int(pct) / 100.0,
                         "file": path, "title": title, "snippet": ""})
    return rows


def _daemon_query(query: str, top_k: int) -> str:
    """Call the running qmd daemon's `query` MCP tool; return its text output."""
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    rerank = bool(os.environ.get("QMD_RERANK"))

    async def _call() -> str:
        async with streamablehttp_client(QMD_DAEMON_URL) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("query", {
                    "searches": [{"type": "vec", "query": query},
                                 {"type": "lex", "query": query}],
                    "collections": [QMD_COLLECTION],
                    "limit": top_k,
                    "rerank": rerank,
                })
                return "".join(getattr(c, "text", "") for c in res.content)

    return asyncio.run(_call())


def _start_daemon() -> None:
    """Start the qmd daemon (idempotent) and give it a moment to bind."""
    subprocess.run([QMD_BIN, "mcp", "--http", "--daemon", "--port", str(QMD_DAEMON_PORT)],
                   capture_output=True, text=True, timeout=30)
    time.sleep(3)


def _run_qmd_daemon(query: str, top_k: int) -> list[dict]:
    try:
        return _parse_daemon_text(_daemon_query(query, top_k))
    except Exception:
        _start_daemon()  # not running yet — start it and retry once
        return _parse_daemon_text(_daemon_query(query, top_k))


def _run_qmd_cli(query: str, top_k: int) -> list[dict]:
    """Fallback: invoke the qmd CLI and parse its JSON output."""
    mode = QMD_MODE if QMD_MODE in ("query", "search", "vsearch") else "query"
    cmd = [QMD_BIN, "--index", QMD_INDEX, mode, query,
           "--format", "json", "-n", str(top_k),
           "--collection", QMD_COLLECTION]
    if mode == "query" and not os.environ.get("QMD_RERANK"):
        cmd.append("--no-rerank")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(
            f"qmd exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    out = proc.stdout.strip()
    return json.loads(out) if out else []


def _run_qmd(query: str, top_k: int) -> list[dict]:
    """Return ranked results, preferring the warm daemon, falling back to the CLI."""
    if USE_DAEMON:
        try:
            return _run_qmd_daemon(query, top_k)
        except Exception as exc:
            log_event({"stage": "mcp", "event": "daemon_fallback", "reason": repr(exc)})
    return _run_qmd_cli(query, top_k)


def search(
    query: str,
    doc_type: str | None = None,
    client: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict]:
    """Hybrid search over the wiki via qmd. Returns up to top_k page excerpts,
    each with the wiki page it came from, a relevance score, and the text.

    The doc_type / client / date filters are accepted for backwards
    compatibility with the previous Chroma-backed signature; qmd searches the
    synthesised wiki (not raw chunks), so they are not applied server-side —
    narrow the query text instead. They remain in the signature so existing
    callers do not break.
    """
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    rows = _run_qmd(query, top_k)

    out: list[dict] = []
    for r in rows:
        score = r.get("score")
        loc = _location_from_file(r.get("file", ""))
        out.append({
            "source": r.get("title") or loc,
            "location": loc,
            "docid": r.get("docid"),
            "score": round(float(score), 4) if isinstance(score, (int, float)) else None,
            "text": _excerpt(loc, r.get("snippet") or r.get("text") or ""),
        })
    return out


def _format_results(query: str, results: list[dict], where_desc: str) -> str:
    """Render results as readable, citation-first text for an agent to consume."""
    if not results:
        return f'No results for "{query}"{where_desc}.'
    lines = [f'{len(results)} result(s) for "{query}"{where_desc}:\n']
    for i, r in enumerate(results, 1):
        score = f"score={r['score']} · " if r["score"] is not None else ""
        lines.append(f"[{i}] {score}{r['source']} ({r['location']})")
        lines.append(r["text"])
        lines.append("")
    return "\n".join(lines).rstrip()


# --- MCP server --------------------------------------------------------------
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "consult-kb",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8000")),
    )

    @mcp.tool()
    def search_kb(
        query: str,
        doc_type: str | None = None,
        client: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> str:
        """Search the consulting knowledge base wiki (synthesised from source decks,
        reports, and documents) and return the most relevant wiki excerpts.

        Results are pages from a compounding, cross-referenced wiki — entities,
        concepts, engagements, and syntheses — not raw document chunks. Each page
        cites its sources by document display name and location.

        Args:
            query: Natural-language search query.
            doc_type: Accepted for compatibility; not applied server-side.
            client: Accepted for compatibility; not applied server-side.
            date_from: Accepted for compatibility; not applied server-side.
            date_to: Accepted for compatibility; not applied server-side.
            top_k: Number of results to return (default 5, max 25).
        """
        results = search(query, doc_type, client, date_from, date_to, top_k)
        return _format_results(query, results, "")

    @mcp.tool()
    def kb_info() -> str:
        """Summarise what's in the knowledge base by reading the wiki catalogue
        (wiki/index.md). Useful before searching."""
        if WIKI_INDEX_MD.exists():
            return WIKI_INDEX_MD.read_text(encoding="utf-8")
        return (f"Wiki index not found at {WIKI_INDEX_MD}. Run `uv run python "
                "update_kb.py` to ingest sources and build the wiki.")

except ImportError:  # mcp not installed — search() still importable for tests
    mcp = None


if __name__ == "__main__":
    if mcp is None:
        raise SystemExit("The 'mcp' package is not installed.")
    log_event({"stage": "mcp", "event": "startup", "engine": "qmd",
               "index": QMD_INDEX, "collection": QMD_COLLECTION, "mode": QMD_MODE})
    # stdio for local MCP clients (e.g. `claude mcp add`); streamable-http when
    # run as the long-lived Docker service (MCP_TRANSPORT=streamable-http).
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http"):
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run()
