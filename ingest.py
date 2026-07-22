"""Stage 1 — Ingestion.

Convert source documents (PPTX / PDF / DOCX) to chunked markdown with YAML
frontmatter using Docling as the *only* extraction library.

Design rules enforced here (and relied on downstream):
  * doc_type is decided ONCE, from the file extension, and written to
    frontmatter. It is never re-inferred by later stages.
      - .pptx          -> "pptx"  -> slides path (1 slide = 1 chunk)
      - .pdf / .docx   -> "pdf" / "docx" -> prose path (heading-bounded chunks)
  * Output is one markdown file per source under processed/. Chunks are
    delimited by HTML-comment markers carrying the metadata embed.py needs
    (slide number / page / heading path) so embed.py never re-chunks.
  * Structured JSONL logging to logs/pipeline.jsonl, one record per file
    (plus warnings, e.g. low-confidence table extraction).

CLI:
    uv run python ingest.py                # ingest every file in sources/
    uv run python ingest.py a.pptx b.pdf   # ingest specific files

`ingest_file()` is also imported by update_kb.py (Stage 2) to ingest only
changed files.
"""

from __future__ import annotations

import fnmatch
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc.document import (
    ContentLayer,
    PictureItem,
    SectionHeaderItem,
    TableItem,
    TextItem,
    TitleItem,
)
from docling_core.types.doc.labels import DocItemLabel

from kb_common import (
    PROCESSED_DIR,
    load_source_dirs,
    log_event,
    sha256_file,
    utc_now_iso,
)

# --- Tuning constants --------------------------------------------------------
# Chunk-size targets. The embedding model (BGE-M3) accepts up to 8192 tokens, so
# we are not bound by a 512 ceiling: a whole section — intro + a large table +
# closing text — comfortably stays in one chunk. We still cap chunk size for
# retrieval granularity (an over-large chunk dilutes its embedding) and only
# split genuinely long prose sections.
PROSE_TARGET_MAX_TOKENS = 1000       # soft cap; text splits at paragraph bounds
PROSE_OVERLAP_TOKENS = 150           # ~15% of the target
HEADING_BOUNDARY_LEVEL = 2          # a chunk never spans two H<=2 sections
SLIDE_MIN_WORDS = 4                 # a slide with <= this many body+notes words
                                    # (beyond its title) is "near-empty"
TABLE_CONFIDENCE_THRESHOLD = 0.5    # Docling table_score below this -> warn
PDF_MIN_CHARS_PER_PAGE = 100        # below this -> warn "possibly scanned (OCR off)"
IMAGE_OCR_MIN_SCORE = 0.5           # drop OCR boxes below this confidence
IMAGE_OCR_MIN_CHARS = 16            # don't emit an image chunk below this much text
IMAGE_MIN_EDGE_PX = 80              # skip tiny images (icons/logos/bullets)
SUPPORTED_EXT = {".pptx", ".pdf", ".docx"}

# Labels that count as a heading for prose sectioning / hierarchy.
_HEADING_LABELS = {DocItemLabel.TITLE, DocItemLabel.SECTION_HEADER}
# Labels whose text we treat as chunkable body prose.
_BODY_TEXT_LABELS = {
    DocItemLabel.TEXT,
    DocItemLabel.PARAGRAPH,
    DocItemLabel.LIST_ITEM,
    DocItemLabel.CODE,
    DocItemLabel.FORMULA,
    DocItemLabel.CAPTION,
    DocItemLabel.FOOTNOTE,
}

_CONVERTER: DocumentConverter | None = None


def _converter() -> DocumentConverter:
    """Lazily build a single shared DocumentConverter (model load is expensive).

    PDF OCR uses RapidOCR on the **onnxruntime** backend. Docling's default OCR
    auto-selects a torch/PP-OCRv6 model that is unsupported in this build (raises
    'Unsupported configuration: torch.PP-OCRv6.det.small') because onnxruntime
    wasn't present; pinning the backend explicitly avoids that path. onnxruntime
    is fully local (no API, no data leaves the machine) and works on both macOS
    and the Linux Docker image.

    force_full_page_ocr stays False (the default): PDFs with a real text layer
    keep their crisp extracted text, and OCR only runs on image/scanned regions.
    Scanned/image-only PDFs are still handled, and the low-text warning remains
    as a backstop signal.
    """
    global _CONVERTER
    if _CONVERTER is None:
        pdf_opts = PdfPipelineOptions()
        pdf_opts.do_ocr = True
        pdf_opts.ocr_options = RapidOcrOptions(backend="onnxruntime", lang=["english"])
        _CONVERTER = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts)}
        )
    return _CONVERTER


# --- Token counting ----------------------------------------------------------
# We size prose chunks with the SAME tokenizer as the embedding model
# (BAAI/bge-m3) so chunk budgets line up with what gets embedded in Stage 3.
# The tokenizer is a local download. If it cannot be loaded (e.g. offline /
# proxy blocks Hugging Face), we fall back to a word-based approximation and
# record which counter was used in the JSONL log — Stage 1 is about extraction
# and must not hard-fail on a missing tokenizer.
_TOKENIZER = None
_TOKENIZER_TRIED = False
EMBED_MODEL = "BAAI/bge-m3"


def _get_tokenizer():
    global _TOKENIZER, _TOKENIZER_TRIED
    if _TOKENIZER_TRIED:
        return _TOKENIZER
    _TOKENIZER_TRIED = True
    try:
        from transformers import AutoTokenizer

        _TOKENIZER = AutoTokenizer.from_pretrained(EMBED_MODEL)
    except Exception:
        _TOKENIZER = None
    return _TOKENIZER


def count_tokens(text: str) -> int:
    tok = _get_tokenizer()
    if tok is not None:
        return len(tok.encode(text, add_special_tokens=False))
    # Approximation: English averages ~0.75 words per token.
    words = len(text.split())
    return max(1, round(words / 0.75))


def _token_counter_name() -> str:
    return "bge-tokenizer" if _get_tokenizer() is not None else "word-approx"


# --- Result container --------------------------------------------------------
@dataclass
class IngestResult:
    source_path: str
    doc_type: str
    content_hash: str
    chunk_count: int
    output_path: str
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    reason: str | None = None


@dataclass
class Chunk:
    text: str
    # Marker metadata written into the processed file for embed.py to read back.
    meta: dict


# --- Sentence splitting (never break mid-sentence) ---------------------------
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def _split_sentences(paragraph: str) -> list[str]:
    parts = [s.strip() for s in _SENT_SPLIT.split(paragraph.strip()) if s.strip()]
    return parts or [paragraph.strip()]


def _pack_oversized_paragraph(paragraph: str) -> list[str]:
    """Split a single over-long paragraph into <=MAX-token pieces at sentence
    boundaries (never mid-sentence)."""
    pieces: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for sent in _split_sentences(paragraph):
        st = count_tokens(sent)
        if cur and cur_tok + st > PROSE_TARGET_MAX_TOKENS:
            pieces.append(" ".join(cur))
            cur, cur_tok = [], 0
        cur.append(sent)
        cur_tok += st
    if cur:
        pieces.append(" ".join(cur))
    return pieces


def _expand_blocks(blocks: list[tuple[str, str]]) -> list[dict]:
    """Turn section blocks into packable pieces. Text blocks are passed through
    (an over-long paragraph is first sentence-split into <=MAX pieces). Tables
    and images are atomic pieces — never split internally, regardless of size."""
    pieces: list[dict] = []
    for kind, content in blocks:
        content = content.strip()
        if not content:
            continue
        if kind == "text":
            if count_tokens(content) > PROSE_TARGET_MAX_TOKENS:
                for sub in _pack_oversized_paragraph(content):
                    pieces.append({"text": sub, "kind": "text", "atomic": False,
                                   "tok": count_tokens(sub)})
            else:
                pieces.append({"text": content, "kind": "text", "atomic": False,
                               "tok": count_tokens(content)})
        else:  # table | image — atomic
            pieces.append({"text": content, "kind": kind, "atomic": True,
                           "tok": count_tokens(content)})
    return pieces


def _chunk_blocks(blocks: list[tuple[str, str]]) -> list[tuple[str, bool, bool]]:
    """Pack a section's ordered blocks into chunks (target PROSE_TARGET_MAX_TOKENS)
    with ~15% text overlap. Text, tables and images flow into the SAME chunk
    while they fit, so a section of intro + table + closing line stays one chunk.
    A table or image is kept atomic: it shares a chunk when it fits, and only
    stands alone when it (with the running text) would exceed the budget. It is
    never split internally.

    Returns a list of (chunk_text, has_table, has_image).
    """
    pieces = _expand_blocks(blocks)
    out: list[tuple[str, bool, bool]] = []
    cur: list[dict] = []

    def emit(group: list[dict]) -> None:
        if not group:
            return
        text = "\n\n".join(p["text"] for p in group)
        has_table = any(p["kind"] == "table" for p in group)
        has_image = any(p["kind"] == "image" for p in group)
        out.append((text, has_table, has_image))

    def overlap_tail(group: list[dict]) -> list[dict]:
        # Carry trailing TEXT pieces (~PROSE_OVERLAP_TOKENS) into the next chunk;
        # never carry a table/image into overlap.
        tail: list[dict] = []
        tot = 0
        for p in reversed(group):
            if p["kind"] != "text":
                break
            if tail and tot + p["tok"] > PROSE_OVERLAP_TOKENS:
                break
            tail.insert(0, p)
            tot += p["tok"]
        return tail

    cur_tok = 0
    for p in pieces:
        # An atomic piece that cannot fit even on its own line: emit current,
        # then the atomic piece alone (no overlap around it).
        if p["atomic"] and p["tok"] > PROSE_TARGET_MAX_TOKENS:
            emit(cur)
            cur, cur_tok = [], 0
            emit([p])
            continue
        if cur and cur_tok + p["tok"] > PROSE_TARGET_MAX_TOKENS:
            emit(cur)
            cur = overlap_tail(cur)
            cur_tok = sum(q["tok"] for q in cur)
        cur.append(p)
        cur_tok += p["tok"]

    emit(cur)
    return out


# --- Per-page item access ----------------------------------------------------
def _page_items(doc, page_no: int, layer: ContentLayer):
    """All leaf items on a given page/slide for a single content layer, in order."""
    out = []
    for item, _level in doc.iterate_items(
        page_no=page_no, included_content_layers={layer}
    ):
        out.append(item)
    return out


def _item_text(item) -> str:
    return (getattr(item, "text", "") or "").strip()


def _table_markdown(item: TableItem, doc) -> str:
    """Render a table to markdown. Tables are kept atomic (never split across
    chunks), so they need a self-contained markdown representation."""
    try:
        md = item.export_to_markdown(doc).strip()
    except Exception:
        md = ""
    return md


def _table_cell_texts(item: TableItem) -> set[str]:
    """Stripped text of every cell in a table. Docling's DOCX/PPTX backends
    sometimes ALSO emit a table's cells as loose TextItems beside the TableItem;
    we use this set to suppress that duplicated text (the table is already
    captured atomically as markdown)."""
    out: set[str] = set()
    data = getattr(item, "data", None)
    if data and getattr(data, "table_cells", None):
        for cell in data.table_cells:
            t = (getattr(cell, "text", "") or "").strip()
            if t:
                out.add(t)
    return out


# --- Image OCR ---------------------------------------------------------------
# Embedded images (diagrams, screenshots, architecture maps) often carry text
# that is otherwise lost. We OCR them locally with RapidOCR/onnxruntime — the
# same local engine used for PDFs, so nothing leaves the machine — and emit the
# extracted text as its own image-derived chunk.
_IMAGE_OCR = None


def _image_ocr_engine():
    global _IMAGE_OCR
    if _IMAGE_OCR is None:
        from rapidocr import RapidOCR

        _IMAGE_OCR = RapidOCR()
    return _IMAGE_OCR


def _ocr_image_item(item: PictureItem, doc) -> str:
    """OCR a single embedded image, returning its text in rough reading order
    (top-to-bottom, left-to-right). Returns '' if the image is too small, has no
    pixels, or yields too little confident text."""
    import numpy as np

    try:
        img = item.get_image(doc)
    except Exception:
        img = None
    if img is None:
        return ""
    w, h = img.size
    if max(w, h) < IMAGE_MIN_EDGE_PX:
        return ""  # icon / bullet / logo — not worth OCR

    try:
        res = _image_ocr_engine()(np.array(img.convert("RGB")))
    except Exception:
        return ""

    txts = getattr(res, "txts", None)
    if not txts:
        return ""
    boxes = getattr(res, "boxes", None)
    scores = getattr(res, "scores", None)

    # Collect confident boxes with a top-left anchor, then group into rows.
    tol = max(12.0, h / 60.0)
    rows: dict[int, list[tuple[float, str]]] = {}
    for i, raw in enumerate(txts):
        t = (raw or "").strip()
        if not t:
            continue
        if scores is not None and i < len(scores) and scores[i] < IMAGE_OCR_MIN_SCORE:
            continue
        if boxes is not None and i < len(boxes):
            pts = boxes[i]
            top = min(p[1] for p in pts)
            left = min(p[0] for p in pts)
        else:
            top, left = float(i), 0.0
        rows.setdefault(int(top // tol), []).append((left, t))

    lines = [" ".join(t for _, t in sorted(r)) for _, r in sorted(rows.items())]
    text = "\n".join(lines).strip()
    return text if len(text) >= IMAGE_OCR_MIN_CHARS else ""


def _image_block(ocr_text: str) -> str:
    """Wrap OCR'd image text so it reads clearly in the markdown and carries
    context into its embedding."""
    return "_Image — text extracted via OCR:_\n\n" + ocr_text


# --- Table-confidence warnings ----------------------------------------------
def _table_warnings(result) -> list[str]:
    """Heuristic, non-fatal warnings about low-confidence table extraction.

    Two signals:
      1. Docling's own confidence.table_score (when present) below threshold.
      2. A detected table whose populated cell count is implausibly low for its
         declared rows x cols (suggests a botched extraction).
    """
    warnings: list[str] = []

    conf = getattr(result, "confidence", None)
    table_score = getattr(conf, "table_score", None) if conf else None
    if isinstance(table_score, (int, float)) and table_score < TABLE_CONFIDENCE_THRESHOLD:
        warnings.append(
            f"low table_score {table_score:.2f} (<{TABLE_CONFIDENCE_THRESHOLD}) "
            "reported by Docling"
        )

    doc = result.document
    for item, _ in doc.iterate_items():
        if not isinstance(item, TableItem):
            continue
        data = getattr(item, "data", None)
        if data is None:
            continue
        rows = getattr(data, "num_rows", 0) or 0
        cols = getattr(data, "num_cols", 0) or 0
        cells = data.table_cells if getattr(data, "table_cells", None) else []
        expected = rows * cols
        page = item.prov[0].page_no if item.prov else None
        if expected >= 4 and len(cells) < expected * 0.5:
            warnings.append(
                f"table on page {page}: {len(cells)} cells extracted for a "
                f"{rows}x{cols} grid (expected ~{expected}) — possible "
                "low-confidence extraction"
            )
    return warnings


# --- Slides path -------------------------------------------------------------
def _build_slide_chunks(doc, num_slides: int) -> list[Chunk]:
    """One chunk per slide: title + body + speaker notes concatenated.

    Near-empty slides (title-only / no extractable text) are merged FORWARD
    into the next slide's chunk rather than producing a near-empty embedding.
    A trailing near-empty slide is merged into the previous chunk instead.
    """
    chunks: list[Chunk] = []
    carry_text: list[str] = []
    carry_slides: list[int] = []

    for slide_no in range(1, num_slides + 1):
        body_items = _page_items(doc, slide_no, ContentLayer.BODY)
        notes_items = _page_items(doc, slide_no, ContentLayer.NOTES)

        title = ""
        body_lines: list[str] = []
        tables: list[str] = []
        images: list[str] = []
        cell_texts: set[str] = set()
        for it in body_items:
            if isinstance(it, TableItem):
                md = _table_markdown(it, doc)
                cell_texts |= _table_cell_texts(it)
                if md:
                    tables.append(md)
                continue
            if isinstance(it, PictureItem):
                ocr_text = _ocr_image_item(it, doc)
                if ocr_text:
                    images.append(_image_block(ocr_text))
                continue
            txt = _item_text(it)
            if not txt:
                continue
            if not title and isinstance(it, (TitleItem, SectionHeaderItem)):
                title = txt
            elif isinstance(it, TextItem):
                body_lines.append(txt)
        # Drop loose text duplicating a table cell already captured on this slide.
        body_lines = [b for b in body_lines if b not in cell_texts]
        notes = [t for t in (_item_text(it) for it in notes_items) if t]

        body_words = sum(len(b.split()) for b in body_lines)
        notes_words = sum(len(n.split()) for n in notes)
        table_words = sum(len(t.split()) for t in tables)
        image_words = sum(len(t.split()) for t in images)
        near_empty = (body_words + notes_words + table_words + image_words) <= SLIDE_MIN_WORDS

        parts: list[str] = []
        if title:
            parts.append(f"# {title}")
        if body_lines:
            parts.append("\n".join(body_lines))
        if tables:
            parts.append("\n\n".join(tables))
        if images:
            parts.append("\n\n".join(images))
        if notes:
            parts.append("## Speaker notes\n" + "\n".join(notes))
        slide_text = "\n\n".join(parts).strip()

        if near_empty:
            # Hold this slide's (sparse) content to merge forward.
            if slide_text:
                carry_text.append(slide_text)
            carry_slides.append(slide_no)
            continue

        merged_text = "\n\n".join([*carry_text, slide_text]).strip()
        merged_slides = [*carry_slides, slide_no]
        carry_text, carry_slides = [], []
        chunks.append(
            Chunk(
                text=merged_text,
                meta={"slide": merged_slides[0], "slides": merged_slides, "page": slide_no},
            )
        )

    # Trailing near-empty slides: fold into the last real chunk, else emit alone.
    if carry_slides:
        if chunks:
            chunks[-1].text = (chunks[-1].text + "\n\n" + "\n\n".join(carry_text)).strip()
            chunks[-1].meta["slides"] = chunks[-1].meta["slides"] + carry_slides
        elif carry_text:
            chunks.append(
                Chunk(
                    text="\n\n".join(carry_text).strip(),
                    meta={"slide": carry_slides[0], "slides": carry_slides, "page": carry_slides[0]},
                )
            )
    return chunks


# --- Prose path --------------------------------------------------------------
def _build_prose_chunks(doc) -> tuple[list[Chunk], list[str]]:
    """Walk the document body in reading order. Headings at level <= H2 are HARD
    section boundaries: a chunk never spans two such sections. Returns the
    chunks plus the full ordered heading hierarchy for frontmatter."""
    heading_hierarchy: list[str] = []

    # A "section" = the run of body content under a given H<=2 heading. Each
    # section holds ordered blocks: ("text", paragraph) or ("table", markdown).
    sections: list[dict] = []
    cur_heading_path: list[str] = []          # full path including sub-headings
    cur_section_heading: str | None = None    # the bounding H<=2 heading
    cur_section_level: int = 1                 # markdown level of that heading
    cur_page: int | None = None
    cur_blocks: list[tuple[str, str]] = []
    cur_cell_texts: set[str] = set()           # cells of tables in this section,
                                               # used to drop duplicated loose text

    def close_section():
        if cur_blocks:
            sections.append(
                {
                    "heading": cur_section_heading,
                    "level": cur_section_level,
                    "heading_path": " > ".join(cur_heading_path) if cur_heading_path else None,
                    "page": cur_page,
                    "blocks": list(cur_blocks),
                }
            )

    for item, _level in doc.iterate_items(included_content_layers={ContentLayer.BODY}):
        page = item.prov[0].page_no if getattr(item, "prov", None) else None

        if isinstance(item, (TitleItem, SectionHeaderItem)):
            txt = _item_text(item)
            if not txt:
                continue
            level = 1 if isinstance(item, TitleItem) else getattr(item, "level", 2) or 2
            heading_hierarchy.append(("  " * (level - 1)) + txt)
            if level <= HEADING_BOUNDARY_LEVEL:
                # Hard boundary: close the running section, start fresh.
                close_section()
                cur_blocks = []
                cur_cell_texts = set()
                cur_heading_path = [txt]
                cur_section_heading = txt
                cur_section_level = level
                cur_page = page
            else:
                # Sub-heading stays inside the current section as inline text.
                cur_heading_path = (cur_heading_path or [])[:1] + [txt]
                cur_blocks.append(("text", f"{'#' * level} {txt}"))
                if cur_page is None:
                    cur_page = page
            continue

        if isinstance(item, TableItem):
            md = _table_markdown(item, doc)
            cur_cell_texts |= _table_cell_texts(item)
            if md:
                if cur_page is None:
                    cur_page = page
                cur_blocks.append(("table", md))
            continue

        if isinstance(item, PictureItem):
            ocr_text = _ocr_image_item(item, doc)
            if ocr_text:
                if cur_page is None:
                    cur_page = page
                cur_blocks.append(("image", _image_block(ocr_text)))
            continue

        if isinstance(item, TextItem) and item.label in _BODY_TEXT_LABELS:
            txt = _item_text(item)
            # Drop loose text that merely duplicates a table cell already
            # captured atomically in this section.
            if txt and txt not in cur_cell_texts:
                if cur_page is None:
                    cur_page = page
                cur_blocks.append(("text", txt))

    close_section()

    chunks: list[Chunk] = []
    for sec in sections:
        heading_line = (
            f"{'#' * sec['level']} {sec['heading']}" if sec["heading"] else None
        )
        for piece, has_table, has_image in _chunk_blocks(sec["blocks"]):
            # Prepend the section heading to every chunk so it is visible in the
            # markdown AND carried into each embedding as context. Skipped only
            # for preamble content that sits before the first heading.
            text = f"{heading_line}\n\n{piece}" if heading_line else piece
            meta = {
                "page": sec["page"],
                "heading": sec["heading"],
                "heading_path": sec["heading_path"],
            }
            if has_table:
                meta["table"] = True
            if has_image:
                meta["image"] = True
            chunks.append(Chunk(text=text, meta=meta))
    return chunks, heading_hierarchy


# --- Markdown serialisation --------------------------------------------------
def _marker(meta: dict) -> str:
    """Compact, machine-parseable chunk delimiter (read back by embed.py)."""
    parts = [f"index={meta['index']}"]
    if meta.get("slide") is not None:
        parts.append(f"slide={meta['slide']}")
    if meta.get("slides"):
        parts.append("slides=" + ",".join(str(s) for s in meta["slides"]))
    if meta.get("page") is not None:
        parts.append(f"page={meta['page']}")
    if meta.get("heading_path"):
        # quote to survive '>' and spaces
        parts.append('heading="' + meta["heading_path"].replace('"', "'") + '"')
    if meta.get("table"):
        parts.append("table=true")
    if meta.get("image"):
        parts.append("image=true")
    return "<!-- chunk " + " ".join(parts) + " -->"


def _write_markdown(
    out_path: Path, frontmatter: dict, chunks: list[Chunk]
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    blocks = [f"---\n{fm}\n---", ""]
    for i, ch in enumerate(chunks):
        meta = dict(ch.meta)
        meta["index"] = i
        blocks.append(_marker(meta))
        blocks.append(ch.text)
        blocks.append("")
    out_path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")


# --- Public entry point ------------------------------------------------------
def _display_name(stem: str) -> str:
    """Human-readable document name from a filename stem: underscores/hyphens to
    spaces, title-cased. Shown to humans and agents; raw paths are never cited."""
    return stem.replace("_", " ").replace("-", " ").strip().title()


def _source_frontmatter(path: Path, source_entry: dict | None) -> dict:
    """Multi-source frontmatter fields (display_name / location / source_root /
    relative_path). Returns {} when no source_entry is supplied so single-file
    callers stay backwards-compatible."""
    if not source_entry:
        return {}
    root = Path(source_entry["path"])
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.name  # file isn't under the declared root (e.g. CLI arg)
    return {
        "display_name": _display_name(path.stem),
        "location": source_entry["name"],
        "source_root": str(root),
        "relative_path": rel,
    }


def ingest_file(path: Path, source_entry: dict | None = None) -> IngestResult:
    """Ingest a single source file into processed/<stem>.md. Returns IngestResult.

    ``source_entry`` is the dict from sources.json for this file's root (carries
    ``name`` and ``path``); when present, human-readable location fields are added
    to the frontmatter so downstream stages cite by display_name, never raw paths.

    Logs start/end (and any warnings) to logs/pipeline.jsonl. Raises on a hard
    Docling failure (caller decides how to surface it); table low-confidence is
    a warning, never an error.
    """
    path = Path(path)
    ext = path.suffix.lower()
    started = utc_now_iso()
    log_event({"stage": "ingest", "event": "start", "file": str(path), "ext": ext})

    if ext not in SUPPORTED_EXT:
        result = IngestResult(
            source_path=str(path),
            doc_type="",
            content_hash="",
            chunk_count=0,
            output_path="",
            skipped=True,
            reason=f"unsupported extension {ext}",
        )
        log_event({"stage": "ingest", "event": "skipped", "file": str(path),
                   "reason": result.reason})
        return result

    doc_type = "pptx" if ext == ".pptx" else ext.lstrip(".")  # pdf | docx
    content_hash = sha256_file(path)

    conv = _converter().convert(path)
    doc = conv.document
    warnings = _table_warnings(conv)

    frontmatter: dict = {
        "source_path": str(path),
        "doc_type": doc_type,
        "client": None,            # filled manually for now (see plan)
        "ingested_at": started,
        "content_hash": content_hash,
        "chunk_count": 0,          # set below
        **_source_frontmatter(path, source_entry),
    }

    if doc_type == "pptx":
        num_slides = doc.num_pages()
        chunks = _build_slide_chunks(doc, num_slides)
        frontmatter["slide_count"] = num_slides
    else:
        chunks, hierarchy = _build_prose_chunks(doc)
        frontmatter["heading_hierarchy"] = hierarchy

    frontmatter["chunk_count"] = len(chunks)

    if doc_type == "pdf":
        total_chars = sum(len(c.text) for c in chunks)
        pages = max(1, doc.num_pages())
        if total_chars < PDF_MIN_CHARS_PER_PAGE * pages:
            warnings.append(
                f"low text yield ({total_chars} chars over {pages} page(s)): PDF "
                "may be image-only or near-empty — extraction may be incomplete"
            )

    out_path = PROCESSED_DIR / f"{path.name}.md"  # full name (ext incl.) avoids
                                                   # foo.pdf / foo.docx collisions
    _write_markdown(out_path, frontmatter, chunks)

    for w in warnings:
        log_event({"stage": "ingest", "event": "warning", "file": str(path),
                   "warning": w})

    log_event({
        "stage": "ingest",
        "event": "end",
        "file": str(path),
        "doc_type": doc_type,
        "content_hash": content_hash,
        "chunk_count": len(chunks),
        "table_chunks": sum(1 for c in chunks if c.meta.get("table")),
        "image_chunks": sum(1 for c in chunks if c.meta.get("image")),
        "output": str(out_path),
        "token_counter": _token_counter_name(),
        "warning_count": len(warnings),
        "started": started,
        "ended": utc_now_iso(),
    })

    return IngestResult(
        source_path=str(path),
        doc_type=doc_type,
        content_hash=content_hash,
        chunk_count=len(chunks),
        output_path=str(out_path),
        warnings=warnings,
    )


def _is_ignorable(name: str) -> bool:
    """Office lock files (~$foo.docx), hidden/dotfiles, and macOS AppleDouble
    (._foo) are not real sources — skip them silently."""
    return name.startswith("~$") or name.startswith(".")


def _is_supported(p: Path) -> bool:
    return (
        p.is_file()
        and p.suffix.lower() in SUPPORTED_EXT
        and not _is_ignorable(p.name)
    )


def _iter_sources(args: list[str]) -> list[tuple[Path, dict]]:
    """Discover source files across all configured source roots.

    Returns a list of ``(path, source_entry)`` tuples, where ``source_entry`` is
    the sources.json dict (``name`` / ``path`` / ``recurse``) for that file's root.

    Explicit CLI paths bypass the config and each get a synthetic "CLI" entry so
    they still carry a location. Configured roots that don't exist are skipped
    with a logged warning (a OneDrive folder may be unsynced on this machine)
    rather than crashing.
    """
    if args:
        return [
            (Path(a), {"name": "CLI", "path": str(Path(a).parent), "recurse": False})
            for a in args
        ]

    out: list[tuple[Path, dict]] = []
    for entry in load_source_dirs():
        root = Path(entry["path"])
        if not root.exists():
            log_event({"stage": "ingest", "event": "warning",
                       "warning": f"source dir not found (skipped): {entry['name']} "
                                  f"-> {root}"})
            continue
        excludes = entry.get("exclude") or []
        walker = root.rglob("*") if entry.get("recurse") else root.iterdir()
        for p in sorted(walker):
            if _is_supported(p) and not any(fnmatch.fnmatch(p.name, pat) for pat in excludes):
                out.append((p, entry))
    return out


def main(argv: list[str]) -> int:
    files = _iter_sources(argv)
    if not files:
        print("No source files found. Add .pptx/.pdf/.docx files to a configured "
              "source dir (see sources.json / sources.example.json).")
        return 0

    print(f"Ingesting {len(files)} file(s)...\n")
    total_chunks = 0
    total_warnings = 0
    for f, entry in files:
        try:
            r = ingest_file(f, entry)
        except Exception as exc:  # hard Docling failure — log and keep going
            log_event({"stage": "ingest", "event": "error", "file": str(f),
                       "error": repr(exc), "traceback": traceback.format_exc()})
            print(f"  ✗ {f.name}: FAILED — {exc}")
            continue
        if r.skipped:
            print(f"  - {f.name}: skipped ({r.reason})")
            continue
        total_chunks += r.chunk_count
        total_warnings += len(r.warnings)
        flag = f"  ⚠ {len(r.warnings)} warning(s)" if r.warnings else ""
        print(f"  ✓ {f.name} [{r.doc_type}] -> {Path(r.output_path).name} "
              f"({r.chunk_count} chunks){flag}")
        for w in r.warnings:
            print(f"        ⚠ {w}")

    print(f"\nDone. {total_chunks} chunks across {len(files)} file(s), "
          f"{total_warnings} warning(s). Token counter: {_token_counter_name()}.")
    print(f"Output: {PROCESSED_DIR}/   Logs: logs/pipeline.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
