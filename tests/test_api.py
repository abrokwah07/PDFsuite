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
    # Minimal valid OOXML package
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>',
        )
        zf.writestr("word/document.xml", "<w:document></w:document>")
    docx = buf.getvalue()

    res = client.post(
        "/api/compress-office",
        files=[("files", ("sample.docx", docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        data={"level": "medium"},
    )
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/zip")


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
