"""Text-to-SQL: the model writes the query, this module decides whether it runs.

Generated SQL is untrusted input that happens to be executable, so there are
independent layers between generation and results. Any one of them alone would
be a single point of failure:

1. `validate()` — a static gate. One statement, must be SELECT/WITH, no comments,
   no DML/DDL, no dangerous functions, every table on the caller's allowlist,
   `users` never reachable. A fast first filter with clear errors, but regexes
   over SQL can be talked around (comma joins, quoted identifiers, functions
   that query tables themselves), so it is not the boundary.
2. A Postgres role per caller class (`run()` switches into it). It can SELECT
   the allowlisted tables — and, for non-finance callers, no money columns — and
   nothing else. This is the boundary: the database decides what is readable,
   however the SQL is written.
3. A READ ONLY transaction, so nothing can be written either.
4. `statement_timeout` and a row cap applied inside Postgres, so a pathological
   query can neither hold a connection nor flood the API's memory.

Everything except `run()` is a pure function over strings, so the gate is tested
exhaustively without a database.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from .config import settings
from .db import engine

# Created by the sql_reader_roles migration, which grants exactly the tables and
# columns below. Generated SQL only ever runs as one of these.
FINANCE_ROLE = "agfabric_sql_finance"
OPS_ROLE = "agfabric_sql_ops"

# `users` is deliberately absent: it holds password hashes and has no business
# in an analytics answer. Anything not listed here cannot be queried at all.
ALLOWED_TABLES = frozenset(
    {
        "customers",
        "commodities",
        "facilities",
        "storage_bins",
        "contracts",
        "deliveries",
        "invoices",
        "documents",
        "document_chunks",
    }
)

# Schema handed to the model. Hand-written rather than reflected, so it can never
# accidentally advertise a table the gate would reject.
SCHEMA = """customers(id, name, kind['farmer'|'buyer'], contact_email, phone)
commodities(id, name, unit, lbs_per_bu)
facilities(id, name, location)
storage_bins(id, facility_id->facilities, name, commodity_id->commodities,
             capacity_bu, current_bu, moisture_pct)
contracts(id, number, customer_id->customers, commodity_id->commodities,
          quantity_bu, price_per_bu, start_date, end_date, status['open'|'closed'])
deliveries(id, ticket_number, contract_id->contracts, customer_id->customers,
           commodity_id->commodities, facility_id->facilities, truck_id,
           gross_lbs, tare_lbs, net_bu, moisture_pct, delivered_at, verified)
invoices(id, number, customer_id->customers, contract_id->contracts, amount,
         issued_date, due_date, status['open'|'paid'|'overdue'])
documents(id, filename, content_type, size_bytes, sha256, version, text_chars,
          chunk_count, status, uploaded_at)
document_chunks(id, document_id->documents, ordinal, text, char_count, embedded)"""

# Non-finance callers see what their role can read: no invoices table and no
# contract price, the same figures the API redacts for them everywhere else.
OPS_TABLES = ALLOWED_TABLES - {"invoices"}
OPS_SCHEMA = re.sub(r"\ninvoices\([^)]*\)", "", SCHEMA).replace(" price_per_bu,", "")


def _system_prompt(schema: str, money_rule: str) -> str:
    return f"""You write a single read-only PostgreSQL query answering the
user's question about a grain operations database.

SCHEMA (these are the only tables and columns that exist for you):
{schema}

Rules — a query breaking any of these is rejected and the user gets no answer:
- Exactly one statement. No semicolons inside it, no second statement.
- SELECT or WITH only. Never INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, GRANT,
  TRUNCATE, COPY or SET.
- No SQL comments (-- or /* */).
- Only the tables and columns listed above. There is no users table; never
  reference one. Name the columns you need rather than using SELECT *.
- At most 200 rows are returned.
{money_rule}- Bushels are net_bu on deliveries, quantity_bu on contracts.

Return ONLY the SQL. No prose, no markdown fences, no explanation.
If the question cannot be answered from this schema, return exactly: NO_QUERY"""


SQL_SYSTEM_PROMPT = _system_prompt(
    SCHEMA, "- Money columns are numeric: invoices.amount, contracts.price_per_bu.\n"
)
OPS_SQL_SYSTEM_PROMPT = _system_prompt(OPS_SCHEMA, "")

_FENCE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)
_STARTS_READONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_.\"]*)", re.IGNORECASE)

# Anything that writes, changes session state, touches the filesystem, or sleeps.
_FORBIDDEN = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|"
    r"vacuum|reindex|cluster|comment|call|do|execute|prepare|deallocate|"
    r"listen|notify|unlisten|lock|set|reset|begin|start|commit|rollback|savepoint|"
    r"pg_sleep|pg_read_file|pg_read_binary_file|pg_write_file|pg_ls_dir|"
    r"pg_stat_file|pg_reload_conf|pg_terminate_backend|pg_cancel_backend|"
    r"lo_import|lo_export|dblink|dblink_exec|current_setting|set_config|"
    r"pg_authid|pg_shadow|pg_user|pg_roles|information_schema|pg_catalog"
    r")\b",
    re.IGNORECASE,
)


class UnsafeSQL(Exception):
    """The generated SQL failed the gate and must not be executed."""


@dataclass(frozen=True)
class SqlResult:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error: str | None = None


def clean(raw: str) -> str:
    """Strip markdown fencing and trailing punctuation from a model response."""
    candidate = raw.strip()
    # Fences can wrap both ends; remove them from each line boundary.
    candidate = _FENCE.sub("", candidate).strip()
    while candidate.endswith(";"):
        candidate = candidate[:-1].rstrip()
    return candidate


def validate(raw: str, tables: frozenset[str] = ALLOWED_TABLES) -> str:
    """Return executable SQL, or raise UnsafeSQL. `tables` is the caller's allowlist."""
    sql = clean(raw)
    if not sql:
        raise UnsafeSQL("empty query")
    if sql.upper() == "NO_QUERY":
        raise UnsafeSQL("model reported the question is unanswerable from the schema")

    # Comments first: they are the standard way to smuggle text past keyword
    # checks, and no legitimately generated query here needs them.
    if "--" in sql or "/*" in sql or "*/" in sql:
        raise UnsafeSQL("SQL comments are not allowed")

    # Trailing semicolons were stripped, so any remaining one chains statements.
    if ";" in sql:
        raise UnsafeSQL("only a single statement is allowed")

    if not _STARTS_READONLY.match(sql):
        raise UnsafeSQL("only SELECT or WITH queries are allowed")

    forbidden = _FORBIDDEN.search(sql)
    if forbidden:
        raise UnsafeSQL(f"forbidden keyword: {forbidden.group(1).lower()}")

    # Catches `users` however it is reached — FROM, JOIN, subquery or UNION.
    if re.search(r"\busers\b", sql, re.IGNORECASE):
        raise UnsafeSQL("the users table is not queryable")

    referenced = {
        table.strip('"').split(".")[-1].lower() for table in _TABLE_REF.findall(sql)
    }
    # CTE names appear after FROM/JOIN too; allow anything defined by a WITH.
    cte_names = {
        name.lower()
        for name in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", sql, re.I)
    }
    unknown = referenced - tables - cte_names
    if unknown:
        raise UnsafeSQL(f"table(s) not allowed: {', '.join(sorted(unknown))}")
    if not referenced:
        raise UnsafeSQL("query references no known table")
    return sql


def run(sql: str, *, finance: bool) -> SqlResult:
    """Execute validated SQL as the caller's reader role. Assumes `validate` passed."""
    try:
        # postgresql_readonly issues a READ ONLY transaction, so a write is
        # refused by the database even if the gate above missed something.
        with engine.connect().execution_options(postgresql_readonly=True) as conn:
            conn.execute(
                text(f"SET LOCAL statement_timeout = {settings.sql_statement_timeout_ms}")
            )
            # From here Postgres decides what is readable: the role sees the
            # allowlisted tables (no money columns for non-finance callers) and
            # nothing else, whatever the SQL says. Reverts with the transaction.
            conn.execute(text(f"SET LOCAL ROLE {FINANCE_ROLE if finance else OPS_ROLE}"))
            # The cap is applied by Postgres. fetchmany alone would only trim a
            # result psycopg had already buffered in full. One row past the cap
            # makes truncation detectable rather than silent.
            result = conn.execute(
                text(f"SELECT * FROM ({sql}) AS q LIMIT {settings.sql_max_rows + 1}")
            )
            columns = list(result.keys())
            fetched = result.fetchall()
    except Exception as exc:
        return SqlResult(sql=sql, error=_short(exc))

    truncated = len(fetched) > settings.sql_max_rows
    rows = [
        {col: _jsonable(value) for col, value in zip(columns, row, strict=True)}
        for row in fetched[: settings.sql_max_rows]
    ]
    return SqlResult(
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
    )


def _jsonable(value: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _short(exc: Exception) -> str:
    """First line only: driver errors otherwise leak the whole statement back."""
    return str(exc).strip().splitlines()[0][:300]
