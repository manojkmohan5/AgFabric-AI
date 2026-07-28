"""Grain futures prices, position exposure, and mark-to-market.

Two halves, deliberately separated:

  * **I/O** — `YahooMarket` / `FakeMarket` behind one seam, same auto/real/fake
    pattern as embeddings, chat, OCR and weather. Free, no API key.
  * **Pure math** — `position_summary` and `mark_to_market` take plain rows and
    return plain dicts, so every figure below is unit-tested without a database
    or a network call.

Scope boundary, stated because it matters: this computes **exposure and
valuation**, and raises **alerts on movement**. It does not recommend hedge
ratios, size a hedge, or place an order. Those need a merchandiser's judgement
and live brokerage integration; a plausible-looking guess would be worse than
the honest gap.

Units: CBOT quotes grains in cents per bushel and Yahoo reports that as currency
`USX`. The conversion to dollars is driven off that field rather than hardcoded,
and happens once at ingest — see `models.MarketPrice`.

Reliability caveat, worth knowing before relying on it: Yahoo's chart endpoint is
a public but *unofficial* API. It needs no key, which is why it is here, but it
can rate-limit or change shape without notice. Every failure path below degrades
rather than raising, and the fake provider means nothing in the test suite
depends on it being up.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Commodity, Contract, Customer, Delivery, MarketPrice

logger = logging.getLogger(__name__)

ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Commodity name -> CBOT continuous front-month symbol. Only the three the seed
# uses; an unmapped commodity is skipped rather than guessed at.
SYMBOLS: dict[str, str] = {
    "Corn": "ZC=F",
    "Soybeans": "ZS=F",
    "Wheat": "ZW=F",
}

# Yahoo blocks requests with no user agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AgFabric/0.1)"}


class MarketError(Exception):
    """The upstream call failed or returned something unusable."""


@dataclass(frozen=True)
class Quote:
    quoted_on: date
    close_usd_per_bu: float


def to_usd_per_bu(value: float, currency: str) -> float:
    """Normalise a quote to dollars per bushel.

    `USX` is US cents — the CBOT grain convention. Anything already in USD passes
    through. An unrecognised currency raises rather than silently being off by
    100×, which is the failure mode that would quietly wreck every valuation.
    """
    unit = (currency or "").upper()
    if unit in ("USX", "USC"):
        return round(value / 100.0, 4)
    if unit in ("USD", ""):
        return round(value, 4)
    raise MarketError(f"unexpected quote currency {currency!r}")


class MarketProvider(Protocol):
    name: str

    def history(self, symbol: str, days: int) -> list[Quote]: ...


class FakeMarket:
    """Deterministic prices. No network, no cost, stable across processes.

    Values are in the right ballpark for each grain so the derived figures — basis,
    exposure, alerts — are plausible in tests rather than nonsense.
    """

    name = "fake"
    BASE: dict[str, float] = {"ZC=F": 4.75, "ZS=F": 12.13, "ZW=F": 6.74}

    def history(self, symbol: str, days: int) -> list[Quote]:
        base = self.BASE.get(symbol, 5.0)
        today = datetime.now(UTC).date()
        # A gentle deterministic wave, so a trend exists without randomness.
        return [
            Quote(
                quoted_on=today - timedelta(days=days - 1 - i),
                close_usd_per_bu=round(base * (1 + 0.01 * ((i % 5) - 2)), 4),
            )
            for i in range(days)
        ]


class YahooMarket:
    name = "yahoo"

    def history(self, symbol: str, days: int) -> list[Quote]:
        try:
            response = httpx.get(
                ENDPOINT.format(symbol=symbol),
                params={"range": f"{max(days, 5)}d", "interval": "1d"},
                headers=HEADERS,
                timeout=settings.http_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise MarketError(f"yahoo request failed for {symbol}: {exc}") from exc
        except ValueError as exc:
            raise MarketError(f"yahoo returned invalid JSON for {symbol}: {exc}") from exc

        return parse_chart(payload, days)


def parse_chart(payload: dict[str, Any], days: int) -> list[Quote]:
    """Turn a Yahoo chart response into quotes. Pure, so it is unit-tested.

    Shape is validated up front: a changed API gives one clear error rather than a
    KeyError deep in the transform.
    """
    try:
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MarketError(f"unexpected yahoo chart shape: {exc}") from exc

    currency = meta.get("currency", "")
    quotes: list[Quote] = []
    for stamp, close in zip(stamps, closes, strict=False):
        # Yahoo pads non-trading days with nulls; skip rather than interpolate.
        if close is None or stamp is None:
            continue
        quotes.append(
            Quote(
                quoted_on=datetime.fromtimestamp(stamp, UTC).date(),
                close_usd_per_bu=to_usd_per_bu(float(close), currency),
            )
        )
    if not quotes:
        raise MarketError("yahoo returned no usable closes")
    return quotes[-days:]


def get_provider() -> MarketProvider:
    provider = settings.market_provider.lower()
    if provider == "auto":
        provider = "yahoo" if settings.enable_live_market else "fake"
    if provider == "fake":
        return FakeMarket()
    if provider == "yahoo":
        return YahooMarket()
    raise MarketError(f"unknown MARKET_PROVIDER {provider!r}; use auto|yahoo|fake")


def sync_all(db: Session, days: int = 10) -> dict[str, Any]:
    """Fetch and upsert prices for every mapped commodity.

    One commodity failing must not abort the rest — a missing wheat quote should
    not cost you the corn ones.
    """
    provider = get_provider()
    commodities = {c.name: c for c in db.scalars(select(Commodity)).all()}
    fetched_at = datetime.now(UTC)

    written = 0
    failures: list[dict[str, str]] = []
    skipped: list[str] = []

    for name, commodity in commodities.items():
        symbol = SYMBOLS.get(name)
        if symbol is None:
            skipped.append(name)
            continue
        try:
            quotes = provider.history(symbol, days)
        except MarketError as exc:
            db.rollback()
            logger.warning("market sync failed for %s (%s): %s", name, symbol, exc)
            failures.append({"commodity": name, "symbol": symbol, "error": str(exc)})
            continue

        existing = {
            row.quoted_on: row
            for row in db.scalars(
                select(MarketPrice).where(MarketPrice.symbol == symbol)
            ).all()
        }
        for quote in quotes:
            row = existing.get(quote.quoted_on)
            if row is None:
                row = MarketPrice(
                    symbol=symbol, quoted_on=quote.quoted_on, commodity_id=commodity.id
                )
                db.add(row)
            # Upsert, so a re-run refreshes a settlement rather than duplicating it.
            row.close_usd_per_bu = quote.close_usd_per_bu
            row.commodity_id = commodity.id
            row.source = provider.name
            row.fetched_at = fetched_at
            written += 1
        db.commit()

    return {
        "provider": provider.name,
        "commodities": len(commodities) - len(skipped),
        "prices_written": written,
        "unmapped_commodities": skipped,
        "failures": failures,
    }


# ------------------------------------------------------------------ pure math

# An elevator BUYS from farmers and SELLS to buyers, so the sign of an open
# contract's exposure depends which side it is. Getting this backwards would
# invert every number on the page, so it is named rather than inlined.
PURCHASE = "farmer"


def mark_to_market(
    contracts: list[dict[str, Any]], prices: dict[int, float]
) -> list[dict[str, Any]]:
    """Value each open contract's undelivered balance against the current market.

    `contracts` rows carry: number, commodity_id, commodity, customer, side,
    quantity_bu, delivered_bu, price_per_bu.

    A purchase committed at P is favourable when the market M is above it (you buy
    something worth M for P), so unrealised = (M - P) x remaining. A sale is the
    mirror. Undelivered balance only — delivered bushels are already settled.
    """
    valued: list[dict[str, Any]] = []
    for c in contracts:
        market = prices.get(c["commodity_id"])
        if market is None:
            continue
        remaining = max(0.0, float(c["quantity_bu"]) - float(c["delivered_bu"]))
        if remaining == 0:
            continue
        contracted = float(c["price_per_bu"])
        delta = market - contracted
        unrealised = delta * remaining * (1 if c["side"] == PURCHASE else -1)
        valued.append(
            {
                **c,
                "remaining_bu": round(remaining, 2),
                "market_usd_per_bu": round(market, 4),
                "basis_usd_per_bu": round(delta, 4),
                "unrealised_usd": round(unrealised, 2),
            }
        )
    return valued


def position_summary(
    valued: list[dict[str, Any]], prices: dict[int, float]
) -> list[dict[str, Any]]:
    """Net open position per commodity, in bushels and dollars.

    Net long means more undelivered purchases than sales — grain owed to you that
    you have not yet committed to sell, so a price fall costs you. Net short is
    the reverse. This is the number a merchandiser actually watches.
    """
    by_commodity: dict[int, dict[str, Any]] = {}
    for c in valued:
        entry = by_commodity.setdefault(
            c["commodity_id"],
            {
                "commodity_id": c["commodity_id"],
                "commodity": c["commodity"],
                "market_usd_per_bu": prices.get(c["commodity_id"]),
                "long_bu": 0.0,
                "short_bu": 0.0,
                "unrealised_usd": 0.0,
                "open_contracts": 0,
            },
        )
        if c["side"] == PURCHASE:
            entry["long_bu"] += c["remaining_bu"]
        else:
            entry["short_bu"] += c["remaining_bu"]
        entry["unrealised_usd"] += c["unrealised_usd"]
        entry["open_contracts"] += 1

    summary = []
    for entry in by_commodity.values():
        net = entry["long_bu"] - entry["short_bu"]
        summary.append(
            {
                **entry,
                "long_bu": round(entry["long_bu"], 2),
                "short_bu": round(entry["short_bu"], 2),
                "net_bu": round(net, 2),
                "direction": "long" if net > 0 else "short" if net < 0 else "flat",
                "unrealised_usd": round(entry["unrealised_usd"], 2),
            }
        )
    return sorted(summary, key=lambda s: -abs(s["net_bu"]))


def load_open_contracts(db: Session) -> list[dict[str, Any]]:
    """Open contracts with their delivered balance, shaped for the math above."""
    delivered: dict[int, float] = {}
    for contract_id, net in db.execute(
        select(Delivery.contract_id, Delivery.net_bu).where(
            Delivery.contract_id.is_not(None)
        )
    ).all():
        delivered[contract_id] = delivered.get(contract_id, 0.0) + float(net)

    commodities = {c.id: c.name for c in db.scalars(select(Commodity)).all()}
    customers = {c.id: c for c in db.scalars(select(Customer)).all()}

    rows: list[dict[str, Any]] = []
    for c in db.scalars(select(Contract).where(Contract.status == "open")).all():
        customer = customers.get(c.customer_id)
        rows.append(
            {
                "number": c.number,
                "commodity_id": c.commodity_id,
                "commodity": commodities.get(c.commodity_id, "unknown"),
                "customer": customer.name if customer else "unknown",
                "side": customer.kind if customer else "unknown",
                "quantity_bu": float(c.quantity_bu),
                "delivered_bu": delivered.get(c.id, 0.0),
                "price_per_bu": float(c.price_per_bu),
                "end_date": c.end_date.isoformat(),
            }
        )
    return rows


def latest_prices(db: Session) -> dict[int, float]:
    """Most recent close per commodity id."""
    prices: dict[int, float] = {}
    for row in db.scalars(
        select(MarketPrice).order_by(MarketPrice.quoted_on.asc())
    ).all():
        if row.commodity_id is not None:
            # Ascending order means the last write per commodity is the newest.
            prices[row.commodity_id] = row.close_usd_per_bu
    return prices
