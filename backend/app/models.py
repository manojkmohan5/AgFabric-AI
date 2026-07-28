from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# Roles map 1:1 to the four users in the plan.
ROLES = ("ops", "accountant", "warehouse", "exec")
FINANCE_ROLES = ("accountant", "exec")


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(Text)
    # Deactivation rather than deletion: an ex-employee's audit history must stay
    # intact and still attributable. `current_user` rejects inactive accounts, so
    # an already-issued token stops working immediately.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Commodity(Base):
    __tablename__ = "commodities"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    unit: Mapped[str] = mapped_column(String(16), default="bu")
    lbs_per_bu: Mapped[Decimal] = mapped_column(Numeric(6, 2))


class Facility(Base):
    __tablename__ = "facilities"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    location: Mapped[str] = mapped_column(String(128))


class StorageBin(Base):
    __tablename__ = "storage_bins"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    commodity_id: Mapped[int | None] = mapped_column(ForeignKey("commodities.id"))
    capacity_bu: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    current_bu: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    moisture_pct: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # farmer | buyer
    contact_email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))


class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    commodity_id: Mapped[int] = mapped_column(ForeignKey("commodities.id"))
    quantity_bu: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    price_per_bu: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|closed

    # The dashboard's expiring-30d count filters status then ranges over
    # end_date; a composite in that order lets one index serve both, and the
    # leading column alone still serves the plain open-contracts count.
    __table_args__ = (Index("ix_contracts_status_end_date", "status", "end_date"),)


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contracts.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    commodity_id: Mapped[int] = mapped_column(ForeignKey("commodities.id"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    truck_id: Mapped[str] = mapped_column(String(32))
    gross_lbs: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    tare_lbs: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    net_bu: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    moisture_pct: Mapped[Decimal] = mapped_column(Numeric(4, 2))
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Dashboard counts unverified deliveries on every load.
    verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class WeatherObservation(Base):
    """Daily weather per facility, pulled from Open-Meteo.

    Unique on (facility, date) so the sync is an upsert and re-running it cannot
    duplicate rows.
    """

    __tablename__ = "weather_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), index=True)
    observed_on: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(32), default="open-meteo")
    temp_max_c: Mapped[float | None] = mapped_column(Float)
    temp_min_c: Mapped[float | None] = mapped_column(Float)
    precipitation_mm: Mapped[float | None] = mapped_column(Float)
    wind_max_kmh: Mapped[float | None] = mapped_column(Float)
    humidity_pct: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("facility_id", "observed_on"),)


class MarketPrice(Base):
    """Daily settlement price for a commodity's futures contract.

    Unique on (symbol, quoted_on) so the sync is an upsert — re-running it
    refreshes a day rather than duplicating it, same contract as the weather sync.

    Stored in **dollars per bushel**, converted at ingest. CBOT quotes grains in
    cents (Yahoo reports currency `USX`), and mixing cents and dollars in one
    column is exactly the kind of unit bug that silently corrupts every downstream
    figure — so the conversion happens once, at the boundary.
    """

    __tablename__ = "market_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    commodity_id: Mapped[int | None] = mapped_column(ForeignKey("commodities.id"))
    quoted_on: Mapped[date] = mapped_column(Date, index=True)
    close_usd_per_bu: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="yahoo")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("symbol", "quoted_on"),)


class FxRate(Base):
    """Daily USD exchange rate.

    Unique on (quote_currency, quoted_on) so the sync is an upsert, same contract
    as weather and market. Base is always USD — grain is quoted in dollars, so
    every rate here answers "what does a dollar buy", which is the direction a
    US exporter actually cares about.
    """

    __tablename__ = "fx_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    quote_currency: Mapped[str] = mapped_column(String(3), index=True)
    quoted_on: Mapped[date] = mapped_column(Date, index=True)
    rate: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="open-er-api")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("quote_currency", "quoted_on"),)


class NewsItem(Base):
    """An agricultural news headline.

    `guid` is unique: RSS feeds repeat the same item across polls, so this is what
    makes the sync idempotent rather than accumulating one duplicate row per tick.
    """

    __tablename__ = "news_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    guid: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(1024))
    publisher: Mapped[str | None] = mapped_column(String(128))
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    topic: Mapped[str] = mapped_column(String(32), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookEvent(Base):
    """An inbound event from an external system.

    `dedupe_key` is unique: an external sender that retries after a timeout must
    not create a second event. That is the whole reason this table exists rather
    than processing inline and forgetting.
    """

    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    # received -> processed | failed | ignored
    status: Mapped[str] = mapped_column(String(16), default="received", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """One AI request, recorded in full: who asked, what was retrieved, what came
    back, which model, how many tokens, and what it cost.

    This is the Audit Center. Retention is deliberate — an answer is only
    defensible if the evidence behind it can be reproduced later, so the retrieved
    context is stored rather than just the response.
    """

    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    user_email: Mapped[str] = mapped_column(String(255))
    user_role: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(String(64), index=True)

    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)

    provider: Mapped[str | None] = mapped_column(String(32))
    chat_model: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # What the answer was grounded in, kept so it can be re-examined.
    sources: Mapped[dict | None] = mapped_column(JSON)
    generated_sql: Mapped[str | None] = mapped_column(Text)
    sql_rejected: Mapped[str | None] = mapped_column(Text)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    graph_edge_count: Mapped[int] = mapped_column(Integer, default=0)

    took_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


ALERT_STATUSES = ("open", "acknowledged", "resolved")


class Alert(Base):
    """A risk finding, persisted so it has an identity and a history.

    `fingerprint` is the natural key of the underlying condition, so re-running a
    scan updates the existing row instead of inserting a near-duplicate each time.
    """

    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(255))
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[dict] = mapped_column(JSON)
    recommendation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    # Set when a scan no longer reports the condition, or a user resolves it.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class AgentRun(Base):
    """One execution of one agent. The Agent Center reads its status from here."""

    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # ok | failed
    trigger: Mapped[str] = mapped_column(String(16))  # manual | scheduled | startup
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)
    items: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)

    # Serves both "latest run per agent" (DISTINCT ON agent, started_at DESC) and
    # the filtered run history. A btree scans backward, so one ascending index
    # covers the descending order too. Replaces separate single-column indexes
    # on agent and started_at, which neither query could use together.
    __table_args__ = (Index("ix_agent_runs_agent_started", "agent", "started_at"),)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), index=True)
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Content hash: makes duplicate detection exact rather than heuristic, and
    # doubles as the object key so a hostile filename never reaches storage.
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    object_key: Mapped[str] = mapped_column(String(512))
    version: Mapped[int] = mapped_column(Integer, default=1)
    text_chars: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # pending -> extracted -> embedded, or failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    extract_note: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # /documents orders by this descending.
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
    # Set once the chunk is in Qdrant (Phase 3). Filtered by four callers
    # (indexing, reindex, /search/status, monitoring), so it earns an index.
    embedded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    __table_args__ = (UniqueConstraint("document_id", "ordinal"),)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contracts.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    issued_date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date] = mapped_column(Date)
    # Dashboard groups the financial summary by status.
    status: Mapped[str] = mapped_column(
        String(16), default="open", index=True
    )  # open|paid|overdue
