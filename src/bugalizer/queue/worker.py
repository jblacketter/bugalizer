"""Async background queue worker for processing bug reports."""

from __future__ import annotations

import asyncio
import logging

from bugalizer.config import settings
from bugalizer.db import (
    localization_eligible_reports,
    reports_eligible_for_fix,
    submitted_reports,
    triage_eligible_reports,
)
from bugalizer.pipeline.orchestrator import (
    process_fix_proposal,
    process_localization,
    process_submitted,
    process_triaged,
)

logger = logging.getLogger(__name__)

_worker_task: asyncio.Task | None = None


async def _process_with_semaphore(
    semaphore: asyncio.Semaphore,
    coro,
) -> None:
    """Run a coroutine bounded by the semaphore."""
    async with semaphore:
        await coro


async def _poll_loop() -> None:
    """Main polling loop — finds work and dispatches to pipeline concurrently."""
    semaphore = asyncio.Semaphore(settings.queue_max_concurrent)
    poll_interval = settings.queue_poll_seconds

    logger.info(
        "Queue worker started (poll=%ds, max_concurrent=%d)",
        poll_interval, settings.queue_max_concurrent,
    )

    while True:
        try:
            tasks: list[asyncio.Task] = []

            # Stage 1: submitted reports — dispatch as concurrent tasks
            for report in submitted_reports():
                task = asyncio.create_task(
                    _process_with_semaphore(semaphore, process_submitted(report["id"]))
                )
                tasks.append(task)

            # Stage 2: triage-eligible reports — dispatch as concurrent tasks
            for report in triage_eligible_reports():
                task = asyncio.create_task(
                    _process_with_semaphore(semaphore, process_triaged(report["id"]))
                )
                tasks.append(task)

            # Stage 3: localization-eligible reports — dispatch as concurrent tasks
            for report in localization_eligible_reports():
                task = asyncio.create_task(
                    _process_with_semaphore(semaphore, process_localization(report["id"]))
                )
                tasks.append(task)

            # Stage 4: fix-proposal-eligible reports (triaged + completed
            # localization + no fix proposal yet) — dispatch concurrently.
            # Opt-in only: each fix is a paid cloud call and local models can't
            # reliably produce a valid patch, so auto-fixing is off by default
            # and reports settle at 'triaged' with their localization. Fixes run
            # on demand via the "Analyze (cloud)" action.
            if settings.auto_fix_enabled:
                for report in reports_eligible_for_fix():
                    task = asyncio.create_task(
                        _process_with_semaphore(semaphore, process_fix_proposal(report["id"]))
                    )
                    tasks.append(task)

            # Await all dispatched tasks for this cycle
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, result in enumerate(results):
                    if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                        logger.error("Task %d failed: %s", i, result)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Queue worker error during poll cycle")

        await asyncio.sleep(poll_interval)


def worker_alive() -> bool:
    """True if the background queue worker task is running (started, not
    finished/cancelled). Used by the readiness health check."""
    return _worker_task is not None and not _worker_task.done()


def start_worker() -> asyncio.Task:
    """Start the background queue worker. Returns the task handle."""
    global _worker_task
    _worker_task = asyncio.create_task(_poll_loop(), name="bugalizer-queue-worker")
    logger.info("Queue worker task created")
    return _worker_task


async def stop_worker() -> None:
    """Cancel the background queue worker and wait for it to finish."""
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
        logger.info("Queue worker stopped")
