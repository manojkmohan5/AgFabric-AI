"""Periodic agent execution.

An asyncio task in the app lifespan, not Celery. What is actually needed here is
"run a handful of functions on their own cadences" — Celery would add a broker, a
worker process, and a beat process to do that.

This is the fallback path. When CELERY_BROKER_URL is set, `app.tasks` owns
scheduling and this loop stays off, so the two never run the same agent twice.
Both call `agents.execute`, so there is one implementation of each job.

Each agent carries its own `interval_seconds` rather than sharing one global
tick. The loop wakes on a short heartbeat and runs only what is due, so the FX
agent can refresh every six hours while risk scanning runs every five minutes.
A single shared interval had to be set to the fastest agent's needs, which meant
polling free third-party APIs hundreds of times a day for data that changes once
— wasteful, and a good way to earn a throttle mid-demo.

ponytail: in-process timer, so it runs once per API process, and the due-times
live in memory. With N replicas the agents run N times per interval and a restart
re-runs everything — which is exactly when to switch to Celery.
"""

import asyncio
import logging
import time

from .agents import REGISTRY, SCHEDULED, execute
from .config import settings
from .db import SessionLocal

logger = logging.getLogger(__name__)

# How often the loop wakes to look for due work. Independent of any agent's
# interval — it only bounds how late an agent can be, so it stays small.
HEARTBEAT_SECONDS = 30


def _interval(name: str) -> int:
    """The configured cadence for an agent, in seconds.

    AGENT_INTERVAL_SECONDS remains the floor, so an operator can still slow every
    agent down at once (or speed the whole set up in a demo) without editing the
    registry. An agent never runs more often than its own interval asks for.
    """
    own = REGISTRY[name].interval_seconds
    if own is None:  # pragma: no cover — SCHEDULED only holds timed agents
        raise ValueError(f"{name} is not a scheduled agent")
    return max(own, settings.agent_interval_seconds)


async def run_forever() -> None:
    """Tick each scheduled agent on its own interval until cancelled."""
    logger.info(
        "agent scheduler started: %s",
        ", ".join(f"{n} every {_interval(n)}s" for n in SCHEDULED),
    )
    # Monotonic, not wall clock: a clock change or DST shift must not strand an
    # agent for hours or stampede them all at once.
    next_due = {name: time.monotonic() + _interval(name) for name in SCHEDULED}

    while True:
        try:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            now = time.monotonic()
            for name in SCHEDULED:
                if now < next_due[name]:
                    continue
                await asyncio.to_thread(_run_one, name, "scheduled")
                # Scheduled off completion, not off the planned time, so a slow
                # agent cannot queue up a backlog of overdue runs.
                next_due[name] = time.monotonic() + _interval(name)
        except asyncio.CancelledError:
            logger.info("agent scheduler stopping")
            raise
        except Exception:
            # A failed tick must not kill the loop; the next one should still run.
            logger.exception("scheduler tick failed")


async def tick(trigger: str) -> None:
    """Run every scheduled agent once, off the event loop thread.

    Used at startup so a freshly opened app already shows forecasts, board
    prices, FX and news instead of empty panels waiting on a manual run.
    """
    for name in SCHEDULED:
        await asyncio.to_thread(_run_one, name, trigger)


def _run_one(name: str, trigger: str) -> None:
    # Its own session: this runs on a worker thread with no request scope.
    with SessionLocal() as db:
        run = execute(db, name, trigger=trigger)
        if run.status == "failed":
            logger.warning("agent %s failed: %s", name, run.error)
