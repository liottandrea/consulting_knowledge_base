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
import subprocess
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


# --- Core search -------------------------------------------------------------
def _run_qmd(query: str, top_k: int) -> list[dict]:
    """Invoke qmd and return its parsed JSON result list (raises on failure)."""
    mode = QMD_MODE if QMD_MODE in ("query", "search", "vsearch") else "query"
    cmd = [QMD_BIN, "--index", QMD_INDEX, mode, query,
           "--format", "json", "-n", str(top_k),
           "--collection", QMD_COLLECTION]
    # Reranking is opt-in: it cold-loads a large model per call and hangs the
    # first query on CPU. Default query mode stays hybrid (BM25+vector) and fast.
    if mode == "query" and not os.environ.get("QMD_RERANK"):
        cmd.append("--no-rerank")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(
            f"qmd exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    out = proc.stdout.strip()
    if not out:
        return []
    return json.loads(out)


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
        out.append({
            "source": r.get("title") or _location_from_file(r.get("file", "")),
            "location": _location_from_file(r.get("file", "")),
            "docid": r.get("docid"),
            "score": round(float(score), 4) if isinstance(score, (int, float)) else None,
            "text": r.get("snippet") or r.get("text") or "",
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
