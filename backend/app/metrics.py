"""Prometheus metrics.

`prometheus-client` is ~50KB of pure Python, which is why it is here and
OpenTelemetry is not: OTel would add a dozen packages plus a collector to run, and
without a collector it is dead weight. Deferred with that reason rather than wired
in for show.

The important detail is the `path` label: it is the **route template**
(`/documents/{document_id}`), never the raw URL. Labelling by raw path would mint
a new time series per document id and eventually take the scraper down. That is
the classic way self-hosted Prometheus falls over.
"""

import time
from collections.abc import Callable

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .models import AgentRun, Alert, AuditLog, Document, DocumentChunk

# Own registry rather than the global default, so importing this module twice (or
# in a test) cannot raise "duplicate metric".
REGISTRY = CollectorRegistry()

requests_total = Counter(
    "agfabric_http_requests_total",
    "HTTP requests handled",
    ["method", "path", "status"],
    registry=REGISTRY,
)
request_duration = Histogram(
    "agfabric_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "path"],
    # Tuned for this app: sub-10ms dashboard reads through multi-second LLM calls.
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

llm_tokens_total = Counter(
    "agfabric_llm_tokens_total",
    "LLM tokens consumed",
    ["provider", "kind"],
    registry=REGISTRY,
)
llm_cost_total = Counter(
    "agfabric_llm_cost_usd_total",
    "LLM spend in USD",
    ["provider"],
    registry=REGISTRY,
)
sql_gate_rejections = Counter(
    "agfabric_sql_gate_rejections_total",
    "Generated SQL blocked before execution",
    ["reason"],
    registry=REGISTRY,
)
agent_runs_total = Counter(
    "agfabric_agent_runs_total",
    "Agent executions",
    ["agent", "status"],
    registry=REGISTRY,
)
agent_duration = Histogram(
    "agfabric_agent_duration_seconds",
    "Agent execution time",
    ["agent"],
    buckets=(0.01, 0.05, 0.25, 1.0, 5.0, 30.0, 120.0),
    registry=REGISTRY,
)
webhook_events_total = Counter(
    "agfabric_webhook_events_total",
    "Inbound webhook events",
    ["event_type", "status"],
    registry=REGISTRY,
)

# Gauges reflect database state, so they are refreshed on scrape rather than
# maintained incrementally — one query per scrape, and it cannot drift.
open_alerts = Gauge(
    "agfabric_open_alerts", "Open alerts", ["severity"], registry=REGISTRY
)
documents_total = Gauge("agfabric_documents_total", "Documents stored", registry=REGISTRY)
chunks_pending = Gauge(
    "agfabric_chunks_pending_embedding",
    "Chunks not yet embedded",
    registry=REGISTRY,
)
audit_cost_total = Gauge(
    "agfabric_audit_cost_usd_lifetime",
    "Lifetime LLM spend from the audit log",
    registry=REGISTRY,
)


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        started = time.perf_counter()
        response = await call_next(request)

        # The matched route template, set by Starlette during routing. Falls back
        # to a constant rather than the raw path so an unmatched URL — a scanner
        # probing /wp-admin, say — cannot create unbounded label values.
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "<unmatched>"

        requests_total.labels(request.method, path, str(response.status_code)).inc()
        request_duration.labels(request.method, path).observe(
            time.perf_counter() - started
        )
        return response


def observe_llm(
    provider: str, input_tokens: int, output_tokens: int, cost: float
) -> None:
    if input_tokens:
        llm_tokens_total.labels(provider, "input").inc(input_tokens)
    if output_tokens:
        llm_tokens_total.labels(provider, "output").inc(output_tokens)
    if cost:
        llm_cost_total.labels(provider).inc(cost)


def refresh_gauges(db: Session) -> None:
    """Read current state into the gauges. Called on each scrape."""
    open_alerts.clear()
    for severity, count in db.execute(
        select(Alert.severity, func.count(Alert.id))
        .where(Alert.status == "open")
        .group_by(Alert.severity)
    ).all():
        open_alerts.labels(severity).set(count)

    documents_total.set(db.scalar(select(func.count()).select_from(Document)) or 0)
    chunks_pending.set(
        db.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.embedded.is_(False))
        )
        or 0
    )
    audit_cost_total.set(
        float(db.scalar(select(func.coalesce(func.sum(AuditLog.cost_usd), 0.0))) or 0.0)
    )


def seed_agent_counters(db: Session) -> None:
    """Prime the agent counter from stored runs so a restart is not a cliff.

    Counters normally only go up in-process; without this every restart would show
    zero runs until the next tick.
    """
    for agent, agent_status, count in db.execute(
        select(AgentRun.agent, AgentRun.status, func.count(AgentRun.id)).group_by(
            AgentRun.agent, AgentRun.status
        )
    ).all():
        agent_runs_total.labels(agent, agent_status).inc(count)


def render() -> bytes:
    return generate_latest(REGISTRY)
