"""Processed-markdown parser.

Formerly Stage 3 (Chroma embedding). Chroma has been retired: retrieval now runs
through qmd hybrid search over the /wiki layer (see mcp_server.py), and knowledge
is synthesised into the wiki by wiki_ingest.py rather than embedded as chunks.

What remains here is the single source of truth for reading back the chunked
markdown that ingest.py writes to processed/. wiki_ingest.py imports
`parse_processed` from this module so there is exactly one parser for the
`<!-- chunk index=N ... -->` marker format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml


# --- Parsing processed markdown ---------------------------------------------
@dataclass
class ParsedChunk:
    index: int
    text: str
    meta: dict = field(default_factory=dict)


_MARKER_RE = re.compile(r"^<!-- chunk (?P<attrs>.*?) -->\s*$", re.MULTILINE)
_ATTR_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')


def _parse_marker(attrs: str) -> dict:
    out: dict = {}
    for key, raw in _ATTR_RE.findall(attrs):
        val = raw[1:-1] if raw.startswith('"') else raw
        if key == "index" or key == "slide" or key == "page":
            try:
                out[key] = int(val)
            except ValueError:
                pass
        elif key == "slides":
            out["slides"] = val  # keep CSV string
        elif key in ("table", "image"):
            out[key] = (val == "true")
        elif key == "heading":
            out["heading"] = val
    return out


def parse_processed(md_path: Path) -> tuple[dict, list[ParsedChunk]]:
    """Return (frontmatter, chunks) from a processed markdown file."""
    text = md_path.read_text(encoding="utf-8")
    fm: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = yaml.safe_load(text[3:end]) or {}
            body = text[end + 4:]

    chunks: list[ParsedChunk] = []
    matches = list(_MARKER_RE.finditer(body))
    for i, m in enumerate(matches):
        attrs = _parse_marker(m.group("attrs"))
        start = m.end()
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk_text = body[start:stop].strip()
        if not chunk_text:
            continue
        idx = attrs.pop("index", i)
        chunks.append(ParsedChunk(index=idx, text=chunk_text, meta=attrs))
    return fm, chunks


def _epoch(iso: str | None) -> int | None:
    """UTC epoch seconds from an ISO timestamp, or None if unparseable."""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None
