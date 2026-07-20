"""Wiki integration — drive a local LLM to fold a processed source into the wiki.

Takes one processed markdown file (output of ingest.py) and asks a local Ollama
model (default qwen3:32b) to integrate its knowledge into the compounding wiki:
updating entity/concept/engagement pages, creating new ones, cross-linking, and
recording the operation. This is the step that turns retrieval into synthesis.

    uv run python wiki_ingest.py processed/<file>.md
    uv run python wiki_ingest.py processed/<file>.md --model qwen3:32b --dry-run

Also callable:
    from wiki_ingest import ingest_to_wiki
    ingest_to_wiki(Path("processed/foo.pptx.md")) ->
        {"pages_written": N, "pages_updated": N, "rejections": N, "error": str|None}

Design notes:
  * The processed markdown is parsed with embed.parse_processed (one parser).
  * display_name / location come from the source frontmatter; raw filesystem
    paths are NEVER sent to the LLM or written into the wiki.
  * The LLM is given CLAUDE.md as the system prompt (the wiki schema) and must
    reply with a single JSON object; its writes are validated before being
    applied (wiki-only paths, no destructive truncation, no raw-path leaks).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from embed import parse_processed
from kb_common import ROOT, log_event, utc_now_iso

WIKI_DIR = ROOT / "wiki"
WIKI_INDEX_MD = WIKI_DIR / "index.md"
WIKI_LOG_MD = WIKI_DIR / "log.md"
SCHEMA_FILE = ROOT / "CLAUDE.md"

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# Generative model for wiki synthesis. Override with WIKI_MODEL or --model.
DEFAULT_MODEL = os.environ.get("WIKI_MODEL", "qwen3.6:35b")

# Raw filesystem paths must never appear in wiki content — cite by display_name.
_RAW_PATH_RE = re.compile(r"(/Users/|/home/|[A-Za-z]:\\|OneDrive/\S)")
# A contradiction/supersession note lets a shrinking rewrite through.
_CONTRADICTION_RE = re.compile(r"supersed|contradict|revis|no longer|replaced by",
                               re.IGNORECASE)


class OllamaUnreachable(RuntimeError):
    """Ollama is genuinely down (connection refused) — stop the whole batch."""


class OllamaError(RuntimeError):
    """Ollama is up but errored on THIS request (e.g. HTTP 500 from memory
    pressure on a large doc). Retryable per-file; must not stop the batch."""


# Context sizing for the wiki-synthesis call. num_ctx too large OOMs the model on
# smaller machines (→ HTTP 500); too small truncates the reply into invalid JSON.
# The slim page-digest prompt keeps input modest, so a moderate default works.
# Tune with WIKI_NUM_CTX / WIKI_NUM_PREDICT if you see 500s (lower) or truncation.
NUM_CTX = int(os.environ.get("WIKI_NUM_CTX", "16384"))
NUM_PREDICT = int(os.environ.get("WIKI_NUM_PREDICT", "6144"))


# --- Prompt assembly ---------------------------------------------------------
def _chunk_context(chunk) -> str:
    """A short human-readable locator for a chunk (slide / heading / page)."""
    m = chunk.meta
    if m.get("slides"):
        return f"slides {m['slides']}"
    if m.get("slide") is not None:
        return f"slide {m['slide']}"
    bits = []
    if m.get("heading"):
        bits.append(m["heading"])
    if m.get("page") is not None:
        bits.append(f"page {m['page']}")
    return " · ".join(bits) if bits else "document"


def _build_source_text(fm: dict, chunks: list) -> str:
    """Concatenate chunks with their slide/heading context for the LLM. Uses only
    display_name / location — never source_path / source_root."""
    display = fm.get("display_name") or fm.get("source_path") or "(unknown document)"
    location = fm.get("location") or "Local"
    header = (f"SOURCE DOCUMENT: {display} ({location})\n"
              f"doc_type: {fm.get('doc_type')}  |  chunks: {len(chunks)}\n")
    body = []
    for c in chunks:
        body.append(f"--- [{_chunk_context(c)}] ---\n{c.text}")
    return header + "\n" + "\n\n".join(body)


_TASK = """You are integrating the SOURCE DOCUMENT below into the wiki, following the
schema in the system prompt exactly.

Steps:
1. Identify which existing wiki pages (from EXISTING WIKI PAGES) this source
   touches; update them to integrate the new knowledge, noting contradictions
   with dated notes rather than overwriting.
2. Create new entity / concept / engagement / source pages as needed, each with
   complete YAML frontmatter per the schema.
3. Add one line to append to wiki/log.md describing this ingest.

Do NOT write wiki/index.md — the catalogue is generated automatically.

Cite the source ONLY by its display_name and location as PLAIN TEXT, e.g.
"{display} ({location})". Do NOT wrap a citation in [[ ]]. NEVER write a raw
filesystem path anywhere.

Use [[slug]] wikilinks ONLY to link to other wiki pages, where slug is the
target file name without .md (lowercase, hyphenated), e.g. [[ai-governance]],
[[acme-prism]]. Name new files with the same slug convention. Give every new
page at least one inbound link from another page (avoid orphans). The log.md
line goes in "appends", never in "writes".

Respond with ONE JSON object and NOTHING ELSE (no markdown fences, no preamble):
{{
  "writes":  [{{"path": "wiki/entities/<slug>.md", "content": "<full file content>"}}],
  "appends": [{{"path": "wiki/log.md", "line": "<one log line>"}}]
}}
Every write path MUST start with "wiki/". Provide the FULL intended content of
each file you write (you are replacing the file)."""


# Wiki page groups: (folder, index heading, frontmatter qualifier field).
_INDEX_SECTIONS = [
    ("entities", "Entities", "entity_type"),
    ("concepts", "Concepts", "domain"),
    ("engagements", "Engagements", None),
    ("sources", "Sources", "location"),
    ("synthesis", "Synthesis", None),
]


def _page_meta(p: Path) -> dict:
    """Frontmatter of a wiki page (reuses the processed-md parser; wiki pages have
    no chunk markers, so only the frontmatter comes back)."""
    try:
        fm, _ = parse_processed(p)
        return fm or {}
    except Exception:
        return {}


def _existing_pages_digest(limit: int = 20000) -> str:
    """Compact list of existing pages (slug + title + qualifier) grouped by type.

    Sent to the model in place of the full index.md so it knows what to update /
    link to — WITHOUT the whole catalogue ballooning the prompt as the wiki grows
    (the root cause of truncated, invalid-JSON replies on large backfills)."""
    lines: list[str] = []
    for folder, heading, qual in _INDEX_SECTIONS:
        d = WIKI_DIR / folder
        pages = sorted(d.glob("*.md")) if d.exists() else []
        if not pages:
            continue
        lines.append(f"{heading}:")
        for p in pages:
            fm = _page_meta(p)
            title = fm.get("title") or p.stem
            q = fm.get(qual) if qual else None
            lines.append(f"  [[{p.stem}]] {title}" + (f" ({q})" if q else ""))
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + "\n… (list truncated)"
    return text or "(no pages yet)"


def _rebuild_index() -> None:
    """Regenerate wiki/index.md deterministically from page frontmatter.

    The index is derived, not LLM-authored — so it is always complete and the
    model never has to rewrite the whole catalogue (which grew unbounded and
    caused truncation failures)."""
    parts = ["---", "type: index", "title: Wiki Index",
             f"updated_at: {utc_now_iso()}", "---", "",
             "# Wiki Index", "",
             "Catalogue of every wiki page (auto-generated on each ingest).", ""]
    for folder, heading, qual in _INDEX_SECTIONS:
        parts.append(f"## {heading}")
        parts.append("")
        d = WIKI_DIR / folder
        rows = []
        for p in (sorted(d.glob("*.md")) if d.exists() else []):
            fm = _page_meta(p)
            title = fm.get("title") or p.stem
            q = fm.get(qual) if qual else None
            rows.append(f"- [[{p.stem}]] — {title}" + (f" ({q})" if q else ""))
        parts += rows or ["_none yet_"]
        parts.append("")
    WIKI_INDEX_MD.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


def _build_messages(fm: dict, chunks: list) -> list[dict]:
    schema = SCHEMA_FILE.read_text(encoding="utf-8")
    task = _TASK.format(display=fm.get("display_name") or "(document)",
                        location=fm.get("location") or "Local")
    user = (f"{_build_source_text(fm, chunks)}\n\n"
            f"===== EXISTING WIKI PAGES (update these or link to them) =====\n"
            f"{_existing_pages_digest()}\n\n"
            f"===== TASK =====\n{task}")
    return [{"role": "system", "content": schema},
            {"role": "user", "content": user}]


# --- Ollama call -------------------------------------------------------------
def _call_ollama(messages: list[dict], model: str) -> str:
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
    }).encode("utf-8")
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Reachable but errored on this request (commonly a 500 from memory
        # pressure on a big doc). Retryable per-file — do NOT stop the batch.
        raise OllamaError(f"Ollama HTTP {exc.code} on this document ({exc})") from exc
    except urllib.error.URLError as exc:
        raise OllamaUnreachable(
            f"Ollama not reachable at {OLLAMA_URL}. Run `ollama serve` to start "
            f"it, and `ollama pull {model}` if the model is not yet downloaded. "
            f"({exc})"
        ) from exc
    return (data.get("message") or {}).get("content", "")


# --- Response validation -----------------------------------------------------
def _extract_json(text: str) -> dict:
    """Parse the model's reply as JSON, tolerating stray prose/fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("model reply was not valid JSON")


def _validate_write(path_str: str, content: str, is_append: bool = False) -> str | None:
    """Return a rejection reason string, or None if the write is allowed.

    ``is_append`` skips the truncation check: an appended line is naturally much
    shorter than the file it is added to, so that heuristic doesn't apply.
    """
    # Must be inside /wiki/.
    rel = Path(path_str)
    try:
        target = (ROOT / rel).resolve()
        target.relative_to(WIKI_DIR.resolve())
    except (ValueError, RuntimeError):
        return f"write outside /wiki/: {path_str}"
    # No raw filesystem paths in content.
    if _RAW_PATH_RE.search(content):
        return f"raw filesystem path in content: {path_str}"
    # No destructive truncation without a contradiction note (full writes only).
    if not is_append and target.exists():
        existing = target.read_text(encoding="utf-8").count("\n") + 1
        new = content.count("\n") + 1
        if existing >= 4 and new < existing / 2 and not _CONTRADICTION_RE.search(content):
            return (f"would truncate {path_str} from {existing} to {new} lines "
                    "without a contradiction note")
    return None


# --- Apply -------------------------------------------------------------------
def _is_log(path_str: str) -> bool:
    return Path(path_str).name == "log.md"


def _apply(result: dict) -> dict:
    writes = list(result.get("writes") or [])
    appends = list(result.get("appends") or [])
    written = updated = rejected = 0
    rejections: list[str] = []

    # index.md is auto-generated (_rebuild_index) — ignore any model write to it.
    writes = [w for w in writes if Path(w.get("path", "")).name != "index.md"]

    # log.md is append-only by design. Models sometimes classify the log line as
    # a "write" (a full replacement), which the truncation guard would reject and
    # lose. Coerce any write targeting log.md into an append instead.
    for w in list(writes):
        if _is_log(w.get("path", "")):
            appends.append({"path": w["path"], "line": w.get("content", "")})
            writes.remove(w)

    for w in writes:
        path_str, content = w.get("path", ""), w.get("content", "")
        reason = _validate_write(path_str, content)
        if reason:
            rejected += 1
            rejections.append(reason)
            continue
        target = (ROOT / Path(path_str)).resolve()
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if content.endswith("\n") else content + "\n",
                          encoding="utf-8")
        if existed:
            updated += 1
        else:
            written += 1

    for a in appends:
        path_str, line = a.get("path", ""), a.get("line", "")
        reason = _validate_write(path_str, line, is_append=True)
        if reason:
            rejected += 1
            rejections.append(reason)
            continue
        target = (ROOT / Path(path_str)).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")

    return {"pages_written": written, "pages_updated": updated,
            "rejections": rejected, "rejection_reasons": rejections}


# --- Public entry point ------------------------------------------------------
def ingest_to_wiki(processed_md_path, dry_run: bool = False,
                   model: str = DEFAULT_MODEL) -> dict:
    """Integrate one processed markdown file into the wiki via the LLM.

    Returns {pages_written, pages_updated, rejections, error}. On a dry run,
    prints the planned writes and applies nothing.
    """
    md_path = Path(processed_md_path)
    if not md_path.exists():
        return {"pages_written": 0, "pages_updated": 0, "rejections": 0,
                "error": f"processed markdown not found: {md_path}"}

    fm, chunks = parse_processed(md_path)
    display = fm.get("display_name") or md_path.name
    if not chunks:
        return {"pages_written": 0, "pages_updated": 0, "rejections": 0,
                "error": "no chunks in processed markdown"}

    messages = _build_messages(fm, chunks)
    # Regenerate a few times: malformed JSON from a local model is often transient.
    result = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = _call_ollama(messages, model)
        except OllamaUnreachable as exc:
            # Genuinely down — surface "not reachable" so the batch stops.
            return {"pages_written": 0, "pages_updated": 0, "rejections": 0,
                    "error": str(exc)}
        except OllamaError as exc:
            # Server error on this doc (e.g. 500) — retry, then fail just this file.
            last_err = exc
            log_event({"stage": "wiki", "event": "retry", "source": display,
                       "attempt": attempt + 1, "reason": str(exc)})
            continue
        try:
            result = _extract_json(raw)
            break
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            log_event({"stage": "wiki", "event": "retry", "source": display,
                       "attempt": attempt + 1, "reason": str(exc)})
    if result is None:
        log_event({"stage": "wiki", "event": "failed", "source": display,
                   "reason": f"bad JSON from model after 3 attempts: {last_err}"})
        return {"pages_written": 0, "pages_updated": 0, "rejections": 0,
                "error": f"model did not return valid JSON: {last_err}"}

    if dry_run:
        print(f"\n[dry-run] planned wiki writes for {display}:")
        for w in result.get("writes", []):
            reason = _validate_write(w.get("path", ""), w.get("content", ""))
            flag = f"  ✗ REJECT: {reason}" if reason else "  ✓"
            print(f"{flag}  {w.get('path')}  ({w.get('content','').count(chr(10))+1} lines)")
        for a in result.get("appends", []):
            print(f"  + append {a.get('path')}: {a.get('line')}")
        return {"pages_written": 0, "pages_updated": 0,
                "rejections": sum(1 for w in result.get("writes", [])
                                  if _validate_write(w.get("path",""), w.get("content",""))),
                "error": None}

    applied = _apply(result)
    _rebuild_index()  # keep the catalogue complete + in sync, deterministically
    log_event({"stage": "wiki", "event": "ingested", "source": display,
               "location": fm.get("location"),
               "pages_written": applied["pages_written"],
               "pages_updated": applied["pages_updated"],
               "rejections": applied["rejections"],
               "rejection_reasons": applied["rejection_reasons"]})
    return {"pages_written": applied["pages_written"],
            "pages_updated": applied["pages_updated"],
            "rejections": applied["rejections"], "error": None}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Integrate a processed source into the wiki")
    ap.add_argument("processed_md", help="path to a processed/<file>.md")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default {DEFAULT_MODEL})")
    ap.add_argument("--dry-run", action="store_true", help="print planned writes, apply nothing")
    args = ap.parse_args(argv)

    res = ingest_to_wiki(args.processed_md, dry_run=args.dry_run, model=args.model)
    if res.get("error"):
        print(f"  ✗ {res['error']}", file=sys.stderr)
        return 1
    print(f"  ✓ wiki: {res['pages_written']} written, {res['pages_updated']} updated, "
          f"{res['rejections']} rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
