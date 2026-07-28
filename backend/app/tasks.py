"""Celery tasks and beat schedule.

Deliberately thin. Every task opens a session and calls `agents.execute`, which
is the same function the API and the in-process scheduler call — so the agents
have no idea what triggered them and there is one implementation of each job, not
two that can drift.

Celery is optional. With CELERY_BROKER_URL unset the app runs the in-process
asyncio scheduler instead and nothing here is imported, so a single container
still works with no broker.

Run it (Redis is already in docker-compose):

    celery -A app.tasks worker --loglevel=info
    celery -A app.tasks beat   --loglevel=info
"""

import logging
from typing import Any

from celery import Celery
from celery.schedules import crontab

from .agents import REGISTRY
from .config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "agfabric",
    broker=settings.celery_broker_url or None,
    backend=settings.celery_result_backend or settings.celery_broker_url or None,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # A task that outlives its interval would otherwise stack up behind itself.
    task_time_limit=600,
    task_soft_time_limit=540,
    # Redelivered on worker loss rather than silently dropped. Safe because
    # agents are idempotent: the risk scan reconciles by fingerprint and the
    # embedding backfill skips chunks already indexed.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "risk-scan-every-5-minutes": {
            "task": "app.tasks.run_agent",
            "schedule": crontab(minute="*/5"),
            "args": ("risk",),
        },
        "embedding-backfill-every-10-minutes": {
            "task": "app.tasks.run_agent",
            "schedule": crontab(minute="*/10"),
            "args": ("embedding",),
        },
        "weather-sync-hourly": {
            "task": "app.tasks.sync_weather",
            "schedule": crontab(minute=15),
        },
    },
)


@celery_app.task(name="app.tasks.run_agent", bind=True, max_retries=2)
def run_agent(self, name: str) -> dict[str, Any]:  # noqa: ANN001
    """Run one registered agent. Returns the AgentRun summary."""
    from .agents import execute
    from .db import SessionLocal

    if name not in REGISTRY:
        # A bad name is a coding error, not something retrying will fix.
        raise ValueError(f"unknown agent {name!r}")

    with SessionLocal() as db:
        run = execute(db, name, trigger="scheduled")
        return {
            "agent": run.agent,
            "status": run.status,
            "duration_ms": run.duration_ms,
            "items": run.items,
            "error": run.error,
        }


@celery_app.task(name="app.tasks.sync_weather", bind=True, max_retries=3)
def sync_weather(self, days: int = 3) -> dict[str, Any]:  # noqa: ANN001
    """Pull Open-Meteo forecasts. Retries with backoff — the upstream is remote."""
    from .db import SessionLocal
    from .weather import sync_all

    with SessionLocal() as db:
        result = sync_all(db, days=days)

    # Partial failure is worth retrying; the sync is an upsert, so a retry cannot
    # duplicate the rows that already landed.
    if result["failures"] and self.request.retries < self.max_retries:
        raise self.retry(countdown=60 * (2**self.request.retries))
    return result
