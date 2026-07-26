# Local PDF Suite

Private, on-prem PDF toolkit. Documents are processed **on your machine / server** — nothing is sent to a third-party cloud.

**Version:** 1.2.0

## Features

| Category | Tools |
|----------|--------|
| Conversion | PDF → Excel, PDF → Word, Office → PDF |
| Organization | Merge, extract pages, rotate/delete, compress PDF |
| Security | Protect (AES-256), unlock, watermark, scrub metadata, OCR |
| Editing | Content editor (replace / redact / add text), Office compress |

## Quick start (local)

### Requirements

- Python 3.11+ (3.12+ recommended)
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
# Easiest
./run.sh

# Or manually (API docs at /docs in development)
APP_ENV=development uvicorn main:app --host 127.0.0.1 --port 8000 --reload

# Or
python main.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Press `/` in the UI to search tools. The sidebar footer shows server readiness and missing optional engines.

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
| `HOST` | `127.0.0.1` (local) / `0.0.0.0` (Docker) | Bind address |
| `MAX_UPLOAD_BYTES` | `104857600` (100 MB) | Per-file upload limit |
| `MAX_FILES_PER_REQUEST` | `50` | Batch upload cap |
| `MAX_OCR_PAGES` | `200` | OCR page ceiling |
| `MAX_CONCURRENT_JOBS` | `2` | Parallel heavy jobs (OCR/GS/LibreOffice) |
| `RATE_LIMIT_PER_MINUTE` | `120` | Per-IP API rate limit |
| `SUBPROCESS_TIMEOUT_SECONDS` | `180` | Ghostscript / LibreOffice timeout |
| `ALLOW_ORIGINS` | `*` | CORS origins (set to your intranet URL) |
| `API_KEY` | _(unset)_ | If set, required on all `/api/*` routes |
| `TRUSTED_HOSTS` | _(unset)_ | Optional Host header allow-list |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trusted reverse-proxy IPs |
| `TEMP_DIR` | system temp | Dedicated temp directory |
| `ENABLE_*` | `true` | Feature toggles for OCR / LibreOffice / GS / Camelot |

See `.env.example` for the full list.

### Exposing on a network safely

1. Set a strong `API_KEY`.
2. Set `ALLOW_ORIGINS` to your real UI origin (not `*`).
3. Prefer a reverse proxy with TLS; keep `FORWARDED_ALLOW_IPS` tight.
4. Optionally set `TRUSTED_HOSTS=your.hostname`.

## Security posture

- Upload **size limits**, **extension** and **magic-byte** checks (PDF / OOXML)
- **Filename sanitization** (path traversal blocked)
- **ZIP bomb / zip-slip** guards for Office packages
- **Password length** limits
- **AES-256** PDF encryption (pypdf)
- Subprocess **timeouts** (no hung Ghostscript/LibreOffice jobs)
- **Concurrency** gate for heavy jobs
- **Per-IP rate limiting** on `/api/*`
- Optional **API key** auth for network deployments
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
| `GET /ready` | Readiness + dependency / feature checks |
| `GET /` | UI |

Logs are structured one-line access logs with request IDs.

## API overview

All processing endpoints are `POST` under `/api/*` and accept `multipart/form-data`.

Examples: `/api/merge`, `/api/split`, `/api/compress`, `/api/ocr`, `/api/protect`, `/api/unlock`, `/api/watermark`, `/api/scrub`, `/api/preview`, `/api/convert/to-pdf`, `/api/convert/to-word`, `/api/convert/to-excel`, `/api/compress-office`, `/api/edit-page`, `/api/modify-pages`, `/api/edit/inspect`, `/api/edit/apply`.

In development, full interactive docs: `/docs`.

When `API_KEY` is set, send header `X-API-Key: <key>` or `Authorization: Bearer <key>`.

## Project layout

```
pdf suite/
├── app/
│   ├── config.py          # Env-based settings
│   ├── security.py        # Validation, sanitization, zip guards
│   ├── middleware.py      # Request IDs, headers, API key, rate limit
│   ├── job_limit.py       # Concurrency + rate limiter
│   ├── pdf_editor.py      # PyMuPDF content editing
│   ├── tempfiles.py       # Temp file lifecycle
│   └── subprocess_util.py # Timed Ghostscript / LibreOffice
├── main.py                # FastAPI routes
├── index.html             # Single-page UI
├── tests/                 # Pytest suite
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Known limits

- Table extraction quality depends on PDF layout (Camelot → pdfplumber → OCR fallback).
- Visual/content editor coordinates are page-relative (not full Acrobat reflow).
- OCR and Office conversion require system packages (bundled in Docker image).
- 8 GB RAM hosts should avoid many concurrent large OCR jobs (`MAX_CONCURRENT_JOBS`).

## License

Internal / private use unless otherwise stated by the author.
