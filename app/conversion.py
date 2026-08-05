"""Helpers for large-PDF conversion: auto-chunk, convert pieces, merge to one file.

Handles born-digital PDFs and scanned / image-heavy documents with:
- multi-strategy table extraction (pdfplumber → Camelot → text-row parser)
- automatic OCR rescue when pages lack usable text
- structured Word output (paragraphs + detected tables)
"""

from __future__ import annotations

import logging
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Sequence

from fastapi import HTTPException
from pypdf import PdfReader, PdfWriter

from app.security import parse_page_spec

logger = logging.getLogger("pdfsuite")

# ---------------------------------------------------------------------------
# Page selection / chunking
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# PDF text quality / OCR rescue
# ---------------------------------------------------------------------------


def analyze_pdf_text(
    input_path: str | Path,
    indices: list[int] | None = None,
) -> dict[str, Any]:
    """
    Assess whether a PDF has usable text or is image/scan-heavy.

    Returns keys: pages, avg_chars, empty_pages, image_pages, total_chars,
    needs_ocr, quality ('good'|'weak'|'empty').
    """
    try:
        import fitz
    except ImportError:  # pragma: no cover
        return {
            "pages": 0,
            "avg_chars": 0,
            "empty_pages": 0,
            "image_pages": 0,
            "total_chars": 0,
            "needs_ocr": False,
            "quality": "empty",
        }

    src = fitz.open(str(input_path))
    try:
        if indices is None:
            indices = list(range(src.page_count))
        empty = 0
        image_pages = 0
        total_chars = 0
        weak_markers = 0
        for idx in indices:
            if idx < 0 or idx >= src.page_count:
                continue
            page = src[idx]
            text = (page.get_text("text") or "").strip()
            total_chars += len(text)
            if len(text) < 40:
                empty += 1
            # Large image covering most of the page → likely scan
            try:
                images = page.get_images(full=True) or []
            except Exception:
                images = []
            if images and len(text) < 200:
                image_pages += 1
            # OCR garbage markers common on bank stamps / logos
            if text:
                junk_ratio = sum(1 for c in text if not (c.isalnum() or c.isspace() or c in ".,;:/-()&%'\"$")) / max(
                    len(text), 1
                )
                if junk_ratio > 0.12 or re.search(r"[|_]{2,}|\d_[A-Z]|[A-Z]{1}\s[A-Z]{1}\s[A-Z]{1}", text):
                    weak_markers += 1

        n = max(len(indices), 1)
        avg = total_chars / n
        empty_ratio = empty / n
        image_ratio = image_pages / n
        if empty_ratio >= 0.4 and avg < 100:
            # Mostly blank text layer — image-only scan
            quality = "empty"
            needs = True
        elif image_ratio >= 0.5 and avg < 180:
            # Image-heavy pages with thin text — force OCR for convert paths
            quality = "empty" if avg < 60 else "weak"
            needs = True
        elif (
            avg < 120
            or image_pages >= max(1, int(n * 0.4))
            or weak_markers >= max(1, int(n * 0.5))
        ):
            # Weak / mixed OCR layer (common on phone scans of bank letters).
            quality = "weak"
            # Re-OCR when image-backed and text is sparse or very noisy
            needs = (image_pages > 0 and avg < 150) or (
                weak_markers >= max(1, int(n * 0.6)) and avg < 200
            )
        else:
            quality = "good"
            needs = False

        return {
            "pages": len(indices),
            "avg_chars": avg,
            "empty_pages": empty,
            "image_pages": image_pages,
            "total_chars": total_chars,
            "needs_ocr": needs,
            "quality": quality,
        }
    finally:
        src.close()


def run_ocr(
    input_path: str | Path,
    output_path: str | Path,
    *,
    ocrmypdf_module: Any,
    force: bool = True,
    deskew: bool = True,
    language: str = "eng",
) -> None:
    """Run ocrmypdf with sensible defaults for scans."""
    kwargs: dict[str, Any] = {
        "progress_bar": False,
        "output_type": "pdf",
        "language": language,
        "deskew": deskew,
        "optimize": 0,
    }
    if force:
        kwargs["force_ocr"] = True
    else:
        kwargs["skip_text"] = True
    ocrmypdf_module.ocr(str(input_path), str(output_path), **kwargs)


def ensure_searchable_pdf(
    input_path: str | Path,
    indices: list[int],
    *,
    enable_ocr: bool,
    ocrmypdf_module: Any | None,
    make_temp: Callable[[str], str] | None,
    cleanup: list[str] | None = None,
    max_ocr_pages: int = 40,
    force_when_weak: bool = False,
) -> tuple[str, list[int], str]:
    """
    If the PDF needs OCR and OCR is available, OCR a page subset and return
    (path, local_indices, engine_note). Otherwise return the original path.

    ``local_indices`` is always 0..n-1 relative to the returned PDF when OCR ran.
    """
    analysis = analyze_pdf_text(input_path, indices)
    needs = analysis["needs_ocr"] or (force_when_weak and analysis["quality"] != "good")
    if not needs:
        return str(input_path), indices, "native"

    if not enable_ocr or ocrmypdf_module is None or make_temp is None:
        logger.info(
            "PDF looks scanned/weak (quality=%s) but OCR unavailable; using native text",
            analysis["quality"],
        )
        return str(input_path), indices, "native-no-ocr"

    if len(indices) > max_ocr_pages:
        logger.info(
            "Skipping auto-OCR: %s pages exceeds auto-OCR cap %s",
            len(indices),
            max_ocr_pages,
        )
        return str(input_path), indices, "native-ocr-capped"

    subset = make_temp(".pdf")
    ocr_path = make_temp(".pdf")
    if cleanup is not None:
        cleanup.extend([subset, ocr_path])
    write_page_subset(input_path, subset, indices)
    try:
        # Prefer force_ocr when text is empty/garbage so we replace bad layers
        force = analysis["quality"] in {"empty", "weak"}
        run_ocr(subset, ocr_path, ocrmypdf_module=ocrmypdf_module, force=force, deskew=True)
        logger.info(
            "Auto-OCR complete quality=%s pages=%s force=%s",
            analysis["quality"],
            len(indices),
            force,
        )
        return ocr_path, list(range(len(indices))), "ocr"
    except Exception as exc:
        logger.warning("Auto-OCR failed, falling back to native: %s", exc)
        return str(input_path), indices, "native-ocr-failed"


# ---------------------------------------------------------------------------
# Word conversion
# ---------------------------------------------------------------------------


def _page_text_blocks(page) -> list[str]:
    """Extract ordered text blocks from a PyMuPDF page."""
    # "blocks" preserves reading order better than joining all lines
    try:
        blocks = page.get_text("blocks") or []
        lines: list[str] = []
        for b in sorted(blocks, key=lambda x: (round(x[1], 1), round(x[0], 1))):
            if len(b) < 5:
                continue
            text = (b[4] or "").strip()
            if text:
                lines.append(text)
        if lines:
            return lines
    except Exception:
        pass
    text = page.get_text("text") or ""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _looks_like_table_header(line: str) -> bool:
    upper = line.upper()
    keywords = (
        "S/NO",
        "S/N",
        "NAME",
        "ACCOUNT",
        "AMOUNT",
        "BANK",
        "BRANCH",
        "TOTAL",
        "DESCRIPTION",
        "QTY",
        "QUANTITY",
        "PRICE",
        "DATE",
    )
    hits = sum(1 for k in keywords if k in upper)
    return hits >= 2


def _parse_tabular_lines(lines: list[str]) -> list[list[str]] | None:
    """
    Turn text lines into a rectangular table when possible.
    Returns None if the page does not look tabular.
    """
    if not lines:
        return None

    # Prefer structured salary / schedule rows
    structured = _parse_schedule_rows(lines)
    if structured and len(structured) >= 2:
        return structured

    # Multi-space / tab columns — require consistent width (avoids letter-page junk)
    multi: list[list[str]] = []
    for line in lines:
        if _looks_like_noise(line):
            continue
        parts = re.split(r"\s{2,}|\t+", line.strip())
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) >= 3:
            multi.append(parts)
    if len(multi) >= 4:
        # Prefer the most common column count (≥3)
        from collections import Counter

        widths = [len(r) for r in multi if len(r) >= 3]
        if not widths:
            return None
        common_w, common_n = Counter(widths).most_common(1)[0]
        if common_w >= 3 and common_n >= 4:
            norm = [
                (r + [""] * (common_w - len(r)))[:common_w]
                for r in multi
                if len(r) >= max(3, common_w - 1)
            ]
            if len(norm) >= 4:
                return norm

    return None


_NOISE_RE = re.compile(
    r"^(page\s+\d+|confidential|tel:?|email:?|web:?|www\.|http)",
    re.I,
)


def _looks_like_noise(line: str) -> bool:
    s = line.strip()
    if len(s) < 2:
        return True
    if _NOISE_RE.match(s):
        return True
    # Mostly punctuation / OCR garbage
    alnum = sum(c.isalnum() for c in s)
    if alnum < 2:
        return True
    return False


# Salary / bank schedule style rows:
#   1  KUMAH, GIDEON  9040009444933  STANBIC  AIRPORT CITY  GHS  2,000.00
#   1_|KUMAH, GIDEON 9040009444933 STANBIC AIRPORT CITY GHS 2,000.00
_SCHEDULE_ROW = re.compile(
    r"""
    ^\s*
    [|_.\s\[\(]*
    (?P<sno>\d{1,4})
    [|_.\s\]\):\-]*
    (?P<body>.+?)
    \s+
    (?P<amount>(?:GHS\s*)?[\d,]+\.\d{2})\s*[,.\s]*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Fallback when S/NO is garbled (OCR "g" for "9") but account + amount exist
_SCHEDULE_ROW_LOOSE = re.compile(
    r"""
    ^\s*
    [|_.\s\[\(]*(?P<sno>\d{1,4}|[oOgG])?[|_.\s\]\):\-]*
    (?P<body>.+?\b\d{9,16}\b.+)
    \s+
    (?P<amount>(?:GHS\s*)?[\d,]+\.\d{2})\s*[,.\s]*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_ACCOUNT_RE = re.compile(r"\b(\d{9,16})\b")
_BANK_HINTS = (
    # Longer multi-word names first (matched by length desc)
    "BANK OF AFRICA",
    "FIDELITY BANK",
    "FIRST BANK",
    "CAL BANK",
    "GT BANK",
    "STANBIC",
    "STABIC",
    "GCB",
    "ECOBANK",
    "FIDELITY",
    "CALBANK",
    "UBA",
    "ZENITH",
    "ACCESS",
    "ABSA",
    "BARCLAYS",
    "GTBANK",
    "FBN",
    "REPUBLIC",
    "PRUDENTIAL",
    "CONSOLIDATED",
    "AGRIC",
    "NIB",
    "UMB",
    "ADB",
)


def _ocr_digit_fix(token: str | None, fallback: str = "") -> str:
    if not token:
        return fallback
    return (
        token.replace("O", "0")
        .replace("o", "0")
        .replace("G", "9")
        .replace("g", "9")
        .replace("l", "1")
        .replace("I", "1")
    )


def _split_schedule_body(body: str, amount: str, sno: str) -> list[str]:
    """Split name / account / bank / branch from the middle of a schedule line."""
    amount = re.sub(r"(?i)GHS\s*", "", amount).strip().rstrip(",.")
    acct_m = _ACCOUNT_RE.search(body)
    if not acct_m:
        return [sno, body.strip(), "", "", "", amount]

    account = acct_m.group(1)
    before = body[: acct_m.start()].strip(" |_.,-")
    after = body[acct_m.end() :].strip()
    after = re.sub(r"(?i)\bGHS\b", " ", after)
    after = re.sub(r"\s+", " ", after).strip(" |_.")

    bank = ""
    branch = after
    upper_after = after.upper()
    for hint in sorted(_BANK_HINTS, key=len, reverse=True):
        pos = upper_after.find(hint)
        if pos != -1:
            bank = after[pos : pos + len(hint)].strip(" .,")
            if bank.upper() == "STABIC":
                bank = "STANBIC"
            branch = (after[:pos] + " " + after[pos + len(hint) :]).strip(" |.,")
            branch = re.sub(r"\s+", " ", branch).strip()
            break

    name = re.sub(r"^[|_\s.\[]+", "", before).strip()
    name = re.sub(r"\s+", " ", name)
    # Soft-fix common OCR name joins when all caps run together
    return [sno, name, account, bank, branch, amount]


def _parse_schedule_rows(lines: list[str]) -> list[list[str]] | None:
    """Parse employee/salary schedule style lines into columns."""
    header = ["S/NO", "NAME OF EMPLOYEE", "ACCOUNT NUMBER", "BANK", "BRANCH", "AMOUNT"]
    rows: list[list[str]] = [header]
    seen_accounts: set[str] = set()
    auto_sno = 0

    for raw in lines:
        line = raw.replace("\n", " ").strip()
        line = re.sub(r"\s+", " ", line)
        line = line.rstrip(" ,;|")
        if _looks_like_noise(line):
            continue
        if re.search(r"\bTOTAL\b", line, re.I) and re.search(r"[\d,]+\.\d{2}", line):
            amt_m = re.search(r"((?:GHS\s*)?[\d,]+\.\d{2})\s*[,.\s]*$", line, re.I)
            if amt_m:
                rows.append(
                    [
                        "",
                        "TOTAL",
                        "",
                        "",
                        "",
                        re.sub(r"(?i)GHS\s*", "", amt_m.group(1)).strip().rstrip(",."),
                    ]
                )
            continue

        m = _SCHEDULE_ROW.match(line) or _SCHEDULE_ROW_LOOSE.match(line)
        if not m:
            continue

        sno_raw = m.groupdict().get("sno") or ""
        sno = _ocr_digit_fix(sno_raw, "")
        if not sno.isdigit():
            auto_sno += 1
            sno = str(auto_sno)
        body = m.group("body").strip()
        amount = m.group("amount")
        row = _split_schedule_body(body, amount, sno)
        # Deduplicate by account number when OCR repeats a line
        acct = row[2]
        if acct and acct in seen_accounts:
            continue
        if acct:
            seen_accounts.add(acct)
        rows.append(row)

    if len(rows) < 3:  # header + at least 2 data rows
        return None
    return rows


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
    Preserves paragraphs, and embeds detected tables as real Word tables.
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
            if written > 0:
                doc.add_page_break()

            blocks = _page_text_blocks(page)
            # Flatten blocks into lines so multi-row OCR blocks parse correctly
            flat_lines: list[str] = []
            for b in blocks:
                flat_lines.extend(ln.strip() for ln in b.splitlines() if ln.strip())
            table_data = _parse_tabular_lines(flat_lines) if flat_lines else None

            if table_data and len(table_data) >= 2:
                # Emit non-table lines as paragraphs, then the table
                preamble = [
                    ln
                    for ln in flat_lines
                    if not _SCHEDULE_ROW.match(re.sub(r"\s+", " ", ln))
                    and not _looks_like_noise(ln)
                    and "TOTAL" not in ln.upper()
                ]
                for line in preamble[:16]:
                    if _looks_like_table_header(line) and len(line) < 80:
                        continue
                    if _ACCOUNT_RE.search(line) and re.search(r"[\d,]+\.\d{2}", line):
                        continue
                    doc.add_paragraph(line)

                table = doc.add_table(rows=len(table_data), cols=len(table_data[0]))
                table.style = "Table Grid"
                for r_i, row in enumerate(table_data):
                    for c_i, val in enumerate(row):
                        cell = table.rows[r_i].cells[c_i]
                        cell.text = str(val) if val is not None else ""
                        if r_i == 0:
                            for p in cell.paragraphs:
                                for run in p.runs:
                                    run.bold = True
            elif flat_lines:
                # Letter / prose: keep line breaks (don't squash whole page into one para)
                for text in flat_lines:
                    doc.add_paragraph(text)
            else:
                doc.add_paragraph(
                    f"[Page {idx + 1}: no extractable text — run OCR first for scanned pages]"
                )

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


# ---------------------------------------------------------------------------
# Excel / table extraction
# ---------------------------------------------------------------------------

_PLUMBER_STRATEGIES: list[dict[str, Any]] = [
    {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
    {"vertical_strategy": "lines_strict", "horizontal_strategy": "lines_strict"},
    {"vertical_strategy": "text", "horizontal_strategy": "text"},
    {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "snap_tolerance": 5,
        "join_tolerance": 5,
        "edge_min_length": 20,
    },
]


def _table_score(table: list[list[Any]]) -> float:
    """Higher is better. Filters out garbage single-column dumps."""
    if not table or not table[0]:
        return 0.0
    rows = len(table)
    cols = max(len(r) for r in table)
    if rows < 2 or cols < 2:
        return 0.0
    cells = 0
    filled = 0
    long_cells = 0
    short_cells = 0
    numericish = 0
    for row in table:
        for cell in row:
            cells += 1
            text = (str(cell).strip() if cell is not None else "")
            if text:
                filled += 1
                if len(text) > 80:
                    long_cells += 1
                if len(text) <= 2:
                    short_cells += 1
                if re.search(r"\d", text):
                    numericish += 1
    if cells == 0:
        return 0.0
    fill = filled / cells
    # Sparse OCR grid noise (logo fragments, stamps)
    if fill < 0.2:
        return 0.0
    # Penalize tables that are mostly one giant string per cell
    if long_cells / max(filled, 1) > 0.5 and cols <= 2:
        return 0.5
    # Penalize tables of tiny OCR shards
    if short_cells / max(filled, 1) > 0.45:
        return rows * 0.5
    score = rows * cols * fill + cols * 2 + (5 if cols >= 3 else 0)
    # Bonus for tables with account/amount style numbers
    if numericish >= rows:
        score += 15
    # Bonus when first row looks like a header
    header = " ".join(str(c or "") for c in table[0]).upper()
    if sum(1 for k in ("NAME", "ACCOUNT", "AMOUNT", "BANK", "S/NO", "TOTAL") if k in header) >= 2:
        score += 25
    return score


def _normalize_table(table: list[list[Any]]) -> list[list[str]]:
    width = max((len(r) for r in table), default=0)
    out: list[list[str]] = []
    for row in table:
        cells = [("" if c is None else str(c).strip()) for c in row]
        if len(cells) < width:
            cells.extend([""] * (width - len(cells)))
        out.append(cells[:width])
    return out


def extract_tables_pdfplumber(
    input_path: str | Path,
    indices: list[int],
    *,
    pd_module,
) -> list:
    """Extract tables from selected pages using multiple pdfplumber strategies."""
    import pdfplumber

    frames: list = []
    with pdfplumber.open(str(input_path)) as pdf:
        total = len(pdf.pages)
        for idx in indices:
            if idx < 0 or idx >= total:
                continue
            page = pdf.pages[idx]
            best: list[list[Any]] | None = None
            best_score = 0.0
            for settings in _PLUMBER_STRATEGIES:
                try:
                    tables = page.extract_tables(settings) or []
                except Exception:
                    continue
                for table in tables:
                    if not table:
                        continue
                    score = _table_score(table)
                    if score > best_score:
                        best_score = score
                        best = table
            if best is not None and best_score >= 4:
                frames.append(pd_module.DataFrame(_normalize_table(best)))
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
    for flavor in ("lattice", "stream"):
        try:
            tables = camelot_module.read_pdf(
                str(input_path),
                pages=page_str,
                flavor=flavor,
            )
            for table in tables:
                if table.df is not None and not table.df.empty:
                    # score via list form
                    as_list = table.df.values.tolist()
                    if _table_score(as_list) >= 4:
                        frames.append(table.df)
            if frames:
                break
        except Exception as exc:
            logger.info("Camelot %s extraction failed: %s", flavor, exc)
    return frames


def extract_tables_from_text(
    input_path: str | Path,
    indices: list[int],
    *,
    pd_module,
) -> list:
    """Parse page text into tables (schedule-aware + multi-space columns)."""
    try:
        import fitz
    except ImportError:
        return []

    frames: list = []
    src = fitz.open(str(input_path))
    try:
        for idx in indices:
            if idx < 0 or idx >= src.page_count:
                continue
            page = src[idx]
            blocks = _page_text_blocks(page)
            # Flatten multi-line blocks into lines
            lines: list[str] = []
            for b in blocks:
                lines.extend(ln for ln in b.splitlines() if ln.strip())
            table = _parse_tabular_lines(lines)
            if table and _table_score(table) >= 4:
                frames.append(pd_module.DataFrame(table))
    finally:
        src.close()
    return frames


def extract_text_lines_frame(
    input_path: str | Path,
    indices: list[int],
    *,
    pd_module,
) -> list:
    """Last-resort: dump non-empty text lines as a single-column sheet."""
    try:
        import fitz
    except ImportError:
        return []

    rows: list[list[str]] = [["Page", "Line", "Text"]]
    src = fitz.open(str(input_path))
    try:
        for idx in indices:
            if idx < 0 or idx >= src.page_count:
                continue
            text = page_text = src[idx].get_text("text") or ""
            line_no = 0
            for ln in text.splitlines():
                s = ln.strip()
                if not s:
                    continue
                line_no += 1
                rows.append([str(idx + 1), str(line_no), s])
            if line_no == 0 and not page_text.strip():
                rows.append([str(idx + 1), "1", "[no extractable text]"])
    finally:
        src.close()
    if len(rows) <= 1:
        return []
    return [pd_module.DataFrame(rows[1:], columns=rows[0])]


def _frame_score(df) -> float:
    """Score a dataframe the same way we score raw tables."""
    try:
        as_list = [list(df.columns)] + df.astype(str).values.tolist()
        # If columns are 0..n, don't count fake header
        if not _has_real_headers(df):
            as_list = df.astype(str).where(df.notna(), "").values.tolist()
        return _table_score(as_list)
    except Exception:
        return 0.0


def _pick_best_frames(candidates: list) -> list:
    """
    From a list of dataframes, keep the highest-scoring coherent set.
    Prefers one strong schedule-style table over many weak text dumps.
    """
    if not candidates:
        return []
    scored = [(_frame_score(df), df) for df in candidates]
    scored = [(s, df) for s, df in scored if s >= 4]
    if not scored:
        return []
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score = scored[0][0]
    # When one table is clearly best (e.g. salary schedule), drop weak runners-up
    if best_score >= 40:
        threshold = best_score * 0.75
    else:
        threshold = max(4.0, best_score * 0.6)
    chosen = [df for s, df in scored if s >= threshold]
    # Cap: avoid writing dozens of junk sheets
    return chosen[:5]


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
    include_text_fallback: bool = True,
) -> list:
    """
    Extract tables in page chunks with a quality cascade:

    1. pdfplumber (multiple strategies)
    2. Camelot lattice/stream (small chunks)
    3. Text-row parser (salary schedules, multi-space columns)
    4. Best-score selection (reject weak dumps when a strong table exists)
    5. Optional raw text-line dump so conversion never returns empty on readable PDFs
    """
    all_candidates: list = []
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
        candidates: list = []
        candidates.extend(
            extract_tables_pdfplumber(input_path, chunk, pd_module=pd_module)
        )

        if (
            enable_camelot
            and camelot_module is not None
            and len(chunk) <= 30
        ):
            candidates.extend(
                extract_tables_camelot(
                    input_path, chunk, camelot_module=camelot_module
                )
            )

        # Always try text-row parser — often wins on OCR'd salary schedules
        candidates.extend(
            extract_tables_from_text(input_path, chunk, pd_module=pd_module)
        )

        best = _pick_best_frames(candidates)
        all_candidates.extend(best)
        pages_done += len(chunk)
        if on_progress:
            on_progress(pages_done, total_pages)

    # Global re-pick so one strong table beats many weak letter-page dumps
    all_frames = _pick_best_frames(all_candidates)

    if not all_frames and include_text_fallback:
        logger.info("No structured tables; using text-line fallback sheet")
        all_frames = extract_text_lines_frame(
            input_path, indices, pd_module=pd_module
        )
    else:
        logger.info(
            "Selected %s table sheet(s) from extraction cascade",
            len(all_frames),
        )

    return all_frames


def preview_tables(
    input_path: str | Path,
    indices: list[int],
    *,
    pd_module,
    camelot_module=None,
    enable_camelot: bool = False,
    enable_ocr: bool = False,
    ocrmypdf_module=None,
    make_temp: Callable[[str], str] | None = None,
    max_ocr_pages: int = 40,
    max_rows: int = 80,
) -> dict[str, Any]:
    """
    Extract tables for UI preview (no download).
    Returns {quality, engine, tables: [{headers, rows, row_count, col_count}]}.
    """
    analysis = analyze_pdf_text(input_path, indices)
    cleanup: list[str] = []
    try:
        work_path, work_indices, ocr_note = ensure_searchable_pdf(
            input_path,
            indices,
            enable_ocr=enable_ocr
            and (analysis["needs_ocr"] or analysis["quality"] in {"empty", "weak"}),
            ocrmypdf_module=ocrmypdf_module,
            make_temp=make_temp,
            cleanup=cleanup,
            max_ocr_pages=max_ocr_pages,
            force_when_weak=analysis["needs_ocr"],
        )
        frames = extract_tables_chunked(
            work_path,
            work_indices,
            chunk_size=50,
            pd_module=pd_module,
            camelot_module=camelot_module,
            enable_camelot=enable_camelot,
            include_text_fallback=True,
        )
        tables_out: list[dict[str, Any]] = []
        for frame in frames[:5]:
            df = _maybe_promote_header(frame.copy())
            df = df.fillna("")
            headers = [str(c) for c in df.columns.tolist()]
            # If still RangeIndex-style ints, use generic names
            if all(isinstance(c, int) for c in df.columns):
                headers = [f"Col {i + 1}" for i in range(len(df.columns))]
            rows = []
            for _, row in df.head(max_rows).iterrows():
                rows.append([str(v) if v is not None else "" for v in row.tolist()])
            tables_out.append(
                {
                    "headers": headers,
                    "rows": rows,
                    "row_count": int(len(df)),
                    "col_count": int(len(headers)),
                    "truncated": bool(len(df) > max_rows),
                }
            )
        return {
            "quality": analysis["quality"],
            "needs_ocr": analysis["needs_ocr"],
            "engine": ocr_note,
            "pages": len(indices),
            "tables": tables_out,
            "table_count": len(tables_out),
        }
    finally:
        for p in cleanup:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass


def write_excel_workbook(
    frames: list,
    output_path: str | Path,
    *,
    pd_module,
    sheet_prefix: str = "Table",
) -> int:
    """
    Write one or more dataframes to an .xlsx file.
    Returns number of sheets written.
    """
    if not frames:
        raise HTTPException(status_code=400, detail="No tables to write.")

    # Single frame → Master_Data for backward compatibility
    if len(frames) == 1:
        df = frames[0].copy()
        df.columns = range(df.shape[1]) if not _has_real_headers(df) else df.columns
        # If first row looks like header and columns are 0..n, promote it
        df = _maybe_promote_header(df)
        df.dropna(how="all", inplace=True)
        df.to_excel(str(output_path), index=False, sheet_name="Master_Data")
        return 1

    with pd_module.ExcelWriter(str(output_path), engine="openpyxl") as writer:
        for i, frame in enumerate(frames, start=1):
            df = frame.copy()
            df = _maybe_promote_header(df)
            df.dropna(how="all", inplace=True)
            name = f"{sheet_prefix}{i}"
            if len(name) > 31:
                name = name[:31]
            df.to_excel(writer, index=False, sheet_name=name)
    return len(frames)


def _has_real_headers(df) -> bool:
    cols = list(df.columns)
    if not cols:
        return False
    # pandas default RangeIndex or 0..n
    if all(isinstance(c, int) for c in cols):
        return False
    if cols == list(range(len(cols))):
        return False
    return True


def _maybe_promote_header(df):
    """If first row looks like headers and columns are numeric, promote it."""
    if df.empty:
        return df
    if _has_real_headers(df):
        return df
    first = [str(v).strip() if v is not None else "" for v in df.iloc[0].tolist()]
    if not first or not any(first):
        return df
    # Header-ish: mostly letters, not pure numbers
    alphaish = sum(1 for c in first if c and re.search(r"[A-Za-z]", c))
    if alphaish >= max(1, len(first) // 2):
        new_cols = []
        seen: dict[str, int] = {}
        for c in first:
            base = c or "col"
            if base in seen:
                seen[base] += 1
                base = f"{base}_{seen[base]}"
            else:
                seen[base] = 0
            new_cols.append(base[:40])
        df = df.iloc[1:].copy()
        df.columns = new_cols
        df.reset_index(drop=True, inplace=True)
    else:
        df.columns = range(df.shape[1])
    return df


def convert_pdf_to_excel(
    input_path: str | Path,
    output_path: str | Path,
    indices: list[int],
    *,
    pd_module,
    chunk_size: int = 50,
    camelot_module=None,
    enable_camelot: bool = False,
    enable_ocr: bool = False,
    ocrmypdf_module=None,
    make_temp: Callable[[str], str] | None = None,
    max_ocr_pages: int = 40,
    force_ocr: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    Full PDF → Excel pipeline with OCR rescue and multi-strategy extraction.
    Returns metadata: engine, tables, pages.
    """
    cleanup: list[str] = []
    try:
        analysis0 = analyze_pdf_text(input_path, indices)
        work_path, work_indices, ocr_note = ensure_searchable_pdf(
            input_path,
            indices,
            enable_ocr=enable_ocr
            and (
                force_ocr
                or analysis0["needs_ocr"]
                or analysis0["quality"] in {"empty", "weak"}
            ),
            ocrmypdf_module=ocrmypdf_module,
            make_temp=make_temp,
            cleanup=cleanup,
            max_ocr_pages=max_ocr_pages,
            force_when_weak=force_ocr or analysis0["needs_ocr"],
        )

        frames = extract_tables_chunked(
            work_path,
            work_indices,
            chunk_size=chunk_size,
            pd_module=pd_module,
            camelot_module=camelot_module,
            enable_camelot=enable_camelot,
            on_progress=on_progress,
            should_cancel=should_cancel,
            include_text_fallback=True,
        )

        # If still weak (only text dump) and OCR available but not used, try force OCR once
        only_text_dump = False
        if len(frames) == 1:
            cols = list(frames[0].columns)
            if list(cols[:2]) == ["Page", "Line"] or (
                frames[0].shape[1] <= 2 and frames[0].shape[0] < 5
            ):
                only_text_dump = True
        if (
            only_text_dump
            and enable_ocr
            and ocrmypdf_module is not None
            and make_temp is not None
            and ocr_note == "native"
            and len(indices) <= max_ocr_pages
        ):
            logger.info("Retrying Excel extraction with forced OCR")
            work_path, work_indices, ocr_note = ensure_searchable_pdf(
                input_path,
                indices,
                enable_ocr=True,
                ocrmypdf_module=ocrmypdf_module,
                make_temp=make_temp,
                cleanup=cleanup,
                max_ocr_pages=max_ocr_pages,
                force_when_weak=True,
            )
            frames = extract_tables_chunked(
                work_path,
                work_indices,
                chunk_size=chunk_size,
                pd_module=pd_module,
                camelot_module=camelot_module,
                enable_camelot=enable_camelot,
                include_text_fallback=True,
            )

        if not frames:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No readable tables or text found. "
                    "Try running OCR first, or pick pages that contain tables."
                ),
            )

        n_sheets = write_excel_workbook(frames, output_path, pd_module=pd_module)
        return {
            "pages": len(indices),
            "tables": len(frames),
            "sheets": n_sheets,
            "engine": ocr_note,
        }
    finally:
        for p in cleanup:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass


def convert_pdf_to_word(
    input_path: str | Path,
    output_path: str | Path,
    indices: list[int],
    *,
    mode: str = "auto",
    layout_threshold: int = 40,
    chunk_size: int = 40,
    converter_cls=None,
    enable_ocr: bool = False,
    ocrmypdf_module=None,
    make_temp: Callable[[str], str] | None = None,
    max_ocr_pages: int = 40,
    force_ocr: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    Full PDF → Word pipeline.
    - auto: layout for short born-digital docs; fast (+OCR) for scans/long docs
    - layout: pdf2docx layout preserve
    - fast: text extraction with table detection
    """
    mode_norm = (mode or "auto").strip().lower()
    if mode_norm not in {"auto", "layout", "fast"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid mode. Use auto, layout, or fast.",
        )

    cleanup: list[str] = []
    try:
        analysis = analyze_pdf_text(input_path, indices)
        # Prefer fast text for scans; layout often just embeds the page image
        prefer_fast = analysis["quality"] in {"empty", "weak"} or analysis["needs_ocr"]

        use_layout = mode_norm == "layout" or (
            mode_norm == "auto"
            and not prefer_fast
            and len(indices) <= layout_threshold
            and converter_cls is not None
        )
        if use_layout and converter_cls is None:
            use_layout = False

        # Auto-OCR for empty / image-only scans; presets can force_ocr.
        work_path, work_indices, ocr_note = ensure_searchable_pdf(
            input_path,
            indices,
            enable_ocr=enable_ocr
            and (
                force_ocr
                or analysis["needs_ocr"]
                or analysis["quality"] == "empty"
            ),
            ocrmypdf_module=ocrmypdf_module,
            make_temp=make_temp,
            cleanup=cleanup,
            max_ocr_pages=max_ocr_pages,
            force_when_weak=force_ocr or analysis["needs_ocr"],
        )

        chunks_used = 1
        if use_layout:
            if make_temp is None:
                raise HTTPException(status_code=500, detail="make_temp required for layout mode")
            local_cleanup: list[str] = []
            try:
                _pages, chunks_used = convert_word_layout_chunked(
                    input_path=work_path,
                    output_path=output_path,
                    indices=work_indices,
                    chunk_size=chunk_size,
                    converter_cls=converter_cls,
                    make_temp=make_temp,
                    cleanup=local_cleanup,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
            finally:
                for p in local_cleanup:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass
            engine = f"layout+{ocr_note}"
        else:
            pdf_to_docx_fast(
                work_path,
                output_path,
                work_indices,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
            engine = f"fast+{ocr_note}"

        return {
            "pages": len(indices),
            "engine": engine,
            "chunks": chunks_used,
            "quality": analysis["quality"],
        }
    finally:
        for p in cleanup:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
