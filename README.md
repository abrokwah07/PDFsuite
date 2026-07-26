# Local PDF Suite

Private, on-prem PDF toolkit. Documents are processed **on your machine / server** — nothing is sent to a third-party cloud.

**Version:** 1.1.0

## Features

| Category | Tools |
|----------|--------|
| Conversion | PDF → Excel, PDF → Word, Office → PDF |
| Organization | Merge, extract pages, rotate/delete, compress PDF |
| Security | Protect, unlock, watermark, scrub metadata, OCR |
| Editing | Visual stamper (text + whiteout), Office compress |

## Quick start (local)

### Requirements

- Python 3.11+ (3.12 recommended)
- Optional system tools:
  - **Ghostscript** (`gs`) — PDF compression
  - **LibreOffice** — Office → PDF
  - **Tesseract** — OCR

### Setup

```bash
cd "pdf suite"
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # optional
```

### Run

```bash
# Development (API docs at /docs)
APP_ENV=development uvicorn main:app --host 127.0.0.1 --port 8000 --reload

# Or
python main.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

### Tests

```bash
APP_ENV=development pytest -q
```

## Docker (recommended for enterprise)

```bash
docker compose up --build -d
# App: http://localhost:8000
# Health: http://localhost:8000/health
```

The image runs as a **non-root** user, drops Linux capabilities, uses a read-only root filesystem with `tmpfs` for temp files, and exposes a Docker **healthcheck**.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_ENV` | `production` | `development` enables `/docs` and OpenAPI |
| `MAX_UPLOAD_BYTES` | `104857600` (100 MB) | Per-file upload limit |
| `MAX_FILES_PER_REQUEST` | `50` | Batch upload cap |
| `SUBPROCESS_TIMEOUT_SECONDS` | `180` | Ghostscript / LibreOffice timeout |
| `ALLOW_ORIGINS` | `*` | CORS origins (set to your intranet URL) |
| `TEMP_DIR` | system temp | Dedicated temp directory |
| `ENABLE_OCR` / `ENABLE_LIBREOFFICE` / `ENABLE_GHOSTSCRIPT` / `ENABLE_CAMELOT` | `true` | Feature toggles |

See `.env.example` for the full list.

## Enterprise posture

### Security

- Upload **size limits** and **extension / PDF magic-byte** checks
- **Filename sanitization** (path traversal blocked)
- Subprocess **timeouts** (no hung Ghostscript/LibreOffice jobs)
- Temp files cleaned via **background tasks**
- Security headers: `X-Content-Type-Options`, `X-Frame-Options`, CSP, `Referrer-Policy`
- **Request IDs** (`X-Request-ID`) on every response
- API docs disabled in production
- Docker: non-root, `no-new-privileges`, dropped capabilities, read-only FS

### Privacy

All processing is local. Suitable for confidential bank / legal / HR documents when deployed on trusted infrastructure.

### Operations

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness |
| `GET /ready` | Readiness + dependency checks |
| `GET /` | UI |

Logs are structured one-line access logs with request IDs.

## API overview

All processing endpoints are `POST` under `/api/*` and accept `multipart/form-data`.

Examples: `/api/merge`, `/api/split`, `/api/compress`, `/api/ocr`, `/api/protect`, `/api/unlock`, `/api/watermark`, `/api/scrub`, `/api/preview`, `/api/convert/to-pdf`, `/api/convert/to-word`, `/api/convert/to-excel`, `/api/compress-office`, `/api/edit-page`, `/api/modify-pages`.

In development, full interactive docs: `/docs`.

## Project layout

```
pdf suite/
├── app/
│   ├── config.py       # Env-based settings
│   ├── security.py     # Validation & sanitization
│   └── tempfiles.py    # Temp file lifecycle
├── main.py             # FastAPI routes
├── index.html          # Single-page UI
├── tests/              # Pytest suite
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Known limits

- Table extraction quality depends on PDF layout (Camelot → pdfplumber → OCR fallback).
- Visual stamper edits the currently previewed page coordinates.
- OCR and Office conversion require system packages (bundled in Docker image).
- 8 GB RAM hosts should avoid many concurrent large OCR jobs.

## License

Internal / private use unless otherwise stated by the author.
