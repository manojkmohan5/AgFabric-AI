"""Routes for the AI Agent Center and the Risk Center.

/alerts now reads persisted rows rather than recomputing per request, so an alert
has an id, a first-seen time, and a status a human can move.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import agents
from .auth import current_user, require_role
from .db import get_db
from .models import FINANCE_ROLES, AgentRun, Alert, User

router = APIRouter(tags=["agents"])

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[User, Depends(current_user)]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
# Duplicate invoices expose commercial amounts, so the same rule as /dashboard.
FINANCE_ONLY_KINDS = ("duplicate_invoice",)


@router.get("/agents")
def list_agents(db: DbDep, user: UserDep) -> dict:
    """The registry plus each agent's most recent run.

    DISTINCT ON rather than fetching the table and reducing in Python: with the
    scheduler running every few minutes that table grows indefinitely, and the
    old version's cost grew with it. This reads one row per agent using
    ix_agent_runs_agent_started.
    """
    latest = {
        run.agent: run
        for run in db.scalars(
            select(AgentRun)
            .distinct(AgentRun.agent)
            .order_by(AgentRun.agent, AgentRun.started_at.desc())
        ).all()
    }

    return {
        "agents": [
            {
                "name": agent.name,
                "description": agent.description,
                "scheduled": agent.name in agents.SCHEDULED,
                "last_run": _serialise_run(latest[agent.name])
                if agent.name in latest
                else None,
            }
            for agent in agents.REGISTRY.values()
        ]
    }


@router.post("/agents/{name}/run")
def run_agent(
    name: str,
    db: DbDep,
    user: Annotated[User, Depends(require_role("ops", "exec"))],
) -> dict:
    if name not in agents.REGISTRY:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown agent {name!r}; known: {', '.join(sorted(agents.REGISTRY))}",
        )
    run = agents.execute(db, name, trigger="manual")
    # A failed agent is reported as a failed run, not as a failed request — the
    # caller asked to run it, and it ran.
    return _serialise_run(run)


@router.get("/agents/runs")
def agent_runs(
    db: DbDep,
    user: UserDep,
    agent: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    query = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
    if agent:
        query = query.where(AgentRun.agent == agent)
    return {"runs": [_serialise_run(r) for r in db.scalars(query).all()]}


@router.get("/alerts")
def list_alerts(
    db: DbDep,
    user: UserDep,
    alert_status: Annotated[str | None, Query(alias="status")] = "open",
    severity: str | None = None,
    kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    query = select(Alert)
    if alert_status and alert_status != "all":
        query = query.where(Alert.status == alert_status)
    if severity:
        query = query.where(Alert.severity == severity)
    if kind:
        query = query.where(Alert.kind == kind)

    rows = db.scalars(query.limit(limit)).all()
    if user.role not in FINANCE_ROLES:
        rows = [r for r in rows if r.kind not in FINANCE_ONLY_KINDS]

    rows = sorted(
        rows, key=lambda r: (SEVERITY_ORDER.get(r.severity, 3), -r.confidence, r.id)
    )
    return {
        "count": len(rows),
        "open_total": db.scalar(
            select(func.count()).select_from(Alert).where(Alert.status == "open")
        )
        or 0,
        "alerts": [_serialise_alert(r) for r in rows],
    }


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge(alert_id: int, db: DbDep, user: UserDep) -> dict:
    alert = _visible_alert(db, alert_id, user)
    if alert.status == "resolved":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "cannot acknowledge a resolved alert"
        )
    alert.status = "acknowledged"
    alert.acknowledged_by = user.id
    db.commit()
    return _serialise_alert(alert)


@router.post("/alerts/{alert_id}/resolve")
def resolve(alert_id: int, db: DbDep, user: UserDep) -> dict:
    alert = _visible_alert(db, alert_id, user)
    alert.status = "resolved"
    alert.resolved_at = datetime.now(UTC)
    alert.acknowledged_by = user.id
    db.commit()
    return _serialise_alert(alert)


def _visible_alert(db: Session, alert_id: int, user: User) -> Alert:
    alert = db.get(Alert, alert_id)
    # Same 404 whether it does not exist or the role may not see it, so the
    # response does not confirm the existence of alerts they cannot read.
    if alert is None or (
        alert.kind in FINANCE_ONLY_KINDS and user.role not in FINANCE_ROLES
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such alert")
    return alert


def _serialise_alert(a: Alert) -> dict:
    return {
        "id": a.id,
        "kind": a.kind,
        "severity": a.severity,
        "title": a.title,
        "confidence": a.confidence,
        "evidence": a.evidence,
        "recommendation": a.recommendation,
        "status": a.status,
        "first_seen_at": a.first_seen_at.isoformat(),
        "last_seen_at": a.last_seen_at.isoformat(),
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
    }


def _serialise_run(r: AgentRun) -> dict:
    return {
        "id": r.id,
        "agent": r.agent,
        "status": r.status,
        "trigger": r.trigger,
        "started_at": r.started_at.isoformat(),
        "duration_ms": r.duration_ms,
        "items": r.items,
        "detail": r.detail,
        "error": r.error,
    }
