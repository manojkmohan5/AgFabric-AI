import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, time, timedelta
from hmac import compare_digest
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import feeds, market, metrics, scheduler, storage, vectors
from . import graph as kg
from .admin import bootstrap_first_admin
from .admin import router as admin_router
from .agentsapi import router as agents_router
from .audit import router as audit_router
from .auth import current_user, make_token, verify_password
from .config import cors_origin_list, settings
from .db import SessionLocal, get_db
from .documents import router as documents_router
from .models import (
    FINANCE_ROLES,
    Alert,
    Commodity,
    Contract,
    Customer,
    Delivery,
    Facility,
    Invoice,
    MarketPrice,
    StorageBin,
    User,
    WeatherObservation,
)
from .provision import router as provision_router
from .query import router as query_router
from .ratelimit import login_limiter
from .search import router as search_router
from .webhooks import router as webhooks_router


async def _startup_agents() -> None:
    """Run every scheduled agent once, in the background, logging any failure.

    Its own coroutine so the exception never escapes into an unretrieved task
    warning, and so a slow third party delays only the panels that need it.
    """
    try:
        await scheduler.tick("startup")
    except asyncio.CancelledError:
        # Shut down before the sweep finished. Not an error.
        raise
    except Exception:
        logger.exception("startup agent run failed; continuing")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    task: asyncio.Task | None = None
    startup_agents: asyncio.Task | None = None

    # An empty database must still be reachable — otherwise a non-seeded deploy
    # has no account that can log in and provision the rest.
    try:
        with SessionLocal() as db:
            bootstrap_first_admin(db)
    except Exception:
        logger.exception("first-admin bootstrap failed; continuing")

    # Counters reset with the process; prime them from stored runs so a restart
    # does not look like zero activity.
    try:
        with SessionLocal() as db:
            metrics.seed_agent_counters(db)
    except Exception:
        logger.warning("could not prime agent metrics", exc_info=True)

    if settings.run_agents_on_startup:
        # Every scheduled agent runs once now, so an opened app already shows
        # forecasts, board prices, FX, news and risk findings instead of empty
        # panels waiting on the first interval.
        #
        # Deliberately not awaited. Four of these call third parties, and the
        # lifespan blocks the server from accepting connections until it returns
        # — so awaiting would leave the API dead for ~15s on every restart, and
        # dead indefinitely if one upstream hangs. Backgrounded, the API is up at
        # once and the panels fill in as each agent lands.
        startup_agents = asyncio.create_task(_startup_agents())
    # Celery beat owns scheduling when a broker is configured. Running both would
    # execute every agent twice per interval.
    if settings.enable_scheduler and not settings.celery_broker_url:
        task = asyncio.create_task(scheduler.run_forever())
    elif settings.celery_broker_url:
        logger.info("celery broker configured; in-process scheduler disabled")
    try:
        yield
    finally:
        # The startup sweep can still be mid-flight on a quick restart; cancelling
        # it stops uvicorn warning about a task that was never retrieved.
        for pending in (startup_agents, task):
            if pending is not None:
                pending.cancel()
                # Await the cancellation so shutdown does not leave it half-stopped.
                with suppress(asyncio.CancelledError):
                    await pending


app = FastAPI(title="AgFabric AI", version="0.1.0", lifespan=lifespan)

# Explicit origins, methods and headers rather than wildcards.
#
# allow_credentials is False on purpose: auth is a Bearer token in a header, not a
# cookie, so the browser never needs to send credentials cross-origin. Turning it
# on would only widen what a malicious page could do.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origin_list(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-AgFabric-Signature"],
    max_age=600,
)

app.include_router(documents_router)
app.include_router(search_router)
app.include_router(query_router)
app.include_router(agents_router)
app.include_router(webhooks_router)
app.include_router(audit_router)
app.include_router(admin_router)
app.include_router(provision_router)

app.add_middleware(metrics.MetricsMiddleware)

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[User, Depends(current_user)]


@app.middleware("http")
async def correlate(request: Request, call_next):  # noqa: ANN001, ANN201
    """Attach a request id to every request, and echo it back.

    A cheap slice of what distributed tracing gives you: one id links a log line,
    an audit row, and the response a user is looking at. An inbound
    X-Request-Id is honoured so a proxy or frontend can supply its own.
    """
    incoming = request.headers.get("X-Request-Id", "")
    # Bound and sanitise: this ends up in logs and a 36-char column.
    request_id = (
        incoming[:36]
        if incoming.replace("-", "").isalnum() and incoming
        else str(uuid4())
    )
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(db: DbDep, authorization: str | None = Header(None)) -> Response:
    if settings.metrics_token:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        # Constant-time: this is a shared secret, so a timing oracle is a real if
        # minor leak.
        if not compare_digest(supplied, settings.metrics_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid metrics token")
    metrics.refresh_gauges(db)
    return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)


logger = logging.getLogger(__name__)


@app.get("/health")
def health(db: DbDep, response: Response) -> dict:
    """Per-dependency health.

    Previously this only touched Postgres, so the app could report healthy while
    search was dead. Now every dependency is probed and named.

    Only a database failure returns 503: without it nothing works. Losing object
    storage or the vector store degrades uploads and search but leaves the
    dashboard, graph and alerts usable, so those report "degraded" and let a load
    balancer keep serving rather than pulling the whole app.
    """
    checks: dict[str, str] = {}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {_brief(exc)}"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unhealthy", "checks": checks}

    try:
        storage.client().bucket_exists(settings.s3_bucket)
        checks["object_storage"] = "ok"
    except Exception as exc:
        checks["object_storage"] = f"error: {_brief(exc)}"

    try:
        vectors.client().get_collections()
        checks["vector_store"] = "ok"
    except Exception as exc:
        checks["vector_store"] = f"error: {_brief(exc)}"

    degraded = [name for name, state in checks.items() if state != "ok"]
    if degraded:
        response.status_code = status.HTTP_207_MULTI_STATUS
    return {
        "status": "degraded" if degraded else "ok",
        "checks": checks,
        "degraded": degraded,
        # The login page reads this to decide whether to advertise demo accounts.
        "demo_mode": settings.demo_mode,
    }


def _brief(exc: Exception) -> str:
    """First line only — driver errors otherwise leak connection strings."""
    return str(exc).strip().splitlines()[0][:160]


@app.post("/login")
def login(
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: DbDep,
) -> dict:
    # Throttle before touching the database, so an unthrottled password oracle
    # cannot be used to enumerate or brute-force accounts.
    # ponytail: request.client.host is the socket peer. Read a trusted
    # X-Forwarded-For only once a proxy actually sits in front of this.
    caller = request.client.host if request.client else "unknown"
    if not login_limiter.allow(caller):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many login attempts. Try again shortly.",
            {"Retry-After": str(login_limiter.retry_after(caller))},
        )

    user = db.scalar(select(User).where(User.email == form.username))
    # Same error and roughly the same work for unknown user and wrong password,
    # so the response does not confirm which emails exist.
    if user is None or not verify_password(form.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    # Same generic message: a deactivated account should not be distinguishable
    # from a wrong password by an outsider probing the endpoint.
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return {
        "access_token": make_token(user),
        "token_type": "bearer",
        "user": {"email": user.email, "name": user.full_name, "role": user.role},
    }


@app.post("/logout")
def logout(user: UserDep) -> dict:
    # ponytail: stateless JWT, so logout is client-side token discard. Add a
    # Redis deny-list when tokens need real revocation.
    return {"status": "ok"}


@app.get("/dashboard")
def dashboard(db: DbDep, user: UserDep) -> dict:
    today = datetime.now(UTC).date()
    week_ago = datetime.now(UTC) - timedelta(days=7)

    capacity, stored = db.execute(
        select(func.sum(StorageBin.capacity_bu), func.sum(StorageBin.current_bu))
    ).one()
    capacity = float(capacity or 0)
    stored = float(stored or 0)

    recent = db.scalars(
        select(Delivery).order_by(Delivery.delivered_at.desc()).limit(10)
    ).all()
    names = dict(db.execute(select(Customer.id, Customer.name)).all())

    payload = {
        "storage": {
            "capacity_bu": capacity,
            "stored_bu": stored,
            "utilization_pct": round(stored / capacity * 100, 1) if capacity else 0.0,
            "bins": db.scalar(select(func.count()).select_from(StorageBin)) or 0,
        },
        "deliveries": {
            "last_7_days": db.scalar(
                select(func.count())
                .select_from(Delivery)
                .where(Delivery.delivered_at >= week_ago)
            )
            or 0,
            "unverified": db.scalar(
                select(func.count())
                .select_from(Delivery)
                .where(Delivery.verified.is_(False))
            )
            or 0,
        },
        "contracts": {
            "open": db.scalar(
                select(func.count())
                .select_from(Contract)
                .where(Contract.status == "open")
            )
            or 0,
            "expiring_30d": db.scalar(
                select(func.count())
                .select_from(Contract)
                .where(
                    Contract.status == "open",
                    Contract.end_date.between(today, today + timedelta(days=30)),
                )
            )
            or 0,
        },
        "recent_events": [
            {
                "ticket": d.ticket_number,
                "customer": names.get(d.customer_id, "unknown"),
                "truck_id": d.truck_id,
                "net_bu": float(d.net_bu),
                "moisture_pct": float(d.moisture_pct),
                "delivered_at": d.delivered_at.isoformat(),
                "verified": d.verified,
            }
            for d in recent
        ],
        # Read from the persisted alerts the risk agent maintains.
        "open_alerts": db.scalar(
            select(func.count()).select_from(Alert).where(Alert.status == "open")
        )
        or 0,
        # Severity breakdown, so the UI can show what the open alerts actually
        # are rather than only how many.
        "alerts_by_severity": dict(
            db.execute(
                select(Alert.severity, func.count(Alert.id))
                .where(Alert.status == "open")
                .group_by(Alert.severity)
            ).all()
        ),
        # Daily counts for the deliveries trend. Grouped in SQL rather than
        # fetched-and-bucketed in Python, and zero-filled below so a quiet day is
        # a visible zero instead of a gap the line jumps over.
        "deliveries_daily": _deliveries_daily(db, days=14),
    }

    # RBAC with teeth: financials only for accountant and exec.
    if user.role in FINANCE_ROLES:
        by_status = db.execute(
            select(Invoice.status, func.sum(Invoice.amount), func.count()).group_by(
                Invoice.status
            )
        ).all()
        payload["financial_summary"] = {
            s: {"amount": float(total or 0), "count": n} for s, total, n in by_status
        }
    return payload


def _graph(db: Session) -> kg.Graph:
    return kg.build(
        customers=db.scalars(select(Customer)).all(),
        commodities=db.scalars(select(Commodity)).all(),
        facilities=db.scalars(select(Facility)).all(),
        bins=db.scalars(select(StorageBin)).all(),
        contracts=db.scalars(select(Contract)).all(),
        deliveries=db.scalars(select(Delivery)).all(),
        invoices=db.scalars(select(Invoice)).all(),
    )


@app.get("/graph")
def graph_overview(db: DbDep, user: UserDep) -> dict:
    return kg.summary(_graph(db))


@app.get("/graph/entity/{node_id}")
def graph_entity(node_id: str, db: DbDep, user: UserDep, depth: int = 1) -> dict:
    try:
        kg.parse_node_id(node_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    try:
        return kg.expand(_graph(db), node_id, depth)
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no such node: {node_id}"
        ) from exc


def _deliveries_daily(db: Session, days: int = 14) -> list[dict]:
    """Deliveries per day for the trend chart, oldest first and zero-filled.

    Zero-filling matters: without it a day with no deliveries is a missing point,
    and a line chart silently interpolates across it — the chart would imply
    deliveries happened when none did.
    """
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days - 1)

    day = func.date(Delivery.delivered_at)
    counts = dict(
        db.execute(
            select(day, func.count(Delivery.id))
            .where(Delivery.delivered_at >= datetime.combine(start, time.min, UTC))
            .group_by(day)
        ).all()
    )
    # Driver may hand back date or str depending on backend; normalise to str keys.
    normalised = {
        (k.isoformat() if hasattr(k, "isoformat") else str(k)): v
        for k, v in counts.items()
    }
    return [
        {
            "date": (d := (start + timedelta(days=i))).isoformat(),
            "count": normalised.get(d.isoformat(), 0),
        }
        for i in range(days)
    ]


@app.get("/weather")
def weather(db: DbDep, user: UserDep, days: int = 5) -> dict:
    """Forecasts per facility, most recent sync.

    The weather agent writes these; without this endpoint the table was being
    populated and never read.
    """
    today = datetime.now(UTC).date()
    facilities = {f.id: f for f in db.scalars(select(Facility)).all()}
    rows = db.scalars(
        select(WeatherObservation)
        .where(WeatherObservation.observed_on >= today)
        .order_by(WeatherObservation.observed_on)
        .limit(max(1, min(days, 16)) * max(len(facilities), 1))
    ).all()

    by_facility: dict[str, list[dict]] = {}
    for row in rows:
        facility = facilities.get(row.facility_id)
        if facility is None:
            continue
        by_facility.setdefault(facility.name, []).append(
            {
                "date": row.observed_on.isoformat(),
                "temp_max_c": row.temp_max_c,
                "temp_min_c": row.temp_min_c,
                "precipitation_mm": row.precipitation_mm,
                "wind_max_kmh": row.wind_max_kmh,
                "humidity_pct": row.humidity_pct,
            }
        )

    latest = max((r.fetched_at for r in rows), default=None)
    return {
        "source": "open-meteo",
        "fetched_at": latest.isoformat() if latest else None,
        "facilities": [
            {"facility": name, "forecast": days_list}
            for name, days_list in sorted(by_facility.items())
        ],
    }


@app.get("/market")
def market_positions(db: DbDep, user: UserDep) -> dict:
    """Board prices, open position per commodity, and mark-to-market.

    RBAC: prices are public information and everyone sees them, but valuations and
    contract-level P&L are commercial figures — same rule as /dashboard, so ops and
    warehouse get the board and the bushels, not the dollars.
    """
    show_money = user.role in FINANCE_ROLES
    prices = market.latest_prices(db)

    board = [
        {
            "commodity": c.name,
            "symbol": market.SYMBOLS.get(c.name),
            "close_usd_per_bu": prices.get(c.id),
        }
        for c in db.scalars(select(Commodity).order_by(Commodity.name)).all()
        if c.name in market.SYMBOLS
    ]

    history = [
        {
            "symbol": row.symbol,
            "date": row.quoted_on.isoformat(),
            "close_usd_per_bu": row.close_usd_per_bu,
        }
        for row in db.scalars(
            select(MarketPrice).order_by(MarketPrice.quoted_on.asc())
        ).all()
    ]

    valued = market.mark_to_market(market.load_open_contracts(db), prices)
    positions = market.position_summary(valued, prices)
    latest = db.scalar(select(func.max(MarketPrice.fetched_at)))

    if not show_money:
        # Bushels and direction stay; dollars go.
        positions = [
            {k: v for k, v in p.items() if k not in ("unrealised_usd",)}
            for p in positions
        ]
        valued = []

    return {
        "source": "cbot-futures",
        "fetched_at": latest.isoformat() if latest else None,
        "financials_visible": show_money,
        "board": board,
        "history": history,
        "positions": positions,
        "contracts": sorted(valued, key=lambda c: -abs(c["unrealised_usd"]))[:20],
        "totals": {
            "unrealised_usd": (
                round(sum(c["unrealised_usd"] for c in valued), 2) if show_money else None
            ),
            "open_contracts": len(valued) if show_money else None,
        },
    }


@app.get("/feeds")
def market_feeds(db: DbDep, user: UserDep) -> dict:
    """FX rates and agricultural headlines.

    Both are public market information, so there is no role gate here — unlike
    /market, nothing in this payload is a commercial figure about this business.
    """
    return {
        "fx": {
            "base": "USD",
            "rates": feeds.fx_with_change(db),
        },
        "news": feeds.recent_news(db, limit=8),
    }


@app.get("/storage")
def storage_bins(db: DbDep, user: UserDep) -> dict:
    facilities = {f.id: f.name for f in db.scalars(select(Facility)).all()}
    commodities = {c.id: c.name for c in db.scalars(select(Commodity)).all()}
    return {
        "bins": [
            {
                "name": b.name,
                "facility": facilities.get(b.facility_id, "unknown"),
                "commodity": commodities.get(b.commodity_id) if b.commodity_id else None,
                "capacity_bu": float(b.capacity_bu),
                "current_bu": float(b.current_bu),
                "moisture_pct": float(b.moisture_pct) if b.moisture_pct else None,
            }
            for b in db.scalars(select(StorageBin).order_by(StorageBin.name)).all()
        ]
    }
