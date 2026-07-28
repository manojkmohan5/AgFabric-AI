"""Create the operational records the business owns.

The four external feeds (weather, market, FX, news) are genuinely real-time. But
facilities, bins, customers and contracts are *this company's own records* — no
public API can supply them, so previously the only way they existed was
`python -m app.seed`, which also drops every table.

That is the real "avoid seeds" gap. These endpoints let an operator stand the
platform up from empty:

    POST /provision/facilities   → then /provision/bins
    POST /provision/customers    → then /provision/contracts
    POST /webhooks/deliveries    → deliveries arrive as they happen (already built)
    POST /documents/upload       → real contracts as PDFs, OCR for photos

With those plus the feed agents, a running instance holds no synthetic data at
all. `seed.py` becomes strictly a demo convenience rather than the only path in.

Validation is real, not decorative: capacities must be positive, a bin cannot be
created above its own capacity, a contract's window must run forwards, and a
duplicate name or number is a 409 rather than a second row.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_role
from .db import get_db
from .models import Commodity, Contract, Customer, Facility, StorageBin, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/provision", tags=["provision"])

DbDep = Annotated[Session, Depends(get_db)]
# Provisioning changes commercial records, so it is ops or exec — the same gate
# that guards running agents.
OperatorDep = Annotated[User, Depends(require_role("ops", "exec"))]

CUSTOMER_KINDS = ("farmer", "buyer")


def _unique_or_409(db: Session, model, column, value: str, label: str) -> None:
    if db.scalar(select(model).where(column == value)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"{label} {value!r} already exists")


# ------------------------------------------------------------------ commodities


class CommodityIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # Test weight per bushel. Wrong here and every net-bushel figure downstream is
    # wrong, so it is required rather than defaulted.
    lbs_per_bu: Decimal = Field(gt=0, le=200)
    unit: str = Field(default="bu", max_length=16)


@router.post("/commodities", status_code=status.HTTP_201_CREATED)
def create_commodity(body: CommodityIn, db: DbDep, user: OperatorDep) -> dict:
    name = body.name.strip()
    _unique_or_409(db, Commodity, Commodity.name, name, "commodity")
    row = Commodity(name=name, unit=body.unit, lbs_per_bu=body.lbs_per_bu)
    db.add(row)
    db.commit()
    return {"id": row.id, "name": row.name, "lbs_per_bu": float(row.lbs_per_bu)}


# -------------------------------------------------------------------- facilities


class FacilityIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    location: str = Field(min_length=1, max_length=128)


@router.post("/facilities", status_code=status.HTTP_201_CREATED)
def create_facility(body: FacilityIn, db: DbDep, user: OperatorDep) -> dict:
    name = body.name.strip()
    _unique_or_409(db, Facility, Facility.name, name, "facility")
    row = Facility(name=name, location=body.location.strip())
    db.add(row)
    db.commit()
    # Weather coordinates are keyed by facility name in weather.py; an unmapped
    # facility is skipped by the sync rather than failing it, and the response
    # says so instead of leaving the operator to discover it.
    from .weather import FACILITY_COORDS

    return {
        "id": row.id,
        "name": row.name,
        "location": row.location,
        "weather_enabled": row.name in FACILITY_COORDS,
        "note": (
            None
            if row.name in FACILITY_COORDS
            else "no coordinates mapped for this facility name, so weather sync "
            "will skip it — add it to weather.FACILITY_COORDS"
        ),
    }


# --------------------------------------------------------------------- bins


class BinIn(BaseModel):
    facility_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=64)
    commodity_id: int | None = Field(default=None, ge=1)
    capacity_bu: Decimal = Field(gt=0)
    current_bu: Decimal = Field(default=Decimal("0"), ge=0)
    moisture_pct: Decimal | None = Field(default=None, ge=0, le=100)


@router.post("/bins", status_code=status.HTTP_201_CREATED)
def create_bin(body: BinIn, db: DbDep, user: OperatorDep) -> dict:
    if db.get(Facility, body.facility_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such facility")
    if body.commodity_id and db.get(Commodity, body.commodity_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such commodity")
    # Bin names are unique per facility, not globally — two sites can both have
    # a "BIN-01".
    if db.scalar(
        select(StorageBin).where(
            StorageBin.facility_id == body.facility_id,
            StorageBin.name == body.name.strip(),
        )
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "that bin already exists at this facility"
        )
    # Creating a bin already over capacity would immediately trip the
    # inventory_mismatch rule, which is a data-entry error, not a real finding.
    if body.current_bu > body.capacity_bu:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "current_bu cannot exceed capacity_bu at creation",
        )

    row = StorageBin(
        facility_id=body.facility_id,
        name=body.name.strip(),
        commodity_id=body.commodity_id,
        capacity_bu=body.capacity_bu,
        current_bu=body.current_bu,
        moisture_pct=body.moisture_pct,
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "name": row.name, "capacity_bu": float(row.capacity_bu)}


# ----------------------------------------------------------------- customers


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: str
    contact_email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("kind")
    @classmethod
    def known_kind(cls, v: str) -> str:
        # The side of the trade drives position sign in market.py, so an
        # unrecognised value would silently invert a valuation.
        if v not in CUSTOMER_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(CUSTOMER_KINDS)}")
        return v


@router.post("/customers", status_code=status.HTTP_201_CREATED)
def create_customer(body: CustomerIn, db: DbDep, user: OperatorDep) -> dict:
    name = body.name.strip()
    _unique_or_409(db, Customer, Customer.name, name, "customer")
    row = Customer(
        name=name,
        kind=body.kind,
        contact_email=body.contact_email,
        phone=body.phone,
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "name": row.name, "kind": row.kind}


# ----------------------------------------------------------------- contracts


class ContractIn(BaseModel):
    number: str = Field(min_length=1, max_length=32)
    customer_id: int = Field(ge=1)
    commodity_id: int = Field(ge=1)
    quantity_bu: Decimal = Field(gt=0)
    price_per_bu: Decimal = Field(gt=0)
    start_date: date
    end_date: date


@router.post("/contracts", status_code=status.HTTP_201_CREATED)
def create_contract(body: ContractIn, db: DbDep, user: OperatorDep) -> dict:
    number = body.number.strip()
    _unique_or_409(db, Contract, Contract.number, number, "contract")
    if db.get(Customer, body.customer_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such customer")
    if db.get(Commodity, body.commodity_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such commodity")
    # A backwards window would make every elapsed/remaining calculation nonsense.
    if body.end_date < body.start_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "end_date cannot be before start_date",
        )

    row = Contract(
        number=number,
        customer_id=body.customer_id,
        commodity_id=body.commodity_id,
        quantity_bu=body.quantity_bu,
        price_per_bu=body.price_per_bu,
        start_date=body.start_date,
        end_date=body.end_date,
        status="open",
    )
    db.add(row)
    db.commit()
    return {
        "id": row.id,
        "number": row.number,
        "quantity_bu": float(row.quantity_bu),
        "price_per_bu": float(row.price_per_bu),
    }


@router.get("/summary")
def provision_summary(db: DbDep, user: OperatorDep) -> dict:
    """What exists, so an operator can see how far setup has got.

    `ready` is the honest bar for a working instance: without a commodity, a
    facility and a bin there is nothing for deliveries or the market agent to
    attach to.
    """

    def count(model) -> int:
        return db.scalar(select(func.count()).select_from(model)) or 0

    counts = {
        "commodities": count(Commodity),
        "facilities": count(Facility),
        "bins": count(StorageBin),
        "customers": count(Customer),
        "contracts": count(Contract),
    }
    missing = [k for k in ("commodities", "facilities", "bins") if not counts[k]]
    return {
        "counts": counts,
        "ready": not missing,
        "missing": missing,
        "next_step": (
            f"create {missing[0][:-1]} records first"
            if missing
            else "ready to receive deliveries"
        ),
    }
