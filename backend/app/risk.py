"""Risk rules. Each returns a list of alert dicts carrying confidence,
evidence, and a recommendation, per the Explainable AI requirement.

Rules take plain sequences, not a Session, so they run against seeded rows or
a test list identically.
"""

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol


class InvoiceLike(Protocol):
    number: str
    customer_id: int
    amount: Decimal
    issued_date: date


class BinLike(Protocol):
    name: str
    capacity_bu: Decimal
    current_bu: Decimal
    moisture_pct: Decimal | None


DUPLICATE_WINDOW = timedelta(days=30)
MOISTURE_CEILING = Decimal("15.0")
# An open contract this far past its start with nothing delivered is suspect.
MISSING_DELIVERY_GRACE = timedelta(days=45)
# Net bushels are derived from weights, so a mismatch means a data error.
NET_BU_TOLERANCE = Decimal("0.5")
# Net open position worth telling a merchandiser about. A round lot of corn is
# 5,000 bu, so this is roughly "more than four lots exposed".
UNHEDGED_THRESHOLD_BU = Decimal("20000")
# How far an open contract may sit from the board before it is worth a look.
OFFMARKET_TOLERANCE_PCT = Decimal("15")


def fingerprint(kind: str, *parts: object) -> str:
    """Stable natural key for an alert condition.

    The same underlying problem must produce the same fingerprint on every scan,
    so re-running a scan updates one alert rather than creating a duplicate each
    time. Derived only from identity, never from confidence or wording.
    """
    joined = "|".join([kind, *(str(p) for p in parts)])
    return hashlib.blake2b(joined.encode(), digest_size=16).hexdigest()


def duplicate_invoices(
    invoices: Sequence[InvoiceLike], window: timedelta = DUPLICATE_WINDOW
) -> list[dict[str, Any]]:
    """Same customer, same amount, issued within `window` of each other.

    ponytail: groups in Python, O(n log n) per customer/amount bucket. Push to
    a SQL self-join if invoice volume outgrows a single fetch.
    """
    buckets: dict[tuple[int, Decimal], list[InvoiceLike]] = defaultdict(list)
    for inv in invoices:
        buckets[(inv.customer_id, Decimal(inv.amount))].append(inv)

    alerts: list[dict[str, Any]] = []
    for (customer_id, amount), group in buckets.items():
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda i: i.issued_date)
        for earlier, later in zip(group, group[1:], strict=False):
            gap = later.issued_date - earlier.issued_date
            if gap > window:
                continue
            # Same-day duplicates are near-certain; confidence decays as the
            # gap widens toward the window edge (a real recurring monthly
            # charge is legitimate).
            confidence = round(0.95 - 0.45 * (gap / window), 2)
            alerts.append(
                {
                    "kind": "duplicate_invoice",
                    # Sorted, so the pair keys the same regardless of scan order.
                    "fingerprint": fingerprint(
                        "duplicate_invoice", *sorted([earlier.number, later.number])
                    ),
                    "severity": "high" if gap.days <= 7 else "medium",
                    "title": f"Possible duplicate invoice for ${amount:,.2f}",
                    "confidence": confidence,
                    "evidence": {
                        "customer_id": customer_id,
                        "amount": str(amount),
                        "invoices": [earlier.number, later.number],
                        "days_apart": gap.days,
                    },
                    "recommendation": (
                        f"Compare {earlier.number} and {later.number} before payment; "
                        "void one if the same delivery is billed twice."
                    ),
                }
            )
    return alerts


def bin_anomalies(bins: Sequence[BinLike]) -> list[dict[str, Any]]:
    """Over-capacity bins and moisture above the safe storage ceiling."""
    alerts: list[dict[str, Any]] = []
    for b in bins:
        if Decimal(b.current_bu) > Decimal(b.capacity_bu):
            over = Decimal(b.current_bu) - Decimal(b.capacity_bu)
            alerts.append(
                {
                    "kind": "inventory_mismatch",
                    "fingerprint": fingerprint("inventory_mismatch", b.name),
                    "severity": "high",
                    "title": f"Bin {b.name} recorded above capacity",
                    "confidence": 0.99,
                    "evidence": {
                        "bin": b.name,
                        "capacity_bu": str(b.capacity_bu),
                        "current_bu": str(b.current_bu),
                        "over_by_bu": str(over),
                    },
                    "recommendation": (
                        "Recount the bin or check for unposted shipments — book "
                        "inventory exceeds physical capacity."
                    ),
                }
            )
        if b.moisture_pct is not None and Decimal(b.moisture_pct) > MOISTURE_CEILING:
            alerts.append(
                {
                    "kind": "moisture_anomaly",
                    "fingerprint": fingerprint("moisture_anomaly", b.name),
                    "severity": "medium",
                    "title": f"Bin {b.name} moisture at {b.moisture_pct}%",
                    "confidence": 0.9,
                    "evidence": {
                        "bin": b.name,
                        "moisture_pct": str(b.moisture_pct),
                        "ceiling_pct": str(MOISTURE_CEILING),
                    },
                    "recommendation": "Schedule aeration or drying to avoid spoilage.",
                }
            )
    return alerts


def missing_deliveries(
    contracts: Sequence[Any],
    deliveries: Sequence[Any],
    today: date,
    grace: timedelta = MISSING_DELIVERY_GRACE,
) -> list[dict[str, Any]]:
    """Open contracts well past their start date with nothing delivered against them.

    Only flags contracts whose window has actually opened — a contract starting
    next month having no deliveries is normal, not a risk.
    """
    delivered = {d.contract_id for d in deliveries if d.contract_id is not None}
    alerts: list[dict[str, Any]] = []
    for c in contracts:
        if c.status != "open" or c.id in delivered:
            continue
        elapsed = today - c.start_date
        if elapsed < grace:
            continue
        # Nearer the end date with nothing shipped is worse.
        window = max((c.end_date - c.start_date).days, 1)
        progress = min(1.0, elapsed.days / window)
        alerts.append(
            {
                "kind": "missing_deliveries",
                "fingerprint": fingerprint("missing_deliveries", c.number),
                "severity": "high" if progress > 0.75 else "medium",
                "title": (
                    f"Contract {c.number} has no deliveries after {elapsed.days} days"
                ),
                "confidence": 0.85,
                "evidence": {
                    "contract": c.number,
                    "start_date": c.start_date.isoformat(),
                    "end_date": c.end_date.isoformat(),
                    "days_since_start": elapsed.days,
                    "window_elapsed_pct": round(progress * 100, 1),
                    "quantity_bu": str(c.quantity_bu),
                },
                "recommendation": (
                    "Confirm with the customer whether this contract will ship, or "
                    "check whether deliveries were booked against the wrong contract."
                ),
            }
        )
    return alerts


def data_inconsistencies(
    deliveries: Sequence[Any],
    lbs_per_bu: dict[int, Decimal],
    tolerance: Decimal = NET_BU_TOLERANCE,
) -> list[dict[str, Any]]:
    """Deliveries whose recorded figures contradict each other.

    net_bu is derived from (gross - tare) / lbs_per_bu, so a mismatch means either
    a scale error or a bad manual edit. Also catches tare >= gross, which is
    physically impossible.
    """
    alerts: list[dict[str, Any]] = []
    for d in deliveries:
        gross, tare = Decimal(d.gross_lbs), Decimal(d.tare_lbs)

        if tare >= gross:
            alerts.append(
                {
                    "kind": "data_inconsistency",
                    "fingerprint": fingerprint("data_inconsistency", d.ticket_number),
                    "severity": "high",
                    "title": f"Ticket {d.ticket_number} tare weight is not below gross",
                    "confidence": 0.99,
                    "evidence": {
                        "ticket": d.ticket_number,
                        "gross_lbs": str(gross),
                        "tare_lbs": str(tare),
                        "problem": "tare >= gross",
                    },
                    "recommendation": (
                        "Re-check the scale tickets; the weights are swapped or mistyped."
                    ),
                }
            )
            continue

        factor = lbs_per_bu.get(d.commodity_id)
        if not factor or Decimal(factor) <= 0:
            continue
        expected = ((gross - tare) / Decimal(factor)).quantize(Decimal("0.01"))
        actual = Decimal(d.net_bu)
        drift = abs(expected - actual)
        if drift > tolerance:
            alerts.append(
                {
                    "kind": "data_inconsistency",
                    "fingerprint": fingerprint("data_inconsistency", d.ticket_number),
                    "severity": "high" if drift > tolerance * 20 else "medium",
                    "title": (
                        f"Ticket {d.ticket_number} net bushels disagree with its weights"
                    ),
                    "confidence": 0.95,
                    "evidence": {
                        "ticket": d.ticket_number,
                        "recorded_net_bu": str(actual),
                        "expected_net_bu": str(expected),
                        "difference_bu": str(drift),
                        "gross_lbs": str(gross),
                        "tare_lbs": str(tare),
                        "lbs_per_bu": str(factor),
                    },
                    "recommendation": (
                        "Recalculate net bushels from the scale weights, or correct "
                        "the weights if the bushel figure is right."
                    ),
                }
            )
    return alerts


def unhedged_position(
    positions: Sequence[dict[str, Any]],
    threshold_bu: Decimal = UNHEDGED_THRESHOLD_BU,
) -> list[dict[str, Any]]:
    """Net open position past a size threshold — price exposure that is not covered.

    Net long means undelivered purchases exceed sales: grain owed to the elevator
    with no matching sale, so a price fall costs money. Net short is the mirror.
    Either direction past the threshold is worth telling a merchandiser about.

    This reports exposure. It does not size a hedge — see market.py on scope.
    """
    alerts: list[dict[str, Any]] = []
    for p in positions:
        net = Decimal(str(p.get("net_bu", 0)))
        if abs(net) < threshold_bu:
            continue
        direction = p.get("direction", "flat")
        unrealised = float(p.get("unrealised_usd") or 0.0)
        alerts.append(
            {
                "kind": "unhedged_position",
                "fingerprint": fingerprint("unhedged_position", p.get("commodity")),
                # A losing position of this size is worse news than a winning one.
                "severity": "high" if unrealised < 0 else "medium",
                "title": (
                    f"{p.get('commodity')} net {direction} {abs(net):,.0f} bu unhedged"
                ),
                "confidence": 0.9,
                "evidence": {
                    "commodity": p.get("commodity"),
                    "direction": direction,
                    "net_bu": str(net),
                    "long_bu": str(p.get("long_bu")),
                    "short_bu": str(p.get("short_bu")),
                    "market_usd_per_bu": str(p.get("market_usd_per_bu")),
                    "unrealised_usd": str(unrealised),
                    "open_contracts": p.get("open_contracts"),
                    "threshold_bu": str(threshold_bu),
                },
                "recommendation": (
                    f"Review {p.get('commodity')} coverage — a "
                    f"{abs(net):,.0f} bu net {direction} position moves with the "
                    "board until it is matched or hedged."
                ),
            }
        )
    return alerts


def contract_offmarket(
    valued: Sequence[dict[str, Any]],
    tolerance_pct: Decimal = OFFMARKET_TOLERANCE_PCT,
) -> list[dict[str, Any]]:
    """Open contracts priced far from the current board.

    Flags the gap, not a verdict: a contract 20% under the market may be a
    perfectly good old hedge, or it may be a mispriced entry. Either way the
    merchandiser wants to know before it settles.
    """
    alerts: list[dict[str, Any]] = []
    for c in valued:
        contracted = Decimal(str(c["price_per_bu"]))
        market = Decimal(str(c["market_usd_per_bu"]))
        if contracted <= 0 or market <= 0:
            continue
        gap_pct = (market - contracted) / contracted * 100
        if abs(gap_pct) < tolerance_pct:
            continue
        exposure = abs(float(c.get("unrealised_usd") or 0.0))
        alerts.append(
            {
                "kind": "contract_offmarket",
                "fingerprint": fingerprint("contract_offmarket", c["number"]),
                "severity": "high" if abs(gap_pct) >= tolerance_pct * 2 else "medium",
                "title": (
                    f"Contract {c['number']} is {abs(gap_pct):.1f}% "
                    f"{'below' if gap_pct > 0 else 'above'} the board"
                ),
                "confidence": 0.85,
                "evidence": {
                    "contract": c["number"],
                    "commodity": c["commodity"],
                    "side": c["side"],
                    "contracted_usd_per_bu": str(contracted),
                    "market_usd_per_bu": str(market),
                    "gap_pct": f"{gap_pct:.2f}",
                    "remaining_bu": str(c["remaining_bu"]),
                    "unrealised_usd": str(c.get("unrealised_usd")),
                },
                "recommendation": (
                    f"Confirm {c['number']} is intentional — "
                    f"{c['remaining_bu']:,.0f} bu still to move at "
                    f"${contracted}/bu against a ${market}/bu board "
                    f"(${exposure:,.0f} unrealised)."
                ),
            }
        )
    return alerts


def expiring_contracts(
    contracts: Sequence[Any], today: date, days: int = 30
) -> list[dict[str, Any]]:
    """Open contracts ending within `days`."""
    alerts = []
    for c in contracts:
        if c.status != "open":
            continue
        remaining = (c.end_date - today).days
        if 0 <= remaining <= days:
            alerts.append(
                {
                    "kind": "contract_expiration",
                    "fingerprint": fingerprint("contract_expiration", c.number),
                    "severity": "medium" if remaining > 7 else "high",
                    "title": f"Contract {c.number} ends in {remaining} day(s)",
                    "confidence": 1.0,
                    "evidence": {
                        "contract": c.number,
                        "end_date": c.end_date.isoformat(),
                        "days_remaining": remaining,
                    },
                    "recommendation": (
                        "Confirm remaining bushels will ship, or roll the contract."
                    ),
                }
            )
    return alerts
