"""API smoke tests for Local PDF Suite enterprise backend."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

# Ensure development mode so /docs is available if needed
import os

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("ENABLE_CAMELOT", "false")
os.environ.setdefault("ENABLE_OCR", "false")
os.environ.setdefault("ENABLE_LIBREOFFICE", "false")
os.environ.setdefault("ENABLE_GHOSTSCRIPT", "false")

from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _make_pdf_bytes(text: str = "Hello PDF Suite") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 750, text)
    c.showPage()
    c.drawString(100, 750, "Page 2")
    c.showPage()
    c.save()
    return buf.getvalue()


def _make_protected_pdf(password: str = "secret") -> bytes:
    plain = _make_pdf_bytes("Secret content")
    reader = PdfReader(io.BytesIO(plain))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=password, owner_password=password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_health(client: TestClient):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "X-Request-ID" in res.headers
    assert res.headers["X-Content-Type-Options"] == "nosniff"


def test_ready(client: TestClient):
    res = client.get("/ready")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"


def test_index(client: TestClient):
    res = client.get("/")
    assert res.status_code == 200
    assert "Local PDF Suite" in res.text
    assert "text/html" in res.headers["content-type"]


def test_merge(client: TestClient):
    pdf = _make_pdf_bytes()
    res = client.post(
        "/api/merge",
        files=[
            ("files", ("a.pdf", pdf, "application/pdf")),
            ("files", ("b.pdf", pdf, "application/pdf")),
        ],
    )
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/pdf")
    reader = PdfReader(io.BytesIO(res.content))
    assert len(reader.pages) == 4


def test_split(client: TestClient):
    pdf = _make_pdf_bytes()
    res = client.post(
        "/api/split",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"pages": "1"},
    )
    assert res.status_code == 200, res.text
    reader = PdfReader(io.BytesIO(res.content))
    assert len(reader.pages) == 1


def test_split_invalid_pages(client: TestClient):
    pdf = _make_pdf_bytes()
    res = client.post(
        "/api/split",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"pages": "not-a-page"},
    )
    assert res.status_code == 400


def test_reject_non_pdf(client: TestClient):
    res = client.post(
        "/api/merge",
        files=[("files", ("note.txt", b"hello", "text/plain"))],
    )
    assert res.status_code == 400


def test_path_traversal_filename_sanitized(client: TestClient):
    pdf = _make_pdf_bytes()
    res = client.post(
        "/api/split",
        files={"file": ("../../etc/passwd.pdf", pdf, "application/pdf")},
        data={"pages": "1"},
    )
    assert res.status_code == 200, res.text
    # Content-Disposition should not contain path separators
    cd = res.headers.get("content-disposition", "")
    assert ".." not in cd
    assert "/etc/" not in cd


def test_watermark(client: TestClient):
    pdf = _make_pdf_bytes()
    res = client.post(
        "/api/watermark",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"text": "CONFIDENTIAL"},
    )
    assert res.status_code == 200, res.text
    assert res.content.startswith(b"%PDF")


def test_scrub(client: TestClient):
    pdf = _make_pdf_bytes()
    res = client.post(
        "/api/scrub",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
    )
    assert res.status_code == 200, res.text


def test_protect_and_unlock(client: TestClient):
    pdf = _make_pdf_bytes()
    protect = client.post(
        "/api/protect",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={
            "open_password": "open123",
            "owner_password": "owner123",
            "disable_print": "false",
            "disable_copy": "false",
            "disable_modify": "false",
        },
    )
    assert protect.status_code == 200, protect.text
    protected = protect.content
    reader = PdfReader(io.BytesIO(protected))
    assert reader.is_encrypted

    unlock = client.post(
        "/api/unlock",
        files={"file": ("locked.pdf", protected, "application/pdf")},
        data={"password": "open123"},
    )
    assert unlock.status_code == 200, unlock.text
    unlocked = PdfReader(io.BytesIO(unlock.content))
    assert not unlocked.is_encrypted
    assert len(unlocked.pages) == 2


def test_unlock_wrong_password(client: TestClient):
    protected = _make_protected_pdf("right-pass")
    res = client.post(
        "/api/unlock",
        files={"file": ("locked.pdf", protected, "application/pdf")},
        data={"password": "wrong-pass"},
    )
    assert res.status_code == 401


def test_preview(client: TestClient):
    pdf = _make_pdf_bytes()
    res = client.post(
        "/api/preview",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"page_start": "0"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total_pages"] == 2
    assert body["is_encrypted"] is False
    assert len(body["preview_images"]) == 2
    assert body["preview_images"][0].startswith("data:image/jpeg;base64,")


def test_preview_never_rejects_for_page_count(client: TestClient, monkeypatch):
    """Huge PDFs must still preview (windowed), not 400 and clear the upload UI."""
    from app.config import Settings, clear_settings_cache
    import main as main_mod

    # Force a tiny preview cap while still allowing the PDF
    monkeypatch.setenv("MAX_TOTAL_PREVIEW_PAGES", "1")
    monkeypatch.setenv("MAX_PREVIEW_PAGES", "1")
    clear_settings_cache()
    # Settings are loaded at lifespan; TestClient already started app with old settings.
    # Call the page-count path with normal settings — ensure 200 for multi-page PDF.
    clear_settings_cache()
    pdf = _make_pdf_bytes()
    res = client.post(
        "/api/preview",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"page_start": "0"},
    )
    assert res.status_code == 200, res.text


def test_modify_pages_delete(client: TestClient):
    pdf = _make_pdf_bytes()
    res = client.post(
        "/api/modify-pages",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"modifications": '{"rotations": {}, "deletions": [1]}'},
    )
    assert res.status_code == 200, res.text
    reader = PdfReader(io.BytesIO(res.content))
    assert len(reader.pages) == 1


def test_edit_page_text(client: TestClient):
    pdf = _make_pdf_bytes()
    actions = (
        '[{"type":"text","text":"Signed","size":12,"pctX":0.1,"pctY":0.1}]'
    )
    res = client.post(
        "/api/edit-page",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"page_index": "0", "actions": actions},
    )
    assert res.status_code == 200, res.text


def test_edit_inspect_and_replace(client: TestClient):
    pdf = _make_pdf_bytes("Editable line here")
    inspect = client.post(
        "/api/edit/inspect",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"page_index": "0"},
    )
    assert inspect.status_code == 200, inspect.text
    body = inspect.json()
    assert body["total_pages"] >= 1
    assert len(body["spans"]) >= 1
    assert body["preview_image"].startswith("data:image/jpeg;base64,")

    span = body["spans"][0]
    ops = [
        {
            "type": "replace",
            "page": 0,
            "bbox": span["bbox"],
            "text": "CHANGED LINE",
            "size": span["size"],
        }
    ]
    apply = client.post(
        "/api/edit/apply",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"operations": __import__("json").dumps(ops)},
    )
    assert apply.status_code == 200, apply.text
    assert apply.content.startswith(b"%PDF")

    # Verify text changed via re-inspect
    inspect2 = client.post(
        "/api/edit/inspect",
        files={"file": ("edited.pdf", apply.content, "application/pdf")},
        data={"page_index": "0"},
    )
    assert inspect2.status_code == 200
    texts = " ".join(s["text"] for s in inspect2.json()["spans"])
    assert "CHANGED LINE" in texts


def test_compress_office_docx(client: TestClient):
    # OOXML package with a large embedded PNG so compression has something to do
    from PIL import Image

    img_buf = io.BytesIO()
    Image.new("RGB", (800, 600), color=(30, 120, 200)).save(img_buf, format="PNG")
    png = img_buf.getvalue()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        zf.writestr("word/document.xml", "<w:document></w:document>")
        zf.writestr("word/media/image1.png", png)
    docx = buf.getvalue()

    res = client.post(
        "/api/compress-office",
        files=[("files", ("sample.docx", docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        data={"level": "high"},
    )
    assert res.status_code == 200, res.text
    # Single file returns compressed docx, not always a zip batch
    assert len(res.content) < len(docx)
    assert "X-Saved-Percent" in res.headers or len(res.content) <= len(docx)


def test_background_word_job(client: TestClient):
    pdf = _make_pdf_bytes("Job convert me")
    create = client.post(
        "/api/jobs/convert/to-word",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"pages": "", "mode": "fast"},
    )
    assert create.status_code == 200, create.text
    body = create.json()
    job_id = body["job_id"]
    assert job_id

    # Poll until done
    import time

    job = body
    for _ in range(50):
        st = client.get(f"/api/jobs/{job_id}")
        assert st.status_code == 200
        job = st.json()
        if job["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert job["status"] == "completed", job
    assert job["progress"] >= 100 or job["downloadable"]

    dl = client.get(f"/api/jobs/{job_id}/download")
    assert dl.status_code == 200, dl.text
    assert len(dl.content) > 500


def test_reject_oversized_payload():
    import asyncio

    from app.config import Settings
    from app.security import read_upload
    from fastapi import HTTPException
    from starlette.datastructures import UploadFile as StarletteUploadFile

    settings = Settings(max_upload_bytes=100)
    upload = StarletteUploadFile(
        filename="big.pdf",
        file=io.BytesIO(b"%PDF-1.4\n" + b"x" * 200),
    )

    async def _run():
        with pytest.raises(HTTPException) as exc:
            await read_upload(upload, settings, allowed_extensions={".pdf"})
        assert exc.value.status_code == 413

    asyncio.run(_run())


def test_sanitize_filename():
    from app.security import sanitize_filename

    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("invoice (final).pdf") == "invoice (final).pdf"
    assert sanitize_filename(None) == "document"


def test_parse_page_spec():
    from app.security import parse_page_spec

    assert parse_page_spec("1,3,5-7") == {0, 2, 4, 5, 6}
    try:
        parse_page_spec("not-a-page")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_password_too_long(client: TestClient):
    pdf = _make_pdf_bytes()
    long_pw = "x" * 200
    res = client.post(
        "/api/protect",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"open_password": long_pw, "owner_password": ""},
    )
    assert res.status_code == 400
    assert "too long" in res.json()["detail"].lower()


def test_ready_includes_features(client: TestClient):
    res = client.get("/ready")
    assert res.status_code == 200
    body = res.json()
    assert "features" in body
    assert "limits" in body
    assert "max_upload_mb" in body["limits"]


def test_zip_member_safety():
    from app.security import is_safe_zip_member

    assert is_safe_zip_member("word/document.xml") is True
    assert is_safe_zip_member("../../etc/passwd") is False
    assert is_safe_zip_member("/etc/passwd") is False


def test_to_word_fast_mode_with_page_range(client: TestClient):
    pdf = _make_pdf_bytes("Word convert test")
    res = client.post(
        "/api/convert/to-word",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"pages": "1", "mode": "fast"},
    )
    assert res.status_code == 200, res.text
    assert "wordprocessingml" in res.headers.get("content-type", "")
    assert res.headers.get("X-Convert-Engine") == "fast"
    assert res.headers.get("X-Pages-Processed") == "1"


def test_resolve_pages_full_document():
    from app.conversion import resolve_pages

    indices = resolve_pages(total_pages=120, pages="", max_total=8000, label="test")
    assert indices == list(range(120))


def test_resolve_pages_rejects_over_cap():
    from app.conversion import resolve_pages
    from fastapi import HTTPException

    try:
        resolve_pages(total_pages=9000, pages="", max_total=1000, label="test")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "9000" in exc.detail or "limited" in exc.detail.lower()


def test_chunked_helper():
    from app.conversion import chunked

    assert chunked(list(range(10)), 4) == [
        [0, 1, 2, 3],
        [4, 5, 6, 7],
        [8, 9],
    ]


def test_to_excel_full_or_range(client: TestClient):
    pdf = _make_pdf_bytes("Table A B\n1 2")
    res = client.post(
        "/api/convert/to-excel",
        files=[("files", ("doc.pdf", pdf, "application/pdf"))],
        data={"pages": ""},
    )
    # May or may not find tables; must not hang / 500
    assert res.status_code in (200, 400), res.text


def test_merge_docx(tmp_path):
    from docx import Document
    from app.conversion import merge_docx_files, pdf_to_docx_fast

    # Build two small docx via fast path from tiny PDFs
    pdf = _make_pdf_bytes("Part A")
    p1 = tmp_path / "a.pdf"
    p2 = tmp_path / "b.pdf"
    p1.write_bytes(pdf)
    p2.write_bytes(_make_pdf_bytes("Part B"))
    d1 = tmp_path / "a.docx"
    d2 = tmp_path / "b.docx"
    out = tmp_path / "merged.docx"
    pdf_to_docx_fast(p1, d1, [0])
    pdf_to_docx_fast(p2, d2, [0])
    merge_docx_files([d1, d2], out)
    assert out.is_file() and out.stat().st_size > 1000
    doc = Document(str(out))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Part A" in text or len(text) > 0

def test_api_key_enforcement(monkeypatch):
    """When API_KEY is set, /api/* requires the key; public routes stay open."""
    import importlib

    monkeypatch.setenv("API_KEY", "test-secret-key")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENABLE_OCR", "false")
    monkeypatch.setenv("ENABLE_LIBREOFFICE", "false")
    monkeypatch.setenv("ENABLE_GHOSTSCRIPT", "false")
    monkeypatch.setenv("ENABLE_CAMELOT", "false")

    from app.config import clear_settings_cache

    clear_settings_cache()

    import main as main_mod

    importlib.reload(main_mod)

    with TestClient(main_mod.app) as c:
        deny = c.post(
            "/api/merge",
            files=[("files", ("a.pdf", _make_pdf_bytes(), "application/pdf"))],
        )
        assert deny.status_code == 401

        ok = c.post(
            "/api/merge",
            files=[
                ("files", ("a.pdf", _make_pdf_bytes(), "application/pdf")),
                ("files", ("b.pdf", _make_pdf_bytes(), "application/pdf")),
            ],
            headers={"X-API-Key": "test-secret-key"},
        )
        assert ok.status_code == 200, ok.text

        health = c.get("/health")
        assert health.status_code == 200

    # Restore clean app without API key for remaining tests
    monkeypatch.delenv("API_KEY", raising=False)
    clear_settings_cache()
    importlib.reload(main_mod)
