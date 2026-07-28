"""Index evidence: EXPLAIN ANALYZE the real queries, with and without indexes.

    python benchmark.py            # measure against the current schema
    python benchmark.py --grow N   # insert N synthetic deliveries first

Measuring on 60 rows proves nothing — Postgres sequential-scans a small table
because that genuinely is faster. So this grows the tables first, then compares
each query with its index present against the same query with indexes disabled
(`enable_indexscan`/`enable_bitmapscan` off), which isolates the index's effect
without dropping and recreating anything.

Reports plan node and actual time for both, so the numbers are the planner's
rather than mine.
"""

import argparse
import os
import random
import sys

os.environ.setdefault("JWT_SECRET", "benchmark-secret-that-is-long-enough-000000")

from datetime import UTC, datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Contract, Delivery  # noqa: E402

RNG = random.Random(7)  # noqa: S311 — synthetic rows, nothing security-relevant

# The queries that actually run on every dashboard load, plus the ones the agents
# and document list use. Named after where they come from.
QUERIES: list[tuple[str, str]] = [
    (
        "dashboard: unverified deliveries",
        "SELECT count(*) FROM deliveries WHERE verified = false",
    ),
    (
        "dashboard: open contracts",
        "SELECT count(*) FROM contracts WHERE status = 'open'",
    ),
    (
        "dashboard: contracts expiring in 30d",
        "SELECT count(*) FROM contracts WHERE status = 'open' "
        "AND end_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 30",
    ),
    (
        "dashboard: financial summary by status",
        "SELECT status, sum(amount), count(*) FROM invoices GROUP BY status",
    ),
    (
        "dashboard: 10 most recent deliveries",
        "SELECT * FROM deliveries ORDER BY delivered_at DESC LIMIT 10",
    ),
    (
        "indexing: chunks awaiting embedding",
        "SELECT count(*) FROM document_chunks WHERE embedded = false",
    ),
    (
        "documents: newest first",
        "SELECT * FROM documents ORDER BY uploaded_at DESC LIMIT 50",
    ),
    (
        "agents: latest run per agent",
        "SELECT DISTINCT ON (agent) * FROM agent_runs ORDER BY agent, started_at DESC",
    ),
]


def grow(rows: int) -> None:
    """Add synthetic deliveries and contracts so the planner has a real choice."""
    with SessionLocal() as db:
        base_delivery = db.query(Delivery).first()
        base_contract = db.query(Contract).first()
        if base_delivery is None or base_contract is None:
            sys.exit("run `python -m app.seed` first")

        now = datetime.now(UTC)
        db.add_all(
            Delivery(
                ticket_number=f"BENCH-{i}",
                contract_id=base_delivery.contract_id,
                customer_id=base_delivery.customer_id,
                commodity_id=base_delivery.commodity_id,
                facility_id=base_delivery.facility_id,
                truck_id=f"IL-{RNG.randrange(1000, 9999)}",
                gross_lbs=Decimal("60000"),
                tare_lbs=Decimal("30000"),
                net_bu=Decimal("535.71"),
                moisture_pct=Decimal("13.5"),
                delivered_at=now - timedelta(minutes=i),
                # Skewed: an index earns its place when the filter is selective.
                verified=i % 50 != 0,
            )
            for i in range(rows)
        )
        db.add_all(
            Contract(
                number=f"BENCH-C-{i}",
                customer_id=base_contract.customer_id,
                commodity_id=base_contract.commodity_id,
                quantity_bu=Decimal("10000"),
                price_per_bu=Decimal("5.0"),
                start_date=(now - timedelta(days=400 + i % 200)).date(),
                end_date=(now + timedelta(days=200 + i % 400)).date(),
                status="closed" if i % 20 else "open",
            )
            for i in range(rows // 10)
        )
        db.commit()

    with engine.connect() as conn:
        # Stale statistics make the planner guess; ANALYZE before measuring.
        conn.exec_driver_sql("COMMIT")
        conn.exec_driver_sql("ANALYZE")
    print(f"added {rows} deliveries and {rows // 10} contracts, then ANALYZE\n")


def explain(sql: str, use_indexes: bool) -> tuple[str, float]:
    """Return (top plan node, actual ms). Indexes disabled per-session only."""
    with engine.connect() as conn:
        if not use_indexes:
            conn.execute(text("SET LOCAL enable_indexscan = off"))
            conn.execute(text("SET LOCAL enable_bitmapscan = off"))
            conn.execute(text("SET LOCAL enable_indexonlyscan = off"))
        rows = conn.execute(text(f"EXPLAIN (ANALYZE, TIMING) {sql}")).fetchall()

    plan = [r[0] for r in rows]
    # The scan node is what an index changes, and it sits below the top node
    # (an Aggregate looks identical either way) — so search the whole plan.
    node = next(
        (
            line.strip().removeprefix("-> ").split("(")[0].strip()
            for line in plan
            for marker in (
                "Index Only Scan",
                "Index Scan",
                "Bitmap Heap Scan",
                "Seq Scan",
            )
            if marker in line
        ),
        plan[0].strip().split("(")[0].strip(),
    )
    total = next(
        (
            float(line.split(":")[1].strip().split(" ")[0])
            for line in plan
            if line.strip().startswith("Execution Time")
        ),
        0.0,
    )
    return node, total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grow", type=int, default=0, help="synthetic deliveries to add")
    args = parser.parse_args()

    if args.grow:
        grow(args.grow)

    with engine.connect() as conn:
        counts = {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            for table in ("deliveries", "contracts", "invoices", "document_chunks")
        }
    print("row counts:", ", ".join(f"{k}={v}" for k, v in counts.items()), "\n")

    header = f"{'query':38} {'no index':>11}  {'with index':>11}  {'gain':>7}  plan"
    print(header)
    print("-" * len(header))

    for name, sql in QUERIES:
        # Warm both paths first so neither pays a one-off cache miss.
        explain(sql, use_indexes=True)
        explain(sql, use_indexes=False)

        seq_node, seq_ms = explain(sql, use_indexes=False)
        idx_node, idx_ms = explain(sql, use_indexes=True)
        gain = f"{seq_ms / idx_ms:.1f}x" if idx_ms > 0 else "n/a"
        used = (
            f"{seq_node} -> {idx_node}" if idx_node != seq_node else f"{idx_node} (both)"
        )
        print(f"{name:38} {seq_ms:9.2f}ms  {idx_ms:9.2f}ms  {gain:>7}  {used}")

    print(
        "\n'(both)' means the planner picked the same scan either way — correct on a "
        "small\nor unselective table, and worth knowing rather than assuming."
    )


if __name__ == "__main__":
    main()
