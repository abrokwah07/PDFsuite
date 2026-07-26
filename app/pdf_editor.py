"""
PyMuPDF-based PDF content editor.

This is not Adobe Acrobat (full layout reflow / font matching), but it does real
content edits: text extraction with positions, permanent redaction, replace
text, and insert free text — not just a transparent overlay stamp.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore


def require_fitz() -> None:
    if fitz is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail="PyMuPDF is not installed. Run: pip install pymupdf",
        )


def _norm_rect(rect: "fitz.Rect", page_w: float, page_h: float) -> dict[str, float]:
    return {
        "x0": max(0.0, min(1.0, rect.x0 / page_w)),
        "y0": max(0.0, min(1.0, rect.y0 / page_h)),
        "x1": max(0.0, min(1.0, rect.x1 / page_w)),
        "y1": max(0.0, min(1.0, rect.y1 / page_h)),
    }


def _denorm_rect(box: dict[str, Any], page_w: float, page_h: float) -> "fitz.Rect":
    return fitz.Rect(
        float(box["x0"]) * page_w,
        float(box["y0"]) * page_h,
        float(box["x1"]) * page_w,
        float(box["y1"]) * page_h,
    )


def _rgb_from_int(color_int: int | None) -> tuple[float, float, float]:
    if color_int is None:
        return (0.0, 0.0, 0.0)
    r = ((color_int >> 16) & 255) / 255.0
    g = ((color_int >> 8) & 255) / 255.0
    b = (color_int & 255) / 255.0
    return (r, g, b)


def inspect_page(pdf_bytes: bytes, page_index: int = 0, dpi: int = 144) -> dict[str, Any]:
    """Return page image + selectable text spans for the editor UI."""
    require_fitz()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if page_index < 0 or page_index >= doc.page_count:
            raise ValueError(f"page_index out of range (0..{doc.page_count - 1})")

        page = doc[page_index]
        page_w = float(page.rect.width)
        page_h = float(page.rect.height)

        # Raster preview
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_bytes = pix.tobytes("jpeg")
        preview = "data:image/jpeg;base64," + base64.b64encode(img_bytes).decode("ascii")

        # Extract text spans (line-level for easier editing)
        spans_out: list[dict[str, Any]] = []
        span_id = 0
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in text_dict.get("blocks", []):
            if block.get("type", 0) != 0:  # 0 = text
                continue
            for line in block.get("lines", []):
                line_spans = line.get("spans", [])
                if not line_spans:
                    continue
                # Merge line into one editable unit when spans share similar size
                texts = []
                rect = fitz.Rect(line["bbox"])
                size = line_spans[0].get("size", 11)
                font = line_spans[0].get("font", "helv")
                color = line_spans[0].get("color", 0)
                for sp in line_spans:
                    t = sp.get("text", "")
                    if t:
                        texts.append(t)
                text = "".join(texts).rstrip()
                if not text.strip():
                    continue
                spans_out.append(
                    {
                        "id": span_id,
                        "text": text,
                        "size": round(float(size), 1),
                        "font": font,
                        "color": _rgb_from_int(color if isinstance(color, int) else 0),
                        "bbox": _norm_rect(rect, page_w, page_h),
                    }
                )
                span_id += 1

        return {
            "page_index": page_index,
            "total_pages": doc.page_count,
            "page_width": page_w,
            "page_height": page_h,
            "preview_image": preview,
            "spans": spans_out,
            "engine": "pymupdf",
        }
    finally:
        doc.close()


def apply_edits(pdf_bytes: bytes, operations: list[dict[str, Any]]) -> bytes:
    """
    Apply a list of edit operations and return a new PDF.

    Supported operation types:
      - replace: {page, bbox, text, size?, color?}  — redact original, write new text
      - redact:  {page, bbox}                       — permanent content removal
      - add_text:{page, x, y, text, size?, color?}  — free text (x/y are top-left 0..1)
      - whiteout:{page, bbox}                       — visual white box only (no content delete)
    """
    require_fitz()
    if len(operations) > 500:
        raise ValueError("Too many operations (max 500)")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        # Group ops that need redaction first (must apply_redactions before insert on same page)
        # Strategy per page: collect redacts, apply, then inserts
        by_page: dict[int, list[dict[str, Any]]] = {}
        for op in operations:
            if not isinstance(op, dict) or "type" not in op:
                continue
            page_i = int(op.get("page", 0))
            by_page.setdefault(page_i, []).append(op)

        for page_i, ops in by_page.items():
            if page_i < 0 or page_i >= doc.page_count:
                raise ValueError(f"Invalid page index {page_i}")
            page = doc[page_i]
            page_w = float(page.rect.width)
            page_h = float(page.rect.height)

            pending_inserts: list[dict[str, Any]] = []
            has_redact = False

            for op in ops:
                typ = op["type"]
                if typ in {"replace", "redact"}:
                    bbox = op.get("bbox")
                    if not bbox:
                        continue
                    rect = _denorm_rect(bbox, page_w, page_h)
                    # Slight padding so glyphs fully covered
                    rect = rect + (-0.5, -0.5, 0.5, 0.5)
                    page.add_redact_annot(rect, fill=(1, 1, 1))
                    has_redact = True
                    if typ == "replace":
                        pending_inserts.append(
                            {
                                "kind": "replace",
                                "bbox": bbox,
                                "text": str(op.get("text", ""))[:2000],
                                "size": float(op.get("size") or max(8.0, (float(bbox["y1"]) - float(bbox["y0"])) * page_h * 0.75)),
                                "color": op.get("color") or [0, 0, 0],
                            }
                        )
                elif typ == "whiteout":
                    bbox = op.get("bbox")
                    if not bbox:
                        continue
                    rect = _denorm_rect(bbox, page_w, page_h)
                    shape = page.new_shape()
                    shape.draw_rect(rect)
                    shape.finish(color=(1, 1, 1), fill=(1, 1, 1), width=0)
                    shape.commit()
                elif typ == "add_text":
                    pending_inserts.append(
                        {
                            "kind": "add",
                            "x": float(op.get("x", 0)),
                            "y": float(op.get("y", 0)),
                            "text": str(op.get("text", ""))[:2000],
                            "size": float(op.get("size") or 12),
                            "color": op.get("color") or [0, 0, 0],
                        }
                    )

            if has_redact:
                # images=0 keeps images; text/graphics under redaction removed
                page.apply_redactions(images=0)

            for ins in pending_inserts:
                color = ins["color"]
                if isinstance(color, (list, tuple)) and len(color) >= 3:
                    rgb = (float(color[0]), float(color[1]), float(color[2]))
                else:
                    rgb = (0.0, 0.0, 0.0)
                size = max(6.0, min(96.0, float(ins["size"])))
                text = ins["text"]
                if not text:
                    continue

                if ins["kind"] == "replace":
                    bbox = ins["bbox"]
                    # Baseline near bottom of original box
                    x = float(bbox["x0"]) * page_w
                    y = float(bbox["y1"]) * page_h - size * 0.2
                    page.insert_text(
                        (x, y),
                        text,
                        fontsize=size,
                        fontname="helv",
                        color=rgb,
                    )
                else:
                    x = float(ins["x"]) * page_w
                    # y is top-relative in UI; insert_text wants baseline
                    y = float(ins["y"]) * page_h + size
                    page.insert_text(
                        (x, y),
                        text,
                        fontsize=size,
                        fontname="helv",
                        color=rgb,
                    )

        out = io.BytesIO()
        doc.save(out, garbage=3, deflate=True, clean=True)
        return out.getvalue()
    finally:
        doc.close()
