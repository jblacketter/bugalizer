# Bugalizer — app-only image (Phase 5 §5.5).
# Ollama runs NATIVELY on the host for GPU access; point the container at it
# via BUGALIZER_OLLAMA_HOST=http://host.docker.internal:11434 (see compose).
FROM python:3.12-slim

# git: required by git_ops (clone/pull of project repos).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

# Dependency layer — cached until pyproject.toml/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Project layer.
COPY src ./src
RUN uv sync --frozen --no-dev

# Non-root runtime user. /data holds ALL mutable state (SQLite DB, cloned
# repos, repo-map cache) and is volume-mounted by compose.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /data /app
USER app

ENV BUGALIZER_DB_PATH=/data/bugalizer.db \
    BUGALIZER_REPOS_DIR=/data/repos \
    BUGALIZER_CACHE_DIR=/data/cache

EXPOSE 8090

# Liveness probe (dependency-free endpoint); uses the venv python so the
# image needs no curl/wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/app/.venv/bin/python", "-c", \
       "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health/live', timeout=4)"]

CMD ["/app/.venv/bin/uvicorn", "bugalizer.main:app", "--host", "0.0.0.0", "--port", "8090"]
