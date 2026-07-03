"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from bugalizer import __version__
from bugalizer.config import settings
from bugalizer.db import init_db
from bugalizer.api.reports import router as reports_router
from bugalizer.api.projects import router as projects_router
from bugalizer.api.queue import router as queue_router
from bugalizer.api.usage import router as usage_router
from bugalizer.queue.worker import start_worker, stop_worker, worker_alive

logger = logging.getLogger(__name__)

# Static assets shipped inside the package (§5.4 dashboard).
_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    if not settings.valid_api_keys():
        logger.warning(
            "API authentication is DISABLED (BUGALIZER_API_KEYS is empty). "
            "This is fine for local dev, but any always-on/LAN deployment MUST "
            "set BUGALIZER_API_KEYS. See docs/deploy-windows.md."
        )
    if settings.queue_enabled:
        start_worker()
    logger.info("Bugalizer started (v%s)", __version__)
    yield
    await stop_worker()
    logger.info("Bugalizer stopped")


async def _check_db() -> bool:
    """Return True if the SQLite connection answers a trivial query."""
    from bugalizer.db import _get_conn

    def _ping() -> bool:
        try:
            _get_conn().execute("SELECT 1")
            return True
        except Exception:  # pragma: no cover - defensive
            return False

    return await asyncio.to_thread(_ping)


async def _check_ollama() -> bool:
    """Return True if the Ollama host answers GET /api/tags within a short timeout."""
    url = settings.ollama_host.rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except Exception:
        return False


def create_app() -> FastAPI:
    app = FastAPI(
        title="Bugalizer",
        description="AI-powered bug report processing server",
        version=__version__,
        lifespan=lifespan,
    )

    # CORS is closed by default (empty origin list). The dashboard is served
    # same-origin by this app; other LAN apps call server-to-server with API
    # keys, not from a browser. Set BUGALIZER_CORS_ORIGINS to opt specific
    # origins in.
    cors_origins = settings.cors_origin_list()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        """Serve the queue dashboard (§5.4) — one self-contained static page.

        The page itself needs no auth; every API call it makes carries the
        X-API-Key the user enters (stored in browser localStorage).
        """
        return FileResponse(_STATIC_DIR / "dashboard.html", media_type="text/html")

    @app.get("/health/live", tags=["meta"])
    async def liveness() -> dict[str, str]:
        """Liveness probe for the process supervisor — cheap, no dependencies."""
        return {"status": "ok", "version": __version__}

    @app.get("/health", tags=["meta"])
    async def readiness(response: Response) -> dict[str, object]:
        """Readiness probe: DB reachability, Ollama reachability, worker-alive.

        Returns 503 only when the database is unreachable (the one hard
        dependency). Ollama being down or the worker being stopped reports
        `degraded` but still 200, since reports can still be accepted.
        """
        db_ok = await _check_db()
        ollama_ok = await _check_ollama()
        worker_ok = worker_alive() if settings.queue_enabled else None

        worker_acceptable = (worker_ok is None) or worker_ok
        overall = "ok" if (db_ok and ollama_ok and worker_acceptable) else "degraded"
        if not db_ok:
            response.status_code = 503

        return {
            "status": overall,
            "version": __version__,
            "checks": {
                "database": db_ok,
                "ollama": ollama_ok,
                "worker": worker_ok,
            },
        }

    app.include_router(reports_router, prefix="/api/v1")
    app.include_router(projects_router, prefix="/api/v1")
    app.include_router(queue_router, prefix="/api/v1")
    app.include_router(usage_router, prefix="/api/v1")

    return app


app = create_app()
