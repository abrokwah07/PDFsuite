"""One-click bank/HR workflow presets for PDF conversion."""

from __future__ import annotations

from typing import Any

# Static catalogue — UI and API share this source of truth.
PRESETS: list[dict[str, Any]] = [
    {
        "id": "salary-to-excel",
        "name": "Salary schedule → Excel",
        "description": "Extract employee salary / payment tables into a spreadsheet (OCR-aware).",
        "category": "Bank / HR",
        "accept": ".pdf,application/pdf",
        "action": "to-excel",
        "force_ocr": True,
        "icon": "table",
    },
    {
        "id": "scan-to-word",
        "name": "Scan → OCR → Word",
        "description": "Turn scanned letters into editable Word with detected tables.",
        "category": "Bank / HR",
        "accept": ".pdf,application/pdf",
        "action": "to-word",
        "mode": "fast",
        "force_ocr": True,
        "icon": "word",
    },
    {
        "id": "scan-searchable",
        "name": "Scan → Searchable PDF",
        "description": "Force a full OCR layer so text is selectable and searchable.",
        "category": "Scans",
        "accept": ".pdf,application/pdf",
        "action": "ocr",
        "force_ocr": True,
        "icon": "ocr",
    },
    {
        "id": "bank-pack",
        "name": "Bank pack → Word + Excel",
        "description": "One PDF becomes both a Word letter and an Excel table (zipped).",
        "category": "Bank / HR",
        "accept": ".pdf,application/pdf",
        "action": "word-and-excel",
        "mode": "fast",
        "force_ocr": True,
        "icon": "pack",
    },
    {
        "id": "batch-merge",
        "name": "Merge many PDFs",
        "description": "Combine multiple PDFs into one document (use Batch tool for folders).",
        "category": "Batch",
        "accept": ".pdf,application/pdf",
        "action": "merge",
        "force_ocr": False,
        "icon": "merge",
    },
]


def list_presets() -> list[dict[str, Any]]:
    return [dict(p) for p in PRESETS]


def get_preset(preset_id: str) -> dict[str, Any] | None:
    pid = (preset_id or "").strip().lower()
    for p in PRESETS:
        if p["id"] == pid:
            return dict(p)
    return None
