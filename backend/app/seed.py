"""Synthetic demo dataset. Run: python -m app.seed

Deterministic (fixed RNG seed) so the demo looks the same every run, and
deliberately seeded with one of each risk condition so the Risk Center has real
findings rather than mocks:
  - a duplicate invoice pair
  - an over-capacity bin
  - a bin above the moisture ceiling
  - a contract expiring inside 30 days
  - an open contract long past its start with no deliveries
  - a delivery whose net bushels contradict its scale weights
  - a contract priced well above the corn board (off-market)
"""

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from .auth import hash_password
from .config import settings
from .db import Base, SessionLocal, engine
from .models import (
    Commodity,
    Contract,
    Customer,
    Delivery,
    Facility,
    Invoice,
    StorageBin,
    User,
)

# Fixed seed for reproducible demo data. Not used for anything security-relevant.
RNG = random.Random(42)  # noqa: S311

USERS = [
    ("ops@agfabric.test", "Dana Okoye", "ops"),
    ("accounting@agfabric.test", "Sam Iverson", "accountant"),
    ("warehouse@agfabric.test", "Rhea Patel", "warehouse"),
    ("exec@agfabric.test", "Morgan Bell", "exec"),
]

COMMODITIES = [("Corn", "56.00"), ("Soybeans", "60.00"), ("Wheat", "60.00")]

# Realistic per-bushel contract bands, so seeded contracts sit near the actual
# CBOT board and the off-market risk rule stays meaningful instead of firing on
# everything. Roughly current cash-market levels for each grain.
PRICE_BANDS: dict[str, tuple[float, float]] = {
    "Corn": (4.15, 5.40),
    "Soybeans": (10.60, 13.20),
    "Wheat": (5.60, 7.40),
}

# Real US grain hubs, so the weather and market context is plausible. Every name
# here must also exist in weather.FACILITY_COORDS or its forecast is skipped.
FACILITIES = [
    ("Chicago Terminal", "Chicago, IL"),
    ("New York Elevator", "New York, NY"),
    ("Kansas City Terminal", "Kansas City, MO"),
    ("Toledo Elevator", "Toledo, OH"),
    ("New Orleans Export", "New Orleans, LA"),
    ("Omaha River Terminal", "Omaha, NE"),
]

FARMERS = [
    "Halvorsen Family Farms",
    "Prairie Ridge Ag",
    "Two Rivers Grain Co",
    "Dunlap Brothers",
    "Cedar Hollow Farms",
    "Kettle Creek Acres",
    "Sandhill Cooperative",
    "Willow Bend Farms",
    "Ironwood Acres",
    "Redtail Grain Partners",
    "Blackfoot Valley Ag",
    "Miller & Sons Farming",
    "Northgate Croplands",
    "Silver Creek Growers",
    "Tallgrass Family Farms",
]
BUYERS = [
    "Midwest Milling LLC",
    "Gulf Export Partners",
    "Lakeside Feed & Grain",
    "Continental Oilseed",
    "Harbor Point Trading",
]

# Deliveries to generate. Enough to page through, chart a real trend, and give
# the position maths a meaningful book.
DELIVERY_COUNT = 260


def reset() -> None:
    """Drop everything, then rebuild via Alembic.

    Refuses to run unless ALLOW_DESTRUCTIVE_SEED is explicitly true. This function
    drops every table, so a stray `python -m app.seed` in a deploy script would
    otherwise wipe live data with no confirmation.

    Deliberately not `create_all`: if the seed built the schema itself, the
    migrations could drift from the models without anything noticing. Running the
    migrations here means the demo database and a deployed one are built by
    exactly the same path.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    if not settings.allow_destructive_seed:
        raise SystemExit(
            "Refusing to seed: this DROPS EVERY TABLE. "
            "Set ALLOW_DESTRUCTIVE_SEED=true to confirm you mean it. "
            "To stand up an empty instance instead, set BOOTSTRAP_ADMIN_EMAIL "
            "and BOOTSTRAP_ADMIN_PASSWORD and use POST /provision/* to create "
            "records."
        )

    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        # Otherwise Alembic thinks it is already at head and applies nothing.
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")


def run() -> None:
    reset()
    now = datetime.now(UTC)
    today = now.date()

    with SessionLocal() as db:
        db.add_all(
            User(
                email=email,
                full_name=name,
                role=role,
                password_hash=hash_password(settings.seed_password),
            )
            for email, name, role in USERS
        )

        commodities = [
            Commodity(name=n, unit="bu", lbs_per_bu=Decimal(lbs))
            for n, lbs in COMMODITIES
        ]
        facilities = [Facility(name=n, location=loc) for n, loc in FACILITIES]
        customers = [
            *(Customer(name=n, kind="farmer", contact_email=_email(n)) for n in FARMERS),
            *(Customer(name=n, kind="buyer", contact_email=_email(n)) for n in BUYERS),
        ]
        db.add_all([*commodities, *facilities, *customers])
        db.flush()

        corn, beans, wheat = commodities
        # Three or four bins per facility, filled to varied levels so the bin
        # chart has a real distribution rather than six similar bars.
        bins = []
        for facility in facilities:
            prefix = "".join(w[0] for w in facility.name.split()[:2]).upper()
            for n in range(1, RNG.randint(3, 5)):
                commodity = RNG.choice(commodities)
                capacity = RNG.randrange(90_000, 260_000, 10_000)
                bins.append(
                    _bin(
                        facility,
                        f"{prefix}-{n:02d}",
                        commodity,
                        capacity,
                        int(capacity * RNG.uniform(0.15, 0.92)),
                        str(round(RNG.uniform(11.2, 14.6), 1)),
                    )
                )
        # Two deliberate anomalies on the first facility, so the Risk Center has
        # a moisture finding and an over-capacity finding to show.
        bins.append(_bin(facilities[0], "ANOM-01", corn, 180_000, 96_300, "16.4"))
        bins.append(_bin(facilities[1], "ANOM-02", beans, 120_000, 127_500, "12.6"))
        db.add_all(bins)

        contracts = []
        for i, cust in enumerate(customers):
            start = today - timedelta(days=RNG.randint(60, 240))
            # One contract lands inside the 30-day window -> contract_expiration.
            end = today + timedelta(days=12 if i == 0 else RNG.randint(45, 300))
            commodity = RNG.choice(commodities)
            contracts.append(
                Contract(
                    number=f"C-2026-{1000 + i}",
                    customer_id=cust.id,
                    commodity_id=commodity.id,
                    quantity_bu=Decimal(RNG.randrange(20_000, 120_000, 5_000)),
                    # Priced near the real board for that grain, not one range
                    # across all three. A flat 4.10–13.75 band put corn contracts
                    # at $11 against a ~$4.75 board, which made the off-market
                    # rule fire on virtually every contract — correct rule,
                    # nonsense data.
                    price_per_bu=Decimal(
                        str(round(RNG.uniform(*PRICE_BANDS[commodity.name]), 4))
                    ),
                    start_date=start,
                    end_date=end,
                    status="open",
                )
            )
        # Started 200 days ago, ends soon, and excluded from delivery assignment
        # below -> missing_deliveries.
        neglected = Contract(
            number="C-2026-1099",
            customer_id=customers[3].id,
            commodity_id=wheat.id,
            quantity_bu=Decimal(35_000),
            price_per_bu=Decimal("6.4200"),
            start_date=today - timedelta(days=200),
            end_date=today + timedelta(days=25),
            status="open",
        )
        # One deliberately mispriced contract, so contract_offmarket has a real
        # finding to show rather than depending on random drift.
        mispriced = Contract(
            number="C-2026-1098",
            customer_id=customers[1].id,
            commodity_id=corn.id,
            quantity_bu=Decimal(30_000),
            price_per_bu=Decimal("7.9500"),  # well above a ~$4.75 corn board
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=120),
            status="open",
        )
        contracts.append(mispriced)
        contracts.append(neglected)
        db.add_all(contracts)
        db.flush()

        # Everything except the neglected contract can receive deliveries.
        deliverable = [c for c in contracts if c is not neglected]

        deliveries = []
        for i in range(DELIVERY_COUNT):
            contract = RNG.choice(deliverable)
            gross = Decimal(RNG.randrange(52_000, 80_000, 250))
            tare = Decimal(RNG.randrange(28_000, 33_000, 250))
            lbs_per_bu = next(
                c.lbs_per_bu for c in commodities if c.id == contract.commodity_id
            )
            deliveries.append(
                Delivery(
                    ticket_number=f"T-{80_000 + i}",
                    contract_id=contract.id,
                    customer_id=contract.customer_id,
                    commodity_id=contract.commodity_id,
                    facility_id=RNG.choice(facilities).id,
                    truck_id=f"IL-{RNG.randrange(1000, 9999)}",
                    gross_lbs=gross,
                    tare_lbs=tare,
                    net_bu=((gross - tare) / lbs_per_bu).quantize(Decimal("0.01")),
                    moisture_pct=Decimal(str(round(RNG.uniform(11.0, 16.8), 2))),
                    delivered_at=now - timedelta(hours=RNG.randint(1, 24 * 30)),
                    verified=RNG.random() > 0.25,
                )
            )
        # Net bushels inconsistent with (gross - tare) / lbs_per_bu, as a bad
        # manual edit would look -> data_inconsistency.
        deliveries[11].net_bu = (deliveries[11].net_bu + Decimal("180.00")).quantize(
            Decimal("0.01")
        )
        db.add_all(deliveries)

        invoices = []
        for i, contract in enumerate(contracts):
            amount = (contract.quantity_bu * contract.price_per_bu).quantize(
                Decimal("0.01")
            )
            issued = today - timedelta(days=RNG.randint(5, 75))
            invoices.append(
                Invoice(
                    number=f"INV-{5000 + i}",
                    customer_id=contract.customer_id,
                    contract_id=contract.id,
                    amount=amount,
                    issued_date=issued,
                    due_date=issued + timedelta(days=30),
                    status=_invoice_status(issued, today),
                )
            )
        # Same customer, same amount, 3 days apart -> duplicate_invoice (high).
        dupe_of = invoices[2]
        invoices.append(
            Invoice(
                number="INV-5099",
                customer_id=dupe_of.customer_id,
                contract_id=dupe_of.contract_id,
                amount=dupe_of.amount,
                issued_date=dupe_of.issued_date + timedelta(days=3),
                due_date=dupe_of.due_date + timedelta(days=3),
                status="open",
            )
        )
        db.add_all(invoices)
        db.commit()

    print(
        f"Seeded {len(USERS)} users, {len(customers)} customers, {len(bins)} bins, "
        f"{len(contracts)} contracts, {len(deliveries)} deliveries, "
        f"{len(invoices)} invoices."
    )
    print(f"Login with any address above / password: {settings.seed_password}")


def _email(name: str) -> str:
    slug = "".join(ch for ch in name.lower() if ch.isalnum() or ch == " ")
    return slug.replace(" ", ".")[:40] + "@example.test"


def _bin(facility, name, commodity, capacity, current, moisture) -> StorageBin:
    return StorageBin(
        facility_id=facility.id,
        name=name,
        commodity_id=commodity.id,
        capacity_bu=Decimal(capacity),
        current_bu=Decimal(current),
        moisture_pct=Decimal(moisture),
    )


def _invoice_status(issued: date, today: date) -> str:
    age = (today - issued).days
    if age > 30:
        return "overdue" if RNG.random() < 0.35 else "paid"
    return "open"


if __name__ == "__main__":
    run()
