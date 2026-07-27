# Local PDF Suite — production image
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    APP_VERSION=1.3.0 \
    PORT=8000 \
    HOST=0.0.0.0 \
    RUNNING_IN_DOCKER=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /app

# System dependencies for Ghostscript, OCR, LibreOffice, Camelot/OpenCV
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-eng \
    qpdf \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    libreoffice-java-common \
    default-jre-headless \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app ./app
COPY main.py index.html ./

# Non-root runtime user
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /tmp/pdfsuite \
    && chown -R appuser:appuser /app /tmp/pdfsuite

ENV TEMP_DIR=/tmp/pdfsuite

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Single worker is safer for memory-heavy PDF jobs; scale with replicas if needed
CMD ["sh", "-c", "uvicorn main:app --host ${HOST} --port ${PORT} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
