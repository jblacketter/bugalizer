"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bugalizer import __version__
from bugalizer.db import init_db
from bugalizer.api.reports import router as reports_router
from bugalizer.api.projects import router as projects_router
from bugalizer.api.queue import router as queue_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Bugalizer",
        description="AI-powered bug report processing server",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(reports_router, prefix="/api/v1")
    app.include_router(projects_router, prefix="/api/v1")
    app.include_router(queue_router, prefix="/api/v1")

    return app


app = create_app()
