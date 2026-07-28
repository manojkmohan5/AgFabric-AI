"""Inbound webhooks: event-driven ingestion from external systems.

This is the path an ERP or scale system uses to push a delivery as it happens,
rather than waiting for someone to upload a file.

Four properties a webhook endpoint needs, and why:

1. **Authenticated.** The URL is public, so anything unsigned is anonymous
   write access. Verified with HMAC-SHA256 over the raw body, compared in
   constant time.
2. **Idempotent.** Senders retry on timeout. A unique `dedupe_key` makes a
   replay a no-op that returns the original result instead of a second delivery.
3. **Durable before processed.** The event is committed on arrival, then handled.
   A handler crash loses nothing and the event can be replayed.
4. **Fast to accept.** Validate, store, acknowledge. Anything slow belongs in an
   agent, not in the request the sender is waiting on.

ponytail: handlers run inline after the event is committed, because they are two
small upserts. Move them to the Celery queue if handler work grows.
"""

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import metrics
from .config import settings
from .db import get_db
from .models import (
    Commodity,
    Contract,
    Customer,
    Delivery,
    Facility,
    WebhookEvent,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

DbDep = Annotated[Session, Depends(get_db)]

MAX_BODY_BYTES = 256 * 1024
SUPPORTED_EVENTS = ("delivery.recorded", "delivery.verified")


def verify_signature(raw_body: bytes, provided: str | None) -> None:
    """Constant-time HMAC check over the raw bytes.

    The raw body matters: re-serialising parsed JSON would change byte order and
    whitespace, and the signature would never match.
    """
    if not settings.webhook_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "webhook receiving is not configured (WEBHOOK_SECRET unset)",
        )
    if not provided:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing X-AgFabric-Signature header"
        )
    expected = hmac.new(
        settings.webhook_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    # Accept an optional "sha256=" prefix, as most senders emit.
    candidate = provided.split("=", 1)[-1].strip()
    if not hmac.compare_digest(expected, candidate):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "signature mismatch")


@router.post("/deliveries", status_code=status.HTTP_202_ACCEPTED)
async def receive_delivery(
    request: Request,
    db: DbDep,
    signature: Annotated[str | None, Header(alias="X-AgFabric-Signature")] = None,
    event_id: Annotated[str | None, Header(alias="X-AgFabric-Event-Id")] = None,
) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "webhook body too large"
        )
    verify_signature(raw, signature)

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"body is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")

    event_type = payload.get("event")
    if event_type not in SUPPORTED_EVENTS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unsupported event {event_type!r}; supported: {', '.join(SUPPORTED_EVENTS)}",
        )

    # Prefer the sender's id; fall back to a body hash so an unversioned sender
    # still gets replay protection.
    dedupe_key = event_id or hashlib.sha256(raw).hexdigest()

    event = WebhookEvent(
        source=str(payload.get("source", "unknown"))[:64],
        event_type=event_type,
        dedupe_key=dedupe_key,
        payload=payload,
        status="received",
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # Already seen. Report the original outcome rather than reprocessing.
        db.rollback()
        prior = db.scalar(
            select(WebhookEvent).where(WebhookEvent.dedupe_key == dedupe_key)
        )
        return {
            "duplicate": True,
            "event_id": prior.id if prior else None,
            "status": prior.status if prior else "unknown",
        }

    # Durable now, so a handler failure is recoverable rather than lost.
    try:
        result = _handle(db, event)
        event.status = result.pop("status", "processed")
        event.processed_at = datetime.now(UTC)
    except Exception as exc:
        db.rollback()
        event.status = "failed"
        event.error = str(exc)[:500]
        event.processed_at = datetime.now(UTC)
        result = {"error": event.error}
        logger.exception("webhook %s handler failed", event.id)
    db.commit()
    metrics.webhook_events_total.labels(event.event_type, event.status).inc()

    return {
        "duplicate": False,
        "event_id": event.id,
        "status": event.status,
        "result": result,
    }


def _handle(db: Session, event: WebhookEvent) -> dict[str, Any]:
    if event.event_type == "delivery.recorded":
        return _record_delivery(db, event.payload)
    return _verify_delivery(db, event.payload)


def _record_delivery(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("payload.data must be an object")

    ticket = str(data.get("ticket_number") or "").strip()
    if not ticket:
        raise ValueError("data.ticket_number is required")

    # Idempotent at the business level too, not only at the event level.
    if db.scalar(select(Delivery).where(Delivery.ticket_number == ticket)):
        return {"status": "ignored", "reason": f"delivery {ticket} already exists"}

    customer = db.scalar(
        select(Customer).where(Customer.name == str(data.get("customer", "")))
    )
    commodity = db.scalar(
        select(Commodity).where(Commodity.name == str(data.get("commodity", "")))
    )
    facility = db.scalar(
        select(Facility).where(Facility.name == str(data.get("facility", "")))
    )
    missing = [
        name
        for name, value in (
            ("customer", customer),
            ("commodity", commodity),
            ("facility", facility),
        )
        if value is None
    ]
    if missing:
        # Reject rather than inventing rows: a typo'd customer name must not
        # silently create a new customer.
        raise ValueError(f"unknown {', '.join(missing)} in payload")

    gross = _decimal(data, "gross_lbs")
    tare = _decimal(data, "tare_lbs")
    if tare >= gross:
        raise ValueError("tare_lbs must be less than gross_lbs")

    net_bu = ((gross - tare) / Decimal(commodity.lbs_per_bu)).quantize(Decimal("0.01"))
    contract = db.scalar(
        select(Contract).where(
            Contract.customer_id == customer.id,
            Contract.commodity_id == commodity.id,
            Contract.status == "open",
        )
    )

    db.add(
        Delivery(
            ticket_number=ticket,
            contract_id=contract.id if contract else None,
            customer_id=customer.id,
            commodity_id=commodity.id,
            facility_id=facility.id,
            truck_id=str(data.get("truck_id", "unknown"))[:32],
            gross_lbs=gross,
            tare_lbs=tare,
            net_bu=net_bu,
            moisture_pct=_decimal(data, "moisture_pct", default=Decimal("0")),
            delivered_at=_timestamp(data.get("delivered_at")),
            verified=False,
        )
    )
    db.flush()
    return {
        "status": "processed",
        "ticket": ticket,
        "net_bu": str(net_bu),
        "contract": contract.number if contract else None,
    }


def _verify_delivery(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("payload.data must be an object")
    ticket = str(data.get("ticket_number") or "").strip()
    delivery = db.scalar(select(Delivery).where(Delivery.ticket_number == ticket))
    if delivery is None:
        raise ValueError(f"no delivery with ticket {ticket!r}")
    if delivery.verified:
        return {"status": "ignored", "reason": "already verified"}
    delivery.verified = True
    db.flush()
    return {"status": "processed", "ticket": ticket, "verified": True}


def _decimal(data: dict[str, Any], key: str, default: Decimal | None = None) -> Decimal:
    raw = data.get(key)
    if raw is None:
        if default is not None:
            return default
        raise ValueError(f"data.{key} is required")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"data.{key} is not a number: {raw!r}") from exc
    if value < 0:
        raise ValueError(f"data.{key} must not be negative")
    return value


def _timestamp(raw: Any) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"data.delivered_at is not ISO-8601: {raw!r}") from exc
    # Naive input is assumed UTC rather than rejected; senders vary.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
