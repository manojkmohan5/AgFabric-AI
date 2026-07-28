"""Two more free, keyless third-party feeds: FX rates and agricultural news.

Same shape as `weather.py` and `market.py` — provider seam, pure parse function,
idempotent upsert, one failure never taking the others down. Both are grouped here
because each is small and they share that skeleton; splitting them into two files
would duplicate it for no benefit.

**Why these sources.** Both are free with no signup and no API key, which is the
binding constraint:

  FX    `open.er-api.com` — no key. (Frankfurter was the first choice for being
        ECB-backed, but its old host now 301s, so it is not a dependency worth
        taking.)
  News  Google News RSS — no key, and the query returns real trade press (AgWeb,
        CME Group, university extension) rather than generic wire copy. USDA's own
        RSS sits behind an antibot page, so it is not usable unattended.

Why FX belongs in a grain platform at all: US grain is priced in dollars, so the
dollar's strength against BRL (Brazil's competing soybean crop), CNY (the largest
buyer) and ARS directly moves export competitiveness. Every rate here is quoted
USD-as-base, so it reads as "what a dollar buys" — the exporter's direction.

Neither feed is a system of record. Both degrade to "no data yet" rather than
erroring, and both have a fake provider so the check suite never touches the
network.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from defusedxml import ElementTree as ET
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import FxRate, NewsItem

logger = logging.getLogger(__name__)

FX_ENDPOINT = "https://open.er-api.com/v6/latest/USD"
NEWS_ENDPOINT = "https://news.google.com/rss/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AgFabric/0.1)"}

# The currencies that actually move US grain export competitiveness, not a
# generic top-ten list.
TRACKED_CURRENCIES: dict[str, str] = {
    "BRL": "Brazil — competing soybean and corn crop",
    "CNY": "China — largest single grain buyer",
    "ARS": "Argentina — competing corn and soymeal",
    "EUR": "Euro area — wheat import demand",
    "CAD": "Canada — competing wheat and canola",
    "MXN": "Mexico — largest US corn customer",
}

# Query kept narrow on purpose; a broad "agriculture" search returns policy and
# lifestyle copy rather than anything a merchandiser would act on.
NEWS_QUERY = "grain market corn soybeans wheat prices"
NEWS_TOPIC = "grain-market"


class FeedError(Exception):
    """An upstream feed failed or returned something unusable."""


# ---------------------------------------------------------------------- FX


@dataclass(frozen=True)
class Rate:
    currency: str
    rate: float
    quoted_on: date


def parse_fx(payload: dict[str, Any]) -> list[Rate]:
    """Turn an open-er-api response into rates. Pure, so it is unit-tested."""
    if payload.get("result") != "success":
        raise FeedError(f"fx provider reported {payload.get('result')!r}")
    rates = payload.get("rates")
    if not isinstance(rates, dict):
        raise FeedError("fx response has no rates object")

    stamp = payload.get("time_last_update_unix")
    quoted_on = (
        datetime.fromtimestamp(stamp, UTC).date()
        if isinstance(stamp, (int, float))
        else datetime.now(UTC).date()
    )

    parsed: list[Rate] = []
    for currency in TRACKED_CURRENCIES:
        value = rates.get(currency)
        # A missing or non-numeric rate is skipped, never coerced to zero — a
        # zero rate would read as "the dollar buys nothing".
        if not isinstance(value, (int, float)) or value <= 0:
            logger.warning("fx: skipping %s, unusable value %r", currency, value)
            continue
        parsed.append(
            Rate(currency=currency, rate=round(float(value), 6), quoted_on=quoted_on)
        )
    if not parsed:
        raise FeedError("fx response contained none of the tracked currencies")
    return parsed


class FakeFx:
    name = "fake"
    RATES = {
        "BRL": 5.086098,
        "CNY": 6.777732,
        "ARS": 1494.3744,
        "EUR": 0.877986,
        "CAD": 1.408746,
        "MXN": 17.450009,
    }

    def latest(self) -> list[Rate]:
        today = datetime.now(UTC).date()
        return [Rate(c, r, today) for c, r in self.RATES.items()]


class LiveFx:
    name = "open-er-api"

    def latest(self) -> list[Rate]:
        try:
            response = httpx.get(
                FX_ENDPOINT, headers=HEADERS, timeout=settings.http_timeout_seconds
            )
            response.raise_for_status()
            return parse_fx(response.json())
        except httpx.HTTPError as exc:
            raise FeedError(f"fx request failed: {exc}") from exc
        except ValueError as exc:
            raise FeedError(f"fx returned invalid JSON: {exc}") from exc


def fx_provider() -> FakeFx | LiveFx:
    provider = settings.feeds_provider.lower()
    if provider == "auto":
        provider = "live" if settings.enable_live_feeds else "fake"
    return LiveFx() if provider == "live" else FakeFx()


def sync_fx(db: Session) -> dict[str, Any]:
    """Fetch and upsert today's rates. Idempotent on (currency, date)."""
    provider = fx_provider()
    try:
        rates = provider.latest()
    except FeedError as exc:
        db.rollback()
        logger.warning("fx sync failed: %s", exc)
        return {"provider": provider.name, "rates_written": 0, "error": str(exc)}

    fetched_at = datetime.now(UTC)
    existing = {
        (row.quote_currency, row.quoted_on): row
        for row in db.scalars(select(FxRate)).all()
    }

    written = 0
    for rate in rates:
        row = existing.get((rate.currency, rate.quoted_on))
        if row is None:
            row = FxRate(
                base_currency="USD",
                quote_currency=rate.currency,
                quoted_on=rate.quoted_on,
            )
            db.add(row)
        row.rate = rate.rate
        row.source = provider.name
        row.fetched_at = fetched_at
        written += 1
    db.commit()
    return {"provider": provider.name, "rates_written": written, "error": None}


def fx_with_change(db: Session) -> list[dict[str, Any]]:
    """Latest rate per currency plus the change against the previous quote.

    The delta is what makes a rate meaningful — 5.08 BRL means nothing on its own,
    "+0.4% since yesterday" is the signal. Currencies with only one observation
    report a null change rather than a fabricated zero.
    """
    history: dict[str, list[FxRate]] = {}
    for row in db.scalars(select(FxRate).order_by(FxRate.quoted_on.asc())).all():
        history.setdefault(row.quote_currency, []).append(row)

    out: list[dict[str, Any]] = []
    for currency, note in TRACKED_CURRENCIES.items():
        rows = history.get(currency)
        if not rows:
            continue
        latest = rows[-1]
        previous = rows[-2] if len(rows) > 1 else None
        change_pct = (
            round((latest.rate - previous.rate) / previous.rate * 100, 3)
            if previous and previous.rate
            else None
        )
        out.append(
            {
                "currency": currency,
                "note": note,
                "rate": latest.rate,
                "quoted_on": latest.quoted_on.isoformat(),
                "change_pct": change_pct,
                "direction": (
                    "flat"
                    if change_pct is None or abs(change_pct) < 0.01
                    else "up"
                    if change_pct > 0
                    else "down"
                ),
            }
        )
    return out


# -------------------------------------------------------------------- news


@dataclass(frozen=True)
class Headline:
    guid: str
    title: str
    url: str
    publisher: str | None
    published_at: datetime | None


def parse_rss(xml_text: str, limit: int = 25) -> list[Headline]:
    """Parse an RSS channel into headlines. Pure, so it is unit-tested.

    `defusedxml` rather than stdlib ElementTree: this parses XML fetched from a
    remote host, and plain ElementTree is vulnerable to entity-expansion ("billion
    laughs") attacks that can exhaust memory. defusedxml is ~30KB and drops in
    with the same API, so there is no reason to take the risk.

    Still no feed library — RSS is four fields deep, and parsing it directly is
    less code than configuring a dependency to do it.
    """
    try:
        root = ET.fromstring(xml_text)
    except Exception as exc:
        raise FeedError(f"news feed is not valid XML: {exc}") from exc

    items = root.findall(".//item")
    if not items:
        raise FeedError("news feed contained no items")

    headlines: list[Headline] = []
    for item in items[:limit]:
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        # Without a title or a link there is nothing to show or click.
        if not title or not url:
            continue
        guid = (item.findtext("guid") or url).strip()[:255]
        source = item.find("source")
        published: datetime | None = None
        raw_date = item.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                # A bad date must not drop an otherwise good headline.
                logger.warning("news: unparseable pubDate %r", raw_date)
        headlines.append(
            Headline(
                guid=guid,
                title=title[:512],
                url=url[:1024],
                publisher=(source.text or "").strip()[:128]
                if source is not None
                else None,
                published_at=published,
            )
        )
    if not headlines:
        raise FeedError("news feed had items but none were usable")
    return headlines


class FakeNews:
    name = "fake"
    ITEMS = [
        ("Soybeans make contract highs on weather and China demand", "AgWeb"),
        (
            "Local basis strength could yield maximum profits on stored grain",
            "MSU Extension",
        ),
        ("Chicago SRW wheat settles higher on export optimism", "CME Group"),
        ("Corn harvest pace ahead of five-year average", "Successful Farming"),
    ]

    def latest(self, limit: int = 25) -> list[Headline]:
        now = datetime.now(UTC)
        return [
            Headline(
                guid=f"fake-news-{i}",
                title=title,
                url=f"https://example.test/news/{i}",
                publisher=publisher,
                published_at=now,
            )
            for i, (title, publisher) in enumerate(self.ITEMS[:limit])
        ]


class LiveNews:
    name = "google-news-rss"

    def latest(self, limit: int = 25) -> list[Headline]:
        try:
            response = httpx.get(
                NEWS_ENDPOINT,
                params={
                    "q": NEWS_QUERY,
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                },
                headers=HEADERS,
                timeout=settings.http_timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FeedError(f"news request failed: {exc}") from exc
        return parse_rss(response.text, limit=limit)


def news_provider() -> FakeNews | LiveNews:
    provider = settings.feeds_provider.lower()
    if provider == "auto":
        provider = "live" if settings.enable_live_feeds else "fake"
    return LiveNews() if provider == "live" else FakeNews()


def sync_news(db: Session, limit: int = 25) -> dict[str, Any]:
    """Fetch headlines and insert the ones not already stored.

    Deduped on `guid`, so polling every hour adds only genuinely new items rather
    than one copy of the whole feed per tick.
    """
    provider = news_provider()
    try:
        headlines = provider.latest(limit=limit)
    except FeedError as exc:
        db.rollback()
        logger.warning("news sync failed: %s", exc)
        return {"provider": provider.name, "new_items": 0, "seen": 0, "error": str(exc)}

    known = set(
        db.scalars(
            select(NewsItem.guid).where(NewsItem.guid.in_([h.guid for h in headlines]))
        ).all()
    )
    fetched_at = datetime.now(UTC)
    added = 0
    for headline in headlines:
        if headline.guid in known:
            continue
        db.add(
            NewsItem(
                guid=headline.guid,
                title=headline.title,
                url=headline.url,
                publisher=headline.publisher,
                published_at=headline.published_at,
                topic=NEWS_TOPIC,
                fetched_at=fetched_at,
            )
        )
        added += 1
    db.commit()
    return {
        "provider": provider.name,
        "new_items": added,
        "seen": len(headlines),
        "error": None,
    }


def recent_news(db: Session, limit: int = 12) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(NewsItem)
        .order_by(NewsItem.published_at.desc().nullslast(), NewsItem.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "title": r.title,
            "url": r.url,
            "publisher": r.publisher,
            "published_at": r.published_at.isoformat() if r.published_at else None,
        }
        for r in rows
    ]
