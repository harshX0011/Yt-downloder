# syntax=docker/dockerfile:1

FROM python:3.12-slim AS runtime

# ffmpeg lets the app merge split audio and video streams and remux to MP4.
# Without it the app still works, but only single-file renditions are offered.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    DOWNLOAD_DIR=/var/tmp/amd-downloads

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# Run unprivileged, with a writable scratch area for finished downloads.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p "$DOWNLOAD_DIR" \
    && chown -R appuser:appuser /app "$DOWNLOAD_DIR"
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# PORT is provided by most hosts; the shell form lets it expand.
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
