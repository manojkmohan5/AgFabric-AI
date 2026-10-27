"""Open-Meteo integration — a real third-party API, no key, no cost.

MET Norway is the fallback. Open-Meteo's free tier limits by IP address, and a
cloud host's shared outbound IPs can exhaust that before this app sends a single
request; MET Norway asks only that callers identify themselves.

An ETL path that is not file upload: fetch, validate, normalise, upsert. Written
to fail usefully, because a third-party API is the part of a pipeline most likely
to be slow, down, or to change shape underneath you:

- a bounded timeout, so a hanging upstream cannot pile up requests
- the response is validated, never trusted — a missing key returns a clear error
  rather than a KeyError three layers down
- upsert on (facility, date), so re-running is idempotent and a retry after a
  partial failure cannot double-write
"""

import logging
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Facility, WeatherObservation

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
MET_ENDPOINT = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
# MET Norway's terms: name the application and give a way to reach its owner.
MET_USER_AGENT = "AgFabric/0.1 https://github.com/manojkmohan5/AgFabric-AI"
DAILY_FIELDS = (
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "wind_speed_10m_max",
    "relative_humidity_2m_mean",
)

# Facility coordinates. Real deployments would store these on the row; the seeded
# facilities are fictional, so they live here rather than pretending otherwise.
FACILITY_COORDS: dict[str, tuple[float, float]] = {
    "Chicago Terminal": (41.8781, -87.6298),
    "New York Elevator": (40.7128, -74.0060),
    "Kansas City Terminal": (39.0997, -94.5786),
    "Toledo Elevator": (41.6528, -83.5379),
    "New Orleans Export": (29.9511, -90.0715),
    "Omaha River Terminal": (41.2565, -95.9345),
}


class WeatherError(Exception):
    """The upstream call failed or returned something unusable."""


def fetch(latitude: float, longitude: float, days: int = 3) -> dict[str, Any]:
    """Call Open-Meteo. Raises WeatherError on anything unusable."""
    try:
        response = httpx.get(
            ENDPOINT,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "daily": ",".join(DAILY_FIELDS),
                "timezone": "UTC",
                "forecast_days": max(1, min(days, 16)),
            },
            timeout=settings.http_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise WeatherError(f"open-meteo request failed: {exc}") from exc
    except ValueError as exc:
        raise WeatherError(f"open-meteo returned invalid JSON: {exc}") from exc

    daily = payload.get("daily")
    if not isinstance(daily, dict) or "time" not in daily:
        # Shape check up front: better one clear error here than a KeyError deep
        # in the transform when the API changes.
        raise WeatherError("open-meteo response has no daily.time array")
    return daily


def fetch_met_norway(latitude: float, longitude: float, days: int = 3) -> dict[str, Any]:
    """MET Norway's forecast in the same daily shape `fetch` returns."""
    try:
        response = httpx.get(
            MET_ENDPOINT,
            # MET serves at most four decimals and refuses requests with more.
            params={"lat": round(latitude, 4), "lon": round(longitude, 4)},
            headers={"User-Agent": MET_USER_AGENT},
            timeout=settings.http_timeout_seconds,
        )
        response.raise_for_status()
        series = response.json()["properties"]["timeseries"]
    except httpx.HTTPError as exc:
        raise WeatherError(f"met-norway request failed: {exc}") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise WeatherError(f"met-norway response is unusable: {exc}") from exc
    if not isinstance(series, list) or not series:
        raise WeatherError("met-norway response has no timeseries")
    return daily_from_hourly(series, days)


def daily_from_hourly(series: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Collapse MET's timeseries into Open-Meteo's daily arrays, by UTC day.

    Steps are hourly for about two and a half days, then six-hourly. Hourly
    steps carry next_1_hours and the later ones only next_6_hours, so taking
    whichever is present counts each stretch of rain once.
    """
    by_day: dict[str, dict[str, list[float]]] = {}
    for step in series:
        data = step.get("data") or {}
        now = (data.get("instant") or {}).get("details") or {}
        period = data.get("next_1_hours") or data.get("next_6_hours") or {}
        bucket = by_day.setdefault(
            str(step.get("time", ""))[:10], {"temp": [], "rain": [], "wind": [], "rh": []}
        )
        for key, value in (
            ("temp", now.get("air_temperature")),
            ("wind", now.get("wind_speed")),
            ("rh", now.get("relative_humidity")),
            ("rain", (period.get("details") or {}).get("precipitation_amount")),
        ):
            if isinstance(value, (int, float)):
                bucket[key].append(float(value))

    dates = sorted(by_day)[: max(1, min(days, 16))]
    buckets = [by_day[d] for d in dates]
    return {
        "time": dates,
        "temperature_2m_max": [max(b["temp"], default=None) for b in buckets],
        "temperature_2m_min": [min(b["temp"], default=None) for b in buckets],
        "precipitation_sum": [
            round(sum(b["rain"]), 1) if b["rain"] else None for b in buckets
        ],
        # MET reports m/s; the column, like Open-Meteo, is km/h.
        "wind_speed_10m_max": [
            round(max(b["wind"]) * 3.6, 1) if b["wind"] else None for b in buckets
        ],
        "relative_humidity_2m_mean": [
            round(sum(b["rh"]) / len(b["rh"]), 1) if b["rh"] else None for b in buckets
        ],
    }


def sync_facility(db: Session, facility: Facility, days: int = 3) -> int:
    """Fetch and upsert observations for one facility. Returns rows written."""
    coords = FACILITY_COORDS.get(facility.name)
    if coords is None:
        raise WeatherError(f"no coordinates configured for {facility.name!r}")

    try:
        daily, source = fetch(*coords, days=days), "open-meteo"
    except WeatherError as exc:
        logger.info(
            "open-meteo unavailable for %s (%s); using met-norway", facility.name, exc
        )
        daily, source = fetch_met_norway(*coords, days=days), "met-norway"
    dates = daily["time"]
    fetched_at = datetime.now(UTC)

    existing = {
        obs.observed_on: obs
        for obs in db.scalars(
            select(WeatherObservation).where(
                WeatherObservation.facility_id == facility.id
            )
        ).all()
    }

    written = 0
    for i, iso_day in enumerate(dates):
        try:
            observed_on = date.fromisoformat(iso_day)
        except (TypeError, ValueError):
            logger.warning("skipping unparseable date %r from %s", iso_day, source)
            continue

        values = {field: _at(daily.get(field), i) for field in DAILY_FIELDS}
        row = existing.get(observed_on)
        if row is None:
            row = WeatherObservation(facility_id=facility.id, observed_on=observed_on)
            db.add(row)
        # Upsert, so a re-run refreshes a forecast rather than duplicating it.
        row.source = source
        row.temp_max_c = values["temperature_2m_max"]
        row.temp_min_c = values["temperature_2m_min"]
        row.precipitation_mm = values["precipitation_sum"]
        row.wind_max_kmh = values["wind_speed_10m_max"]
        row.humidity_pct = values["relative_humidity_2m_mean"]
        row.fetched_at = fetched_at
        written += 1

    db.commit()
    return written


def sync_all(db: Session, days: int = 3) -> dict[str, Any]:
    """Sync every facility. One failure must not abort the rest."""
    facilities = db.scalars(select(Facility)).all()
    written = 0
    failures: list[dict[str, str]] = []
    for facility in facilities:
        try:
            written += sync_facility(db, facility, days=days)
        except WeatherError as exc:
            db.rollback()
            logger.warning("weather sync failed for %s: %s", facility.name, exc)
            failures.append({"facility": facility.name, "error": str(exc)})
    if facilities and len(failures) == len(facilities):
        # Nothing landed anywhere: report the run as failed, not as an "ok" that
        # leaves the forecast panel empty with no explanation.
        raise WeatherError(f"every facility failed; first: {failures[0]['error']}")
    return {
        "facilities": len(facilities),
        "observations_written": written,
        "failures": failures,
    }


def _at(series: Any, index: int) -> float | None:
    """Read series[index] as a float, tolerating nulls and short arrays.

    Open-Meteo returns parallel arrays and uses null for unavailable values, so a
    field can be absent or shorter than daily.time.
    """
    if not isinstance(series, list) or index >= len(series):
        return None
    value = series[index]
    return float(value) if isinstance(value, (int, float)) else None
