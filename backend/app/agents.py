"""Agent registry: the background jobs, and the record of them running.

Each agent is a plain function taking a Session and returning a detail dict. That
keeps them callable three ways — on demand via the API, on the in-process
schedule, or from a Celery task later — without any of them knowing which.

Five real agents rather than the nine the plan lists. Notification, forecast,
analytics, and knowledge-graph maintenance have no work to do yet; registering
empty shells would report green for jobs that do nothing.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import feeds, indexing, market, metrics, risk, vectors
from .config import settings
from .models import (
    AgentRun,
    Alert,
    Commodity,
    Contract,
    Delivery,
    Document,
    DocumentChunk,
    Invoice,
    StorageBin,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Agent:
    name: str
    description: str
    run: Callable[[Session], dict[str, Any]]
    # How often the scheduler re-runs this agent. None means on-demand only.
    # Per-agent rather than one global interval because these sources refresh at
    # wildly different rates: a 5-minute tick would hit the FX API 288 times a
    # day for a rate that changes once, and poll Yahoo and Google News hard
    # enough to risk a throttle or an IP block in the middle of a demo.
    interval_seconds: int | None = None


# ---------------------------------------------------------------- risk scanning


def scan_risks(db: Session) -> dict[str, Any]:
    """Run every rule and reconcile the results against the stored alerts."""
    today = datetime.now(UTC).date()
    invoices = db.scalars(select(Invoice)).all()
    bins = db.scalars(select(StorageBin)).all()
    contracts = db.scalars(select(Contract)).all()
    deliveries = db.scalars(select(Delivery)).all()
    lbs_per_bu = {
        c.id: Decimal(c.lbs_per_bu) for c in db.scalars(select(Commodity)).all()
    }

    # Market-derived rules only fire once prices have been synced; with no
    # prices `mark_to_market` returns nothing and both rules are simply quiet,
    # rather than erroring or inventing a position.
    prices = market.latest_prices(db)
    valued = market.mark_to_market(market.load_open_contracts(db), prices)
    positions = market.position_summary(valued, prices)

    found = [
        *risk.duplicate_invoices(invoices),
        *risk.bin_anomalies(bins),
        *risk.expiring_contracts(contracts, today),
        *risk.missing_deliveries(contracts, deliveries, today),
        *risk.data_inconsistencies(deliveries, lbs_per_bu),
        *risk.unhedged_position(positions),
        *risk.contract_offmarket(valued),
    ]
    return upsert_alerts(db, found)


def upsert_alerts(db: Session, found: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconcile a scan's findings with stored alerts.

    - a new fingerprint is inserted
    - a fingerprint seen again refreshes last_seen_at and current details, and
      reopens the alert if a previous scan had auto-resolved it
    - a stored open alert the scan no longer reports is auto-resolved

    An acknowledged alert is left acknowledged: a human decision about a
    still-present condition must not be silently undone by the next scan.
    """
    now = datetime.now(UTC)
    by_fingerprint = {a["fingerprint"]: a for a in found}

    existing = {
        a.fingerprint: a
        for a in db.scalars(
            select(Alert).where(Alert.fingerprint.in_(by_fingerprint.keys()))
        ).all()
    }

    created = updated = reopened = 0
    for fp, alert in by_fingerprint.items():
        row = existing.get(fp)
        if row is None:
            db.add(
                Alert(
                    fingerprint=fp,
                    kind=alert["kind"],
                    severity=alert["severity"],
                    title=alert["title"],
                    confidence=float(alert["confidence"]),
                    evidence=alert["evidence"],
                    recommendation=alert["recommendation"],
                    status="open",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            created += 1
            continue

        # Refresh the details — severity and confidence drift as dates move.
        row.severity = alert["severity"]
        row.title = alert["title"]
        row.confidence = float(alert["confidence"])
        row.evidence = alert["evidence"]
        row.recommendation = alert["recommendation"]
        row.last_seen_at = now
        if row.status == "resolved":
            row.status = "open"
            row.resolved_at = None
            reopened += 1
        else:
            updated += 1

    # Anything open or acknowledged that no longer appears has gone away.
    stale = db.scalars(
        select(Alert).where(
            Alert.status.in_(("open", "acknowledged")),
            Alert.fingerprint.notin_(by_fingerprint.keys() or {""}),
        )
    ).all()
    for row in stale:
        row.status = "resolved"
        row.resolved_at = now

    db.commit()
    return {
        "found": len(found),
        "created": created,
        "updated": updated,
        "reopened": reopened,
        "auto_resolved": len(stale),
    }


# ------------------------------------------------------------------ other agents


def backfill_embeddings(db: Session) -> dict[str, Any]:
    """Embed anything not yet in the vector index."""
    return indexing.reindex(db)


def resolve_entities(db: Session) -> dict[str, Any]:
    """Report deliveries not linked to a contract, with candidate matches.

    Report-only on purpose. Auto-attaching a delivery to a guessed contract would
    silently rewrite commercial records; a human should confirm the match.
    """
    unlinked = db.scalars(select(Delivery).where(Delivery.contract_id.is_(None))).all()
    suggestions = []
    for delivery in unlinked:
        candidates = db.scalars(
            select(Contract).where(
                Contract.customer_id == delivery.customer_id,
                Contract.commodity_id == delivery.commodity_id,
                Contract.status == "open",
            )
        ).all()
        suggestions.append(
            {
                "ticket": delivery.ticket_number,
                "candidate_contracts": [c.number for c in candidates],
            }
        )
    return {
        "unlinked_deliveries": len(unlinked),
        "with_candidates": sum(1 for s in suggestions if s["candidate_contracts"]),
        "suggestions": suggestions[:50],
    }


def sync_weather(db: Session) -> dict[str, Any]:
    """Pull Open-Meteo forecasts for every facility."""
    from .weather import sync_all

    return sync_all(db)


def sync_market(db: Session) -> dict[str, Any]:
    """Pull CBOT grain futures closes for every mapped commodity."""
    return market.sync_all(db)


def sync_fx(db: Session) -> dict[str, Any]:
    """Pull USD exchange rates for the currencies that move grain exports."""
    return feeds.sync_fx(db)


def sync_news(db: Session) -> dict[str, Any]:
    """Pull agricultural market headlines."""
    return feeds.sync_news(db)


def collect_monitoring(db: Session) -> dict[str, Any]:
    """Counts the Monitoring page reads. Cheap enough to run on every tick."""

    def count(model) -> int:  # noqa: ANN001
        return db.scalar(select(func.count()).select_from(model)) or 0

    return {
        "documents": count(Document),
        "chunks": count(DocumentChunk),
        "chunks_pending_embedding": db.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.embedded.is_(False))
        )
        or 0,
        "vectors": vectors.count(),
        "deliveries": count(Delivery),
        "open_alerts": db.scalar(
            select(func.count()).select_from(Alert).where(Alert.status == "open")
        )
        or 0,
    }


REGISTRY: dict[str, Agent] = {
    a.name: a
    for a in (
        # Pure DB work: no third party to annoy, no tokens to spend.
        Agent(
            "risk",
            "Run all risk rules and reconcile stored alerts",
            scan_risks,
            interval_seconds=300,
        ),
        # Only calls OpenAI when chunks are actually waiting, so an idle instance
        # costs nothing on this tick.
        Agent(
            "embedding",
            "Embed document chunks not yet indexed",
            backfill_embeddings,
            interval_seconds=300,
        ),
        Agent(
            "entity_resolution",
            "Find deliveries with no contract and suggest matches",
            resolve_entities,
            interval_seconds=1800,
        ),
        # Open-Meteo publishes hourly; 30 minutes keeps it fresh without waste.
        Agent(
            "weather",
            "Pull Open-Meteo forecasts per facility",
            sync_weather,
            interval_seconds=1800,
        ),
        # The CBOT board moves intraday but settles daily. 15 minutes is live
        # enough to demo and gentle enough that Yahoo does not throttle us.
        Agent(
            "market",
            "Pull CBOT grain futures closes and reprice positions",
            sync_market,
            interval_seconds=900,
        ),
        # open.er-api.com's free tier refreshes once every 24h. Anything faster
        # is pure waste — it returns the identical rate.
        Agent(
            "fx",
            "Pull USD rates for export-relevant currencies",
            sync_fx,
            interval_seconds=21_600,
        ),
        Agent(
            "news",
            "Pull agricultural market headlines",
            sync_news,
            interval_seconds=1800,
        ),
        Agent(
            "monitoring",
            "Collect system counts",
            collect_monitoring,
            interval_seconds=900,
        ),
    )
}

# Derived from the registry, so adding an agent with an interval schedules it and
# there is no second list to forget to update.
SCHEDULED: tuple[str, ...] = tuple(
    a.name for a in REGISTRY.values() if a.interval_seconds is not None
)


def execute(db: Session, name: str, trigger: str = "manual") -> AgentRun:
    """Run one agent and record the outcome. Never raises for agent failure.

    A crashed agent must still leave an AgentRun row saying so, otherwise the
    Agent Center shows a stale green from the previous successful run.
    """
    agent = REGISTRY[name]
    started = datetime.now(UTC)
    detail: dict[str, Any] | None = None
    error: str | None = None
    try:
        detail = agent.run(db)
    except Exception as exc:
        db.rollback()
        error = str(exc)[:500]
        logger.exception("agent %s failed", name)

    finished = datetime.now(UTC)
    run = AgentRun(
        agent=name,
        status="failed" if error else "ok",
        trigger=trigger,
        started_at=started,
        finished_at=finished,
        duration_ms=int((finished - started).total_seconds() * 1000),
        items=_item_count(detail),
        detail=detail,
        error=error,
    )
    db.add(run)
    db.commit()
    metrics.agent_runs_total.labels(name, run.status).inc()
    metrics.agent_duration.labels(name).observe(run.duration_ms / 1000)
    _prune_runs(db, name)
    return run


def _prune_runs(db: Session, name: str) -> None:
    """Keep only the newest N runs for this agent.

    Without this the table grows forever — two rows per scheduler tick, so a
    5-minute interval adds ~576 rows a day and never stops.
    """
    keep = (
        select(AgentRun.id)
        .where(AgentRun.agent == name)
        .order_by(AgentRun.started_at.desc())
        .limit(settings.agent_run_retention)
        .scalar_subquery()
    )
    db.execute(delete(AgentRun).where(AgentRun.agent == name, AgentRun.id.notin_(keep)))
    db.commit()


def _item_count(detail: dict[str, Any] | None) -> int:
    if not detail:
        return 0
    for key in (
        "found",
        "chunks_indexed",
        "unlinked_deliveries",
        "prices_written",
        "rates_written",
        "new_items",
        "documents",
    ):
        if isinstance(detail.get(key), int):
            return detail[key]
    return 0
