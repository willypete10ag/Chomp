# Chomp MCP server. Multi-stage: uv builds the venv, the runtime image has no build tools.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md SCHEMA.sql ./
COPY migrations ./migrations
COPY chomp ./chomp
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
# tesseract reads nutrition label photos on the dashboard's New food page (#59).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
# Fixed uid/gid so the backup folder on the host can be chowned to match (see README).
RUN groupadd --system --gid 10001 chomp \
    && useradd --system --uid 10001 --gid chomp --home-dir /app --no-create-home chomp \
    && mkdir -p /data /backups && chown chomp:chomp /data /backups
COPY --from=build --chown=chomp:chomp /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/chomp.db \
    BACKUP_DIR=/backups \
    HOST=0.0.0.0 \
    PORT=8000
USER chomp
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\", timeout=4)"]
CMD ["chomp", "serve"]
