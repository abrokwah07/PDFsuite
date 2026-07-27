"""Helpers for large-PDF conversion: auto-chunk, convert pieces, merge to one file."""

from __future__ import annotations

import logging
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Callable, Iterable, Sequence

from fastapi import HTTPException
from pypdf import PdfReader, PdfWriter

from app.config import Settings
from app.security import parse_page_spec

logger = logging.getLogger("pdfsuite")


def count_pages(path: str | Path) -> int:
    """Return page count; 0 if unreadable."""
    try:
        return len(PdfReader(str(path)).pages)
    except Exception as exc:
        logger.warning("Could not count pages for %s: %s", path, exc)
        return 0


def chunked(items: Sequence[int], size: int) -> list[list[int]]:
    """Split a sequence into fixed-size chunks (last may be shorter)."""
    if size < 1:
        size = 1
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def resolve_pages(
    *,
    total_pages: int,
    pages: str | None,
    max_total: int,
    label: str = "conversion",
) -> list[int]:
    """
    Resolve which pages to convert.

    - Empty ``pages`` → entire document (up to max_total).
    - Explicit range → that subset (must not exceed max_total).
    """
    if max_total < 1:
        raise HTTPException(status_code=500, detail=f"Invalid max_total for {label}.")

    if total_pages < 0:
        total_pages = 0

    if pages and str(pages).strip():
        try:
            indices = sorted(parse_page_spec(str(pages).strip()))
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid page specification. Use formats like 1-50 or 1,3,5-7.",
            ) from exc
        if total_pages > 0:
            indices = [i for i in indices if 0 <= i < total_pages]
        if not indices:
            raise HTTPException(status_code=400, detail="No valid pages selected.")
        if len(indices) > max_total:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Too many pages selected ({len(indices)}). "
                    f"Maximum for {label} is {max_total} pages. "
                    f"Narrow the range or raise the limit in settings."
                ),
            )
        return indices

    # Full document
    if total_pages == 0:
        raise HTTPException(
            status_code=400,
            detail="Could not read PDF page count. Specify a page range explicitly.",
        )
    if total_pages < 1:
        raise HTTPException(status_code=400, detail="PDF has no pages.")

    if total_pages > max_total:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This PDF has {total_pages} pages; {label} is limited to "
                f"{max_total} pages per job for stability. "
                f"Convert a page range (e.g. 1-{max_total}) or raise "
                f"MAX_WORD_TOTAL_PAGES / MAX_EXCEL_TOTAL_PAGES."
            ),
        )
    return list(range(total_pages))


# Backward-compatible name used by older tests/code
def resolve_page_window(
    *,
    total_pages: int,
    pages: str | None,
    max_pages: int,
    label: str = "conversion",
) -> tuple[int, int, list[int]]:
    indices = resolve_pages(
        total_pages=total_pages,
        pages=pages,
        max_total=max_pages,
        label=label,
    )
    return indices[0], indices[-1] + 1, indices


def write_page_subset(
    input_path: str | Path,
    output_path: str | Path,
    indices: list[int],
) -> int:
    """Write selected pages to a new PDF. Returns number of pages written."""
    reader = PdfReader(str(input_path))
    writer = PdfWriter()
    total = len(reader.pages)
    written = 0
    for idx in indices:
        if 0 <= idx < total:
            writer.add_page(reader.pages[idx])
            written += 1
    if written == 0:
        raise HTTPException(status_code=400, detail="No valid pages to extract for conversion.")
    with open(output_path, "wb") as out:
        writer.write(out)
    return written


def pdf_to_docx_fast(
    input_path: str | Path,
    output_path: str | Path,
    indices: list[int],
    *,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> int:
    """
    Fast text extraction into a Word file via PyMuPDF + python-docx.
    Processes pages in order; suitable for full multi-thousand-page documents.
    """
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail="PyMuPDF is required for fast Word conversion.",
        ) from exc

    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail="python-docx is required for Word conversion.",
        ) from exc

    src = fitz.open(str(input_path))
    try:
        doc = Document()
        style = doc.styles["Normal"]
        style.font.name = "Calibri"
        style.font.size = Pt(11)

        written = 0
        total = len(indices)
        for n, idx in enumerate(indices, start=1):
            if should_cancel and should_cancel():
                raise RuntimeError("cancelled")
            if idx < 0 or idx >= src.page_count:
                continue
            page = src[idx]
            text = page.get_text("text") or ""
            if written > 0:
                doc.add_page_break()
            if text.strip():
                blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
                if not blocks:
                    doc.add_paragraph(text.strip())
                else:
                    for block in blocks:
                        para = " ".join(
                            line.strip() for line in block.splitlines() if line.strip()
                        )
                        if para:
                            doc.add_paragraph(para)
            else:
                doc.add_paragraph(f"[Page {idx + 1}: no extractable text]")
            written += 1
            if on_progress and (n == 1 or n == total or n % 5 == 0):
                on_progress(n, total)

        if written == 0:
            raise HTTPException(status_code=400, detail="No pages converted.")
        if on_progress:
            on_progress(total, total)
        doc.save(str(output_path))
        return written
    finally:
        src.close()


def merge_docx_files(paths: Sequence[str | Path], output_path: str | Path) -> None:
    """
    Merge multiple .docx files into one document (in order).
    Inserts a page break between chunks.
    """
    from docx import Document
    from docx.oxml.ns import qn

    if not paths:
        raise HTTPException(status_code=500, detail="No Word chunks to merge.")

    if len(paths) == 1:
        shutil.copy2(str(paths[0]), str(output_path))
        return

    master = Document(str(paths[0]))
    master_body = master.element.body

    for path in paths[1:]:
        # Page break between chunks
        master.add_page_break()
        part = Document(str(path))
        for child in list(part.element.body):
            # Do not copy the final section properties of the part
            if child.tag == qn("w:sectPr"):
                continue
            master_body.insert(-1, deepcopy(child))

    master.save(str(output_path))


def convert_word_layout_chunked(
    *,
    input_path: str | Path,
    output_path: str | Path,
    indices: list[int],
    chunk_size: int,
    converter_cls,
    make_temp: Callable[[str], str],
    cleanup: list[str],
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[int, int]:
    """
    Convert layout-preserving Word in page chunks, then merge into one .docx.
    Returns (pages_written, chunks_used).
    """
    if converter_cls is None:
        raise HTTPException(status_code=503, detail="pdf2docx is not installed.")

    chunks = chunked(indices, max(1, chunk_size))
    part_paths: list[str] = []
    pages_done = 0
    total_pages = len(indices)

    for n, chunk in enumerate(chunks, start=1):
        if should_cancel and should_cancel():
            raise RuntimeError("cancelled")
        subset = make_temp(".pdf")
        part_docx = make_temp(".docx")
        cleanup.extend([subset, part_docx])
        write_page_subset(input_path, subset, chunk)

        logger.info(
            "layout chunk %s/%s pages %s-%s",
            n,
            len(chunks),
            chunk[0] + 1,
            chunk[-1] + 1,
        )
        cv = converter_cls(subset)
        try:
            cv.convert(part_docx, start=0, end=None)
        finally:
            cv.close()
        part_paths.append(part_docx)
        pages_done += len(chunk)
        if on_progress:
            on_progress(pages_done, total_pages)

    if should_cancel and should_cancel():
        raise RuntimeError("cancelled")
    merge_docx_files(part_paths, output_path)
    return len(indices), len(chunks)


def pages_label(indices: list[int], total_pages: int, *, chunks: int = 0) -> str:
    """Human-friendly label for which pages were processed."""
    if not indices:
        return "none"
    if len(indices) == 1:
        label = f"page {indices[0] + 1}"
    else:
        continuous = indices == list(range(indices[0], indices[-1] + 1))
        if continuous:
            label = f"pages {indices[0] + 1}-{indices[-1] + 1}"
        else:
            label = f"{len(indices)} pages"

    if total_pages > 0 and len(indices) >= total_pages:
        label = f"all {total_pages} pages"
    elif total_pages > len(indices):
        label += f" of {total_pages}"

    if chunks > 1:
        label += f" · {chunks} auto-batches merged"
    return label


def extract_tables_pdfplumber(
    input_path: str | Path,
    indices: list[int],
    *,
    pd_module,
) -> list:
    """Extract tables from selected pages using pdfplumber."""
    import pdfplumber

    frames: list = []
    with pdfplumber.open(str(input_path)) as pdf:
        total = len(pdf.pages)
        for idx in indices:
            if idx < 0 or idx >= total:
                continue
            page = pdf.pages[idx]
            tables = page.extract_tables(
                {
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                }
            )
            for table in tables or []:
                if table:
                    frames.append(pd_module.DataFrame(table))
    return frames


def extract_tables_camelot(
    input_path: str | Path,
    indices: list[int],
    *,
    camelot_module,
) -> list:
    """Extract tables with Camelot on a compact page list (not 'all')."""
    if not indices:
        return []
    page_str = ",".join(str(i + 1) for i in indices)
    if len(indices) > 1 and indices == list(range(indices[0], indices[-1] + 1)):
        page_str = f"{indices[0] + 1}-{indices[-1] + 1}"

    frames: list = []
    try:
        tables = camelot_module.read_pdf(
            str(input_path),
            pages=page_str,
            flavor="stream",
        )
        for table in tables:
            if not table.df.empty:
                frames.append(table.df)
    except Exception as exc:
        logger.info("Camelot bounded extraction failed: %s", exc)
    return frames


def extract_tables_chunked(
    input_path: str | Path,
    indices: list[int],
    *,
    chunk_size: int,
    pd_module,
    camelot_module=None,
    enable_camelot: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list:
    """
    Extract tables in page chunks and concatenate results.
    Prefers pdfplumber; Camelot only on small chunks that found nothing.
    """
    all_frames: list = []
    chunks = chunked(indices, max(1, chunk_size))
    pages_done = 0
    total_pages = len(indices)
    for n, chunk in enumerate(chunks, start=1):
        if should_cancel and should_cancel():
            raise RuntimeError("cancelled")
        logger.info(
            "excel chunk %s/%s pages %s-%s",
            n,
            len(chunks),
            chunk[0] + 1,
            chunk[-1] + 1,
        )
        frames = extract_tables_pdfplumber(input_path, chunk, pd_module=pd_module)
        if (
            not frames
            and enable_camelot
            and camelot_module is not None
            and len(chunk) <= 30
        ):
            frames = extract_tables_camelot(
                input_path, chunk, camelot_module=camelot_module
            )
        all_frames.extend(frames)
        pages_done += len(chunk)
        if on_progress:
            on_progress(pages_done, total_pages)
    return all_frames
