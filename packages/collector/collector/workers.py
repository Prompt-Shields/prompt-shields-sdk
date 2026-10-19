"""Background workers: partner webhook delivery and the provider usage pull.

Which loops run is set by their intervals:
  PS_WEBHOOK_WORKER_INTERVAL_SECONDS       webhook delivery (0 = off)
  PS_PROVIDER_USAGE_PULL_INTERVAL_SECONDS  provider usage pull (0 = off)

Where they run:
- In the API process (collector.app lifespan), the default. Every replica
  then runs them; set PS_COLLECTOR_RUN_WORKERS=false on the API to stop that.
- In one dedicated process: ``python -m collector.workers`` runs the same
  loops without the HTTP API, for a single-replica worker Deployment. It
  ignores PS_COLLECTOR_RUN_WORKERS, so one shared environment can serve both.
- Or as one-shot CronJobs instead of loops: ``python -m collector.webhooks
  deliver`` and ``python -m collector.provider_usage_admin --all-tenants``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from collector.config import settings

logger = logging.getLogger(__name__)


def start_workers(session_factory) -> list[asyncio.Task]:
    """Start every loop whose interval is set; returns the running tasks."""
    tasks: list[asyncio.Task] = []
    if settings.webhook_worker_interval_seconds > 0:
        from collector.webhooks import run_worker

        tasks.append(
            asyncio.create_task(
                run_worker(session_factory, settings.webhook_worker_interval_seconds)
            )
        )
    if settings.provider_usage_pull_interval_seconds > 0:
        from collector.provider_usage_admin import run_schedule

        tasks.append(
            asyncio.create_task(
                run_schedule(
                    session_factory,
                    settings.provider_usage_pull_interval_seconds,
                    settings.provider_usage_pull_days,
                )
            )
        )
    return tasks


async def stop_workers(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _run() -> int:
    from collector import rls
    from collector.app import SessionLocal, engine

    try:
        if settings.rls_required:
            async with engine.connect() as conn:
                await rls.check_required(conn)
        tasks = start_workers(SessionLocal)
        if not tasks:
            print(
                "No worker enabled: set PS_WEBHOOK_WORKER_INTERVAL_SECONDS and/or "
                "PS_PROVIDER_USAGE_PULL_INTERVAL_SECONDS above 0.",
                file=sys.stderr,
            )
            return 2
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        logger.info("collector workers running: %d loop(s)", len(tasks))
        # A loop only ends by raising (they catch and log per-run failures).
        done_or_stop = asyncio.ensure_future(stop.wait())
        await asyncio.wait([done_or_stop, *tasks], return_when=asyncio.FIRST_COMPLETED)
        done_or_stop.cancel()
        failed = [t for t in tasks if t.done() and not t.cancelled() and t.exception()]
        await stop_workers(tasks)
        for task in failed:
            logger.error("worker loop failed", exc_info=task.exception())
        return 1 if failed else 0
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
