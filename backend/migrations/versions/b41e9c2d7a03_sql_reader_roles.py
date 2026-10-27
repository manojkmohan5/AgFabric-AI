"""sql reader roles

Generated SQL runs as one of these roles (`SET LOCAL ROLE` in sqlgen.run), so
Postgres itself decides what a model-written query can read, however it is
phrased. The regex gate in sqlgen.validate stays as a first filter.

Finance callers read the nine tables the model is shown. Everyone else reads the
same minus invoices and contract prices, matching what the API redacts for them.

Revision ID: b41e9c2d7a03
Revises: 7e13ffe077f7
Create Date: 2026-10-27 18:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b41e9c2d7a03"
down_revision: str | None = "7e13ffe077f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FINANCE = "agfabric_sql_finance"
OPS = "agfabric_sql_ops"
SHARED = (
    "customers, commodities, facilities, storage_bins, deliveries, documents, "
    "document_chunks"
)
# Every contracts column except price_per_bu.
CONTRACT_COLUMNS = (
    "id, number, customer_id, commodity_id, quantity_bu, start_date, end_date, status"
)


def upgrade() -> None:
    for role in (FINANCE, OPS):
        # Roles are cluster-wide and outlive the seed's drop-and-remigrate, so
        # create each once. NOLOGIN: nothing can connect as them directly.
        op.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') "
            f"THEN CREATE ROLE {role} NOLOGIN; END IF; END $$"
        )
        # Lets the app's own user switch into the role for one transaction.
        op.execute(f"GRANT {role} TO CURRENT_USER")
    op.execute(f"GRANT SELECT ON {SHARED}, contracts, invoices TO {FINANCE}")
    op.execute(f"GRANT SELECT ON {SHARED} TO {OPS}")
    op.execute(f"GRANT SELECT ({CONTRACT_COLUMNS}) ON contracts TO {OPS}")


def downgrade() -> None:
    # Revoking table privileges also revokes the column grant on contracts.
    op.execute(f"REVOKE ALL ON {SHARED}, contracts, invoices FROM {FINANCE}, {OPS}")
    for role in (FINANCE, OPS):
        op.execute(f"DROP ROLE IF EXISTS {role}")
