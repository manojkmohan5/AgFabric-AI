"""End-to-end check against a seeded database. Run after `python -m app.seed`:

    python smoketest.py

Verifies auth, RBAC, and that the seeded risk conditions are actually detected.
"""

import hashlib
import logging
import os

# Pin the fake providers BEFORE any app module reads settings. The suites assert
# `provider == "fake"`, but asserting is too late: with a real OPENAI_API_KEY in
# the environment, `auto` resolves to openai and the run bills real tokens before
# reaching the assertion. Forcing it here keeps the suite at $0 and offline
# whether or not a key is configured.
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["OCR_PROVIDER"] = "fake"
os.environ["MARKET_PROVIDER"] = "fake"
os.environ["FEEDS_PROVIDER"] = "fake"

from fastapi.testclient import TestClient

from app.agents import REGISTRY, SCHEDULED
from app.config import settings
from app.main import app
from app.ratelimit import login_limiter
from app.seed import BUYERS, DELIVERY_COUNT, FACILITIES, FARMERS

# The webhook checks deliberately send bad payloads. A stack trace per failure is
# right in production and noise here.
logging.getLogger("app.webhooks").setLevel(logging.CRITICAL)

client = TestClient(app)

# TestClient sends every request from one client address, so unmemoized logins
# would trip the rate limiter partway through this file.
_TOKENS: dict[str, str] = {}


def login(email: str) -> str:
    if email not in _TOKENS:
        r = client.post(
            "/login", data={"username": email, "password": settings.seed_password}
        )
        assert r.status_code == 200, r.text
        _TOKENS[email] = r.json()["access_token"]
    return _TOKENS[email]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def check_health() -> None:
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    # Every dependency is probed and named, not just Postgres.
    assert body["status"] == "ok", body
    assert body["checks"] == {
        "database": "ok",
        "object_storage": "ok",
        "vector_store": "ok",
    }, body
    assert body["degraded"] == []


def check_auth() -> None:
    bad = client.post(
        "/login", data={"username": "ops@agfabric.test", "password": "nope"}
    )
    assert bad.status_code == 401, bad.text
    unknown = client.post(
        "/login", data={"username": "nobody@nowhere.test", "password": "nope"}
    )
    assert unknown.status_code == 401
    # Identical message for both, so the endpoint does not enumerate accounts.
    assert bad.json()["detail"] == unknown.json()["detail"]

    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard", headers=bearer("garbage")).status_code == 401


def check_dashboard_and_rbac() -> None:
    warehouse = client.get("/dashboard", headers=bearer(login("warehouse@agfabric.test")))
    assert warehouse.status_code == 200, warehouse.text
    body = warehouse.json()
    assert body["storage"]["capacity_bu"] > 0
    # Derived from the seed constants, not a magic number: re-tuning the demo
    # dataset should not fail an unrelated smoke check. Pinned totals have
    # broken this file twice.
    assert body["storage"]["bins"] >= 2 * len(FACILITIES)
    assert len(body["recent_events"]) == 10
    assert "financial_summary" not in body, "warehouse must not see financials"

    accountant = client.get(
        "/dashboard", headers=bearer(login("accounting@agfabric.test"))
    ).json()
    assert accountant["financial_summary"], "accountant must see financials"


def check_storage() -> None:
    r = client.get("/storage", headers=bearer(login("warehouse@agfabric.test")))
    assert r.status_code == 200, r.text
    bins = r.json()["bins"]
    assert len(bins) >= 2 * len(FACILITIES)
    assert any(b["current_bu"] > b["capacity_bu"] for b in bins)
    # Every facility has at least one bin, or the storage view has a blind spot.
    assert len({b["facility"] for b in bins}) == len(FACILITIES)


def check_graph() -> None:
    token = login("ops@agfabric.test")
    overview = client.get("/graph", headers=bearer(token))
    assert overview.status_code == 200, overview.text
    body = overview.json()
    # Every seeded row of every kind becomes a node. Derived rather than a magic
    # total, so tweaking the seed does not break this for no reason.
    by_kind = body["nodes_by_kind"]
    assert body["node_count"] == sum(by_kind.values()), body
    assert set(by_kind) == {
        "customer",
        "commodity",
        "facility",
        "bin",
        "contract",
        "delivery",
        "invoice",
    }, by_kind
    # >= because the webhook checks add real deliveries to this same database.
    assert by_kind["delivery"] >= DELIVERY_COUNT
    assert by_kind["facility"] == len(FACILITIES)
    assert by_kind["customer"] == len(FARMERS) + len(BUYERS)
    # One edge per foreign key, so there are more edges than nodes here.
    assert body["edge_count"] > body["node_count"]
    assert set(body["edges_by_label"]) == {
        "BILLED_TO",
        "BILLS",
        "DELIVERED_BY",
        "FOR_COMMODITY",
        "FULFILLS",
        "HAS_BIN",
        "RECEIVED_AT",
        "SIGNED",
        "STORES",
    }, body["edges_by_label"]

    one = client.get("/graph/entity/customer:1?depth=1", headers=bearer(token))
    assert one.status_code == 200, one.text
    hop1 = one.json()
    assert hop1["root"] == "customer:1" and hop1["depth"] == 1
    assert hop1["nodes"][0]["id"] == "customer:1"
    assert hop1["nodes"][0]["hops"] == 0
    assert len(hop1["nodes"]) > 1, "customer:1 should have neighbours"

    # More depth cannot return fewer nodes.
    two = client.get("/graph/entity/customer:1?depth=2", headers=bearer(token)).json()
    assert len(two["nodes"]) >= len(hop1["nodes"])
    assert two["depth"] == 2

    # Malformed node ids are rejected, unknown ones 404, and auth is required.
    assert client.get("/graph/entity/bogus:1", headers=bearer(token)).status_code == 422
    assert client.get("/graph/entity/customer", headers=bearer(token)).status_code == 422
    assert (
        client.get("/graph/entity/customer:99999", headers=bearer(token)).status_code
        == 404
    )
    assert client.get("/graph").status_code == 401


def check_documents() -> None:
    token = login("ops@agfabric.test")
    h = bearer(token)
    body = b"name,bushels\nCorn,500\nWheat,250\n"

    created = client.post(
        "/documents/upload",
        headers=h,
        files={"file": ("deliveries.csv", body, "text/csv")},
    )
    assert created.status_code == 201, created.text
    doc = created.json()["document"]
    assert created.json()["duplicate"] is False
    # Since Phase 3 upload also embeds inline, so a successful upload lands on
    # "embedded" rather than stopping at "extracted".
    assert doc["version"] == 1 and doc["status"] == "embedded", doc
    assert doc["chunk_count"] >= 1 and doc["text_chars"] > 0
    assert doc["sha256"] == hashlib.sha256(body).hexdigest()

    # Identical bytes are recognised as a duplicate, not stored twice.
    again = client.post(
        "/documents/upload",
        headers=h,
        files={"file": ("other-name.csv", body, "text/csv")},
    )
    assert again.status_code == 201, again.text
    assert again.json()["duplicate"] is True
    assert again.json()["document"]["id"] == doc["id"]

    # Same filename, different bytes -> version 2.
    v2 = client.post(
        "/documents/upload",
        headers=h,
        files={"file": ("deliveries.csv", body + b"Soy,100\n", "text/csv")},
    )
    assert v2.status_code == 201, v2.text
    assert v2.json()["document"]["version"] == 2

    # Chunks are readable and ordered.
    detail = client.get(f"/documents/{doc['id']}", headers=h).json()
    assert len(detail["chunks"]) == doc["chunk_count"]
    assert [c["ordinal"] for c in detail["chunks"]] == list(range(doc["chunk_count"]))
    assert "Corn | 500" in detail["chunks"][0]["preview"]
    assert detail["chunks"][0]["embedded"] is True, "upload should have embedded it"

    listing = client.get("/documents", headers=h).json()
    assert listing["total"] >= 2
    assert listing["documents"][0]["id"] is not None

    # Rejections: disallowed type, empty file, missing auth, unknown id.
    assert (
        client.post(
            "/documents/upload",
            headers=h,
            files={"file": ("evil.exe", b"MZ\x90", "application/octet-stream")},
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/documents/upload",
            headers=h,
            files={"file": ("empty.txt", b"", "text/plain")},
        ).status_code
        == 400
    )
    assert client.get("/documents").status_code == 401
    assert client.get("/documents/999999", headers=h).status_code == 404

    # An image uploads, gets OCR'd, and is chunked like any other document.
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
        b"\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
        b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    image = client.post(
        "/documents/upload", headers=h, files={"file": ("ticket.png", png, "image/png")}
    )
    assert image.status_code == 201, image.text
    img_doc = image.json()["document"]
    assert img_doc["chunk_count"] >= 1, img_doc
    assert img_doc["status"] == "embedded", img_doc
    assert "OCR" in (img_doc["note"] or ""), img_doc["note"]

    # A disguised executable is rejected even though .png is allowed — the
    # content-type header says image/png and it is still refused.
    assert (
        client.post(
            "/documents/upload",
            headers=h,
            files={"file": ("evil.png", b"MZ\x90\x00" + b"\x00" * 60, "image/png")},
        ).status_code
        == 422
    )

    # A path-traversal filename is accepted only as a name; the stored key is
    # derived from the content hash, so it cannot escape the bucket prefix.
    sneaky = client.post(
        "/documents/upload",
        headers=h,
        files={"file": ("../../etc/passwd.txt", b"root:x:0:0", "text/plain")},
    )
    assert sneaky.status_code == 201, sneaky.text
    stored = sneaky.json()["document"]
    assert stored["sha256"] in _object_key_of(stored["id"])
    assert _object_key_of(stored["id"]).startswith("documents/")
    assert ".." not in _object_key_of(stored["id"])


def _object_key_of(document_id: int) -> str:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Document

    with SessionLocal() as db:
        return db.scalar(select(Document.object_key).where(Document.id == document_id))


def check_search() -> None:
    h = bearer(login("ops@agfabric.test"))

    # Two documents with distinct vocabulary, so ranking is falsifiable.
    corn = (
        b"Bin ELK-04 corn moisture reading was 16.4 percent on arrival.\n"
        b"Aeration was scheduled to dry the corn before long term storage.\n"
    )
    invoice = (
        b"Invoice INV-5099 payment terms are net thirty days from issue.\n"
        b"Remit payment to the accounts receivable department by cheque.\n"
    )
    for name, blob in (("moisture-note.txt", corn), ("payment-terms.txt", invoice)):
        r = client.post(
            "/documents/upload", headers=h, files={"file": (name, blob, "text/plain")}
        )
        assert r.status_code == 201, r.text
        assert r.json()["index_error"] is None, r.json()["index_error"]
        assert r.json()["chunks_indexed"] >= 1
        assert r.json()["document"]["status"] == "embedded"

    st = client.get("/search/status", headers=h).json()
    assert st["provider"] == "fake", f"expected the fake provider in tests, got {st}"
    assert st["vectors"] >= 2
    assert st["chunks_pending"] is False

    found = client.post(
        "/search", headers=h, json={"query": "corn moisture reading", "limit": 5}
    )
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["count"] >= 1
    assert body["provider"] == "fake" and body["dimensions"] == 1536
    assert body["took_ms"] >= 0

    # The moisture note must outrank the payment terms for this query.
    top = body["results"][0]
    assert top["source"]["filename"] == "moisture-note.txt", [
        (r["source"]["filename"], r["score"]) for r in body["results"]
    ]
    # Scores are ordered best-first.
    scores = [r["score"] for r in body["results"]]
    assert scores == sorted(scores, reverse=True), scores

    # Full traceability on every hit, and full text not just the preview.
    for result in body["results"]:
        src = result["source"]
        assert src["document_id"] and src["filename"] and src["sha256"]
        assert src["chunk_ordinal"] is not None and src["chunk_id"]
        assert result["text"]
    assert "moisture" in top["text"].lower()

    # The opposite query flips the ranking.
    other = client.post(
        "/search", headers=h, json={"query": "invoice payment terms net thirty"}
    ).json()
    assert other["results"][0]["source"]["filename"] == "payment-terms.txt", [
        (r["source"]["filename"], r["score"]) for r in other["results"]
    ]

    # document_id scopes the search to one document.
    only = client.post(
        "/search",
        headers=h,
        json={"query": "corn moisture", "document_id": top["source"]["document_id"]},
    ).json()
    assert only["count"] >= 1
    assert {r["source"]["document_id"] for r in only["results"]} == {
        top["source"]["document_id"]
    }

    # Rejections: empty, whitespace-only, oversized, bad limit, no auth.
    assert client.post("/search", headers=h, json={"query": ""}).status_code == 422
    assert client.post("/search", headers=h, json={"query": "   "}).status_code == 422
    assert (
        client.post("/search", headers=h, json={"query": "x" * 5000}).status_code == 422
    )
    assert (
        client.post("/search", headers=h, json={"query": "ok", "limit": 0}).status_code
        == 422
    )
    assert (
        client.post("/search", headers=h, json={"query": "ok", "limit": 999}).status_code
        == 422
    )
    assert client.post("/search", json={"query": "ok"}).status_code == 401

    # Reindex is idempotent and restricted: warehouse has no business doing it.
    assert (
        client.post(
            "/search/reindex", headers=bearer(login("warehouse@agfabric.test"))
        ).status_code
        == 403
    )
    again = client.post("/search/reindex", headers=h)
    assert again.status_code == 200, again.text
    assert again.json()["chunks_indexed"] == 0, "nothing should be left to index"
    assert again.json()["failures"] == []


def check_query() -> None:
    exec_h = bearer(login("exec@agfabric.test"))

    # A structured question: the contract number resolves to a real row, the
    # graph adds its neighbours, and vector search adds any relevant documents.
    r = client.post(
        "/query",
        headers=exec_h,
        json={"question": "what is the status of contract C-2026-1000?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    exp = body["explanation"]

    assert exp["resolved"]["identifiers"] == {"contract": ["C-2026-1000"]}, exp
    sql = exp["sql_evidence"]
    assert any(
        rec["kind"] == "contract" and rec["number"] == "C-2026-1000" for rec in sql
    )
    contract = next(rec for rec in sql if rec["kind"] == "contract")
    assert contract["status"] == "open"
    # Exec is a finance role, so the price is present rather than redacted.
    assert contract["price_per_bu"] != "redacted"

    # The graph contributed related entities.
    assert exp["graph_relationships"], "graph should surface neighbours"
    labels = {t["relationship"] for t in exp["graph_relationships"]}
    assert "SIGNED" in labels or "FOR_COMMODITY" in labels, labels

    # The envelope carries everything the plan asks for.
    assert body["answer"]
    assert 0 < body["confidence"] <= 0.99
    assert body["model"]["provider"] == "fake"
    assert body["model"]["chat_model"] and body["model"]["embedding_model"]
    assert body["model"]["input_tokens"] > 0
    assert body["model"]["cost_usd"] == 0.0, "the fake provider must be free"
    assert body["took_ms"] >= 0
    assert exp["retrieval_error"] is None

    # Grounding: the answer quotes retrieved content, it is not invented.
    assert "C-2026-1000" in body["answer"], body["answer"]

    # A customer name resolves without any identifier present.
    named = client.post(
        "/query", headers=exec_h, json={"question": "what contracts does Halvorsen have?"}
    ).json()
    customers = named["explanation"]["resolved"]["customers"]
    assert customers and "Halvorsen" in customers[0]["name"], customers
    assert any(rec["kind"] == "customer" for rec in named["explanation"]["sql_evidence"])

    # A document-only question falls through to vector retrieval.
    doc_q = client.post(
        "/query", headers=exec_h, json={"question": "corn moisture reading percent"}
    ).json()
    assert doc_q["explanation"]["retrieved_chunks"], "should retrieve document chunks"
    top = doc_q["explanation"]["retrieved_chunks"][0]
    assert top["source"]["filename"] and top["source"]["sha256"]

    # RBAC: warehouse must not see the contract price. Same rule as /dashboard.
    wh = client.post(
        "/query",
        headers=bearer(login("warehouse@agfabric.test")),
        json={"question": "what is the status of contract C-2026-1000?"},
    ).json()
    assert wh["explanation"]["financials_visible"] is False
    wh_contract = next(
        rec for rec in wh["explanation"]["sql_evidence"] if rec["kind"] == "contract"
    )
    assert wh_contract["price_per_bu"] == "redacted", wh_contract
    # Non-financial fields are still available to them.
    assert wh_contract["status"] == "open"

    # Nothing matches -> zero confidence and a refusal, not a fabrication.
    miss = client.post(
        "/query", headers=exec_h, json={"question": "zzzqqq nonexistent xyzzy"}
    ).json()
    assert miss["confidence"] == 0.0, miss
    assert not miss["explanation"]["sql_evidence"]

    # graph_depth=0 skips graph expansion entirely.
    nog = client.post(
        "/query",
        headers=exec_h,
        json={"question": "contract C-2026-1000", "graph_depth": 0},
    ).json()
    assert nog["explanation"]["graph_relationships"] == []

    # Rejections mirror /search.
    assert client.post("/query", headers=exec_h, json={"question": ""}).status_code == 422
    assert (
        client.post("/query", headers=exec_h, json={"question": "  "}).status_code == 422
    )
    assert (
        client.post("/query", headers=exec_h, json={"question": "x" * 5000}).status_code
        == 422
    )
    assert (
        client.post(
            "/query", headers=exec_h, json={"question": "ok", "graph_depth": 9}
        ).status_code
        == 422
    )
    assert client.post("/query", json={"question": "ok"}).status_code == 401


def check_text_to_sql() -> None:
    exec_h = bearer(login("exec@agfabric.test"))

    # The whole point of text-to-SQL: an aggregate the fixed lookups cannot express.
    r = client.post(
        "/query",
        headers=exec_h,
        json={"question": "what is the total invoice amount by status?"},
    )
    assert r.status_code == 200, r.text
    gen = r.json()["explanation"]["generated_sql"]
    assert gen["attempted"] is True
    assert gen["rejected"] is None, gen["rejected"]
    assert gen["error"] is None, gen["error"]
    assert "SUM(amount)" in gen["sql"], gen["sql"]
    assert gen["row_count"] >= 1, gen
    assert "total_amount" in gen["columns"], gen["columns"]
    # Real numbers came back, and they reached the answer.
    assert all(isinstance(row["total_amount"], (int, float)) for row in gen["rows"])
    assert r.json()["confidence"] > 0
    # Two LLM calls now: one for SQL, one for the answer.
    assert r.json()["model"]["llm_calls"] == 2

    # A ranking question — also impossible before.
    ranked = client.post(
        "/query",
        headers=exec_h,
        json={"question": "which customer delivered the most bushels?"},
    ).json()["explanation"]["generated_sql"]
    assert ranked["error"] is None and ranked["row_count"] >= 1, ranked
    assert "total_bu" in ranked["columns"], ranked["columns"]

    # An unanswerable question yields NO_QUERY, which the gate rejects cleanly
    # rather than executing or crashing.
    unknown = client.post(
        "/query", headers=exec_h, json={"question": "what is the capital of France?"}
    ).json()["explanation"]["generated_sql"]
    assert unknown["raw"] == "NO_QUERY"
    assert unknown["sql"] is None
    assert "unanswerable" in unknown["rejected"], unknown


def check_sql_readonly_enforced() -> None:
    """The defence that does not depend on my regexes being complete.

    Bypasses the gate entirely and hands writes straight to the executor. Postgres
    must refuse them because the transaction is READ ONLY.
    """
    from app import sqlgen

    for write in (
        "DELETE FROM invoices",
        "UPDATE invoices SET amount = 0",
        "INSERT INTO customers (name, kind) VALUES ('evil', 'farmer')",
        "DROP TABLE document_chunks",
    ):
        result = sqlgen.run(write)
        assert result.error is not None, f"database ALLOWED a write: {write}"
        assert result.rows == []

    # Confirm nothing was actually written.
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import Customer, Invoice

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Invoice)) > 0
        assert not db.scalar(select(Customer).where(Customer.name == "evil"))

    # A legitimate read still works through the same path.
    ok = sqlgen.run("SELECT COUNT(*) AS n FROM invoices")
    assert ok.error is None and ok.rows[0]["n"] > 0, ok

    # The users table is unreachable even though it exists in the database.
    blocked = 0
    for sql in ("SELECT * FROM users", "SELECT email FROM users LIMIT 1"):
        try:
            sqlgen.validate(sql)
        except sqlgen.UnsafeSQL:
            blocked += 1
    assert blocked == 2, "users table must be gated before it ever reaches Postgres"


def check_agents() -> None:
    ops_h = bearer(login("ops@agfabric.test"))

    listing = client.get("/agents", headers=ops_h)
    assert listing.status_code == 200, listing.text
    names = {a["name"] for a in listing.json()["agents"]}
    assert names == {
        "risk",
        "embedding",
        "entity_resolution",
        "weather",
        "market",
        "fx",
        "news",
        "monitoring",
    }, names
    # Every agent is on a timer, so nothing needs a manual run for the app to
    # have data. Derived from the registry rather than a pinned set: the point of
    # the check is that `scheduled` reflects the intervals actually configured,
    # not that the list matches a literal written here months ago.
    scheduled = {a["name"] for a in listing.json()["agents"] if a["scheduled"]}
    assert scheduled == set(SCHEDULED), scheduled
    assert scheduled == names, (
        f"every agent should be scheduled, missing {names - scheduled}"
    )
    # A scheduled agent without an interval would never actually fire.
    assert all(
        REGISTRY[n].interval_seconds and REGISTRY[n].interval_seconds > 0
        for n in scheduled
    )

    # Run each one and check it recorded a real outcome. `weather`, `market`, `fx`
    # and `news` are skipped: all four call third-party APIs, and a network
    # dependency would make this flaky. Their parsers are unit-tested instead.
    for name in sorted(names - {"weather", "market", "fx", "news"}):
        run = client.post(f"/agents/{name}/run", headers=ops_h)
        assert run.status_code == 200, run.text
        body = run.json()
        assert body["agent"] == name
        assert body["status"] == "ok", body["error"]
        assert body["trigger"] == "manual"
        assert body["duration_ms"] >= 0
        assert body["detail"] is not None

    # The registry reports a last run for everything that was run.
    after = client.get("/agents", headers=ops_h).json()["agents"]
    ran = [a for a in after if a["name"] not in ("weather", "market", "fx", "news")]
    assert all(a["last_run"] is not None for a in ran), ran
    assert all(a["last_run"]["status"] == "ok" for a in ran)

    # Run history is bounded — the scheduler would otherwise append forever.
    from sqlalchemy import func as sqlfunc
    from sqlalchemy import select as sqlselect

    from app.config import settings as cfg
    from app.db import SessionLocal as SL
    from app.models import AgentRun

    for _ in range(3):
        client.post("/agents/monitoring/run", headers=ops_h)
    with SL() as db:
        kept = db.scalar(
            sqlselect(sqlfunc.count())
            .select_from(AgentRun)
            .where(AgentRun.agent == "monitoring")
        )
    assert kept <= cfg.agent_run_retention, kept

    # Run history is queryable and filterable.
    runs = client.get("/agents/runs?agent=risk&limit=5", headers=ops_h).json()["runs"]
    assert runs and all(r["agent"] == "risk" for r in runs)

    # An unknown agent is a 404, not a 500.
    assert client.post("/agents/nope/run", headers=ops_h).status_code == 404
    # Running agents is restricted; warehouse may look but not trigger.
    wh_h = bearer(login("warehouse@agfabric.test"))
    assert client.post("/agents/risk/run", headers=wh_h).status_code == 403
    assert client.get("/agents", headers=wh_h).status_code == 200
    assert client.get("/agents").status_code == 401

    # entity_resolution is report-only: it must not have rewritten anything.
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import Delivery

    with SessionLocal() as db:
        before = db.scalar(
            select(func.count())
            .select_from(Delivery)
            .where(Delivery.contract_id.is_(None))
        )
    client.post("/agents/entity_resolution/run", headers=ops_h)
    with SessionLocal() as db:
        after_count = db.scalar(
            select(func.count())
            .select_from(Delivery)
            .where(Delivery.contract_id.is_(None))
        )
    assert before == after_count, "entity_resolution must not mutate deliveries"


def check_persisted_alerts() -> None:
    acct_h = bearer(login("accounting@agfabric.test"))
    ops_h = bearer(login("ops@agfabric.test"))

    first = client.post("/agents/risk/run", headers=ops_h).json()
    assert first["status"] == "ok", first["error"]

    listing = client.get("/alerts", headers=acct_h)
    assert listing.status_code == 200, listing.text
    alerts = listing.json()["alerts"]
    kinds = {a["kind"] for a in alerts}
    # The seed plants one of each of all six conditions on purpose.
    for expected in (
        "duplicate_invoice",
        "inventory_mismatch",
        "moisture_anomaly",
        "contract_expiration",
        "missing_deliveries",
        "data_inconsistency",
    ):
        assert expected in kinds, f"{expected} missing; got {kinds}"

    # Persisted alerts have identity and history, unlike the old computed ones.
    for a in alerts:
        assert a["id"] and a["status"] == "open"
        assert a["first_seen_at"] and a["last_seen_at"]
        assert a["resolved_at"] is None
        assert a["evidence"] and a["recommendation"]

    # Severity order preserved.
    order = {"high": 0, "medium": 1, "low": 2}
    sev = [order[a["severity"]] for a in alerts]
    assert sev == sorted(sev), sev

    # Re-running must update in place, never duplicate. This is what the
    # fingerprint exists for.
    count_before = len(alerts)
    second = client.post("/agents/risk/run", headers=ops_h).json()
    assert second["detail"]["created"] == 0, second["detail"]
    assert second["detail"]["updated"] > 0, second["detail"]
    assert second["detail"]["auto_resolved"] == 0, second["detail"]
    assert len(client.get("/alerts", headers=acct_h).json()["alerts"]) == count_before

    # Acknowledge moves status but keeps the alert.
    target = alerts[0]["id"]
    ack = client.post(f"/alerts/{target}/acknowledge", headers=acct_h)
    assert ack.status_code == 200, ack.text
    assert ack.json()["status"] == "acknowledged"
    assert target not in {
        a["id"] for a in client.get("/alerts", headers=acct_h).json()["alerts"]
    }
    assert target in {
        a["id"]
        for a in client.get("/alerts?status=acknowledged", headers=acct_h).json()[
            "alerts"
        ]
    }

    # A scan must not undo a human's acknowledgement of a live condition.
    client.post("/agents/risk/run", headers=ops_h)
    still = client.get("/alerts?status=acknowledged", headers=acct_h).json()["alerts"]
    assert target in {a["id"] for a in still}, "scan reverted an acknowledgement"

    # Resolve sets a timestamp, and a resolved alert cannot be acknowledged.
    res = client.post(f"/alerts/{target}/resolve", headers=acct_h)
    assert res.status_code == 200 and res.json()["status"] == "resolved"
    assert res.json()["resolved_at"] is not None
    assert client.post(f"/alerts/{target}/acknowledge", headers=acct_h).status_code == 409

    # Filters, and RBAC: warehouse must not see duplicate-invoice amounts.
    high = client.get("/alerts?severity=high", headers=acct_h).json()["alerts"]
    assert all(a["severity"] == "high" for a in high)
    assert (
        client.get("/alerts?kind=moisture_anomaly", headers=acct_h).json()["count"] >= 1
    )

    wh = client.get("/alerts", headers=bearer(login("warehouse@agfabric.test"))).json()
    assert not any(a["kind"] == "duplicate_invoice" for a in wh["alerts"])

    # A finance-only alert 404s for warehouse rather than confirming it exists.
    dupes = client.get("/alerts?status=all&kind=duplicate_invoice", headers=acct_h).json()
    if dupes["alerts"]:
        dupe_id = dupes["alerts"][0]["id"]
        wh_h = bearer(login("warehouse@agfabric.test"))
        assert (
            client.post(f"/alerts/{dupe_id}/acknowledge", headers=wh_h).status_code == 404
        )

    assert client.get("/alerts").status_code == 401
    assert client.post("/alerts/999999/resolve", headers=acct_h).status_code == 404


def _sign(body: bytes) -> str:
    import hashlib
    import hmac

    return hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()


def _post_webhook(payload: dict, event_id: str | None = None, sig: str | None = None):
    import json

    raw = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-AgFabric-Signature": sig if sig is not None else f"sha256={_sign(raw)}",
    }
    if event_id:
        headers["X-AgFabric-Event-Id"] = event_id
    return client.post("/webhooks/deliveries", content=raw, headers=headers)


def check_webhooks() -> None:
    assert settings.webhook_secret, "set WEBHOOK_SECRET to run the webhook checks"

    payload = {
        "event": "delivery.recorded",
        "source": "scale-house-1",
        "data": {
            "ticket_number": "WH-90001",
            "customer": "Halvorsen Family Farms",
            "commodity": "Corn",
            "facility": "Chicago Terminal",
            "truck_id": "IL-4242",
            "gross_lbs": "62000",
            "tare_lbs": "31000",
            "moisture_pct": "13.9",
            "delivered_at": "2026-07-26T14:30:00Z",
        },
    }

    ok = _post_webhook(payload, event_id="evt-1")
    assert ok.status_code == 202, ok.text
    body = ok.json()
    assert body["duplicate"] is False and body["status"] == "processed", body
    # (62000 - 31000) / 56 = 553.57
    assert body["result"]["net_bu"] == "553.57", body["result"]
    assert body["result"]["ticket"] == "WH-90001"

    # The delivery really landed, and is unverified until told otherwise.
    from sqlalchemy import select as sqlselect

    from app.db import SessionLocal as SL
    from app.models import Delivery

    with SL() as db:
        row = db.scalar(sqlselect(Delivery).where(Delivery.ticket_number == "WH-90001"))
    assert row is not None and row.verified is False

    # Replay with the same event id is a no-op reporting the original outcome.
    replay = _post_webhook(payload, event_id="evt-1")
    assert replay.status_code == 202
    assert replay.json()["duplicate"] is True, replay.json()
    assert replay.json()["status"] == "processed"

    # A different event id but the same ticket is caught at the business level.
    resent = _post_webhook(payload, event_id="evt-2")
    assert resent.json()["duplicate"] is False
    assert resent.json()["status"] == "ignored", resent.json()

    # The follow-up event flips verification.
    verify = _post_webhook(
        {"event": "delivery.verified", "data": {"ticket_number": "WH-90001"}},
        event_id="evt-3",
    )
    assert verify.json()["status"] == "processed", verify.json()
    with SL() as db:
        assert db.scalar(
            sqlselect(Delivery).where(Delivery.ticket_number == "WH-90001")
        ).verified

    # Authentication: wrong signature, and no signature at all.
    assert (
        _post_webhook(payload, event_id="evt-4", sig="sha256=" + "0" * 64).status_code
        == 401
    )
    import json as _json

    raw = _json.dumps(payload).encode()
    assert client.post("/webhooks/deliveries", content=raw).status_code == 401

    # A tampered body fails even with a signature for the original body.
    tampered = dict(payload)
    tampered["data"] = {**payload["data"], "gross_lbs": "999999"}
    assert (
        _post_webhook(tampered, event_id="evt-5", sig=f"sha256={_sign(raw)}").status_code
        == 401
    )

    # Validation: unknown event type, bad JSON, non-object body.
    assert (
        _post_webhook({"event": "nope", "data": {}}, event_id="evt-6").status_code == 422
    )
    bad_raw = b"{not json"
    assert (
        client.post(
            "/webhooks/deliveries",
            content=bad_raw,
            headers={"X-AgFabric-Signature": _sign(bad_raw)},
        ).status_code
        == 400
    )

    # A payload naming an unknown customer is recorded as failed, not silently
    # creating a customer — and the event row survives for replay.
    unknown = {
        "event": "delivery.recorded",
        "data": {
            **payload["data"],
            "ticket_number": "WH-90002",
            "customer": "Nobody Inc",
        },
    }
    failed = _post_webhook(unknown, event_id="evt-7")
    assert failed.json()["status"] == "failed", failed.json()
    assert "unknown customer" in failed.json()["result"]["error"]

    # Physically impossible weights are rejected too.
    bad_weights = {
        "event": "delivery.recorded",
        "data": {**payload["data"], "ticket_number": "WH-90003", "tare_lbs": "99999"},
    }
    assert _post_webhook(bad_weights, event_id="evt-8").json()["status"] == "failed"

    # Every event was persisted, including the failures.
    from app.models import WebhookEvent

    with SL() as db:
        events = db.scalars(sqlselect(WebhookEvent)).all()
    statuses = {e.dedupe_key: e.status for e in events}
    assert statuses.get("evt-1") == "processed"
    assert statuses.get("evt-2") == "ignored"
    assert statuses.get("evt-7") == "failed"
    assert all(e.payload for e in events), "payloads must be retained for replay"


def check_audit() -> None:
    exec_h = bearer(login("exec@agfabric.test"))
    wh_h = bearer(login("warehouse@agfabric.test"))

    # A query must leave an audit row with the full evidence trail.
    q = client.post(
        "/query",
        headers=exec_h,
        json={"question": "what is the status of contract C-2026-1000?"},
    )
    assert q.status_code == 200, q.text
    # Every response carries a correlation id, echoed back.
    request_id = q.headers.get("X-Request-Id")
    assert request_id and len(request_id) >= 8, q.headers

    listing = client.get("/audit", headers=exec_h)
    assert listing.status_code == 200, listing.text
    entries = listing.json()["entries"]
    assert entries, "the query should have been audited"
    assert listing.json()["scoped_to_self"] is False, "exec sees everything"

    latest = entries[0]
    assert latest["request_id"] == request_id, (latest["request_id"], request_id)
    assert "C-2026-1000" in latest["question"]
    assert latest["endpoint"] == "/query"
    assert latest["user"] == "exec@agfabric.test" and latest["role"] == "exec"
    assert latest["took_ms"] > 0
    # Finance roles see the cost figures.
    assert "cost_usd" in latest and "input_tokens" in latest
    assert latest["input_tokens"] > 0

    # The detail view carries the evidence needed to reproduce the answer.
    full = client.get(f"/audit/{latest['id']}", headers=exec_h).json()
    assert full["answer"], full
    assert full["sources"] is not None
    assert "resolved" in full["sources"]
    assert full["embedding_model"]

    # An aggregate question records the SQL that was actually run.
    client.post(
        "/query", headers=exec_h, json={"question": "total invoice amount by status?"}
    )
    with_sql = [
        e
        for e in client.get("/audit", headers=exec_h).json()["entries"]
        if "total invoice" in e["question"]
    ]
    assert with_sql, "aggregate query not audited"
    detail = client.get(f"/audit/{with_sql[0]['id']}", headers=exec_h).json()
    assert detail["generated_sql"] and "SUM(amount)" in detail["generated_sql"]

    # Spend rollup, finance only.
    summary = client.get("/audit/summary?since_hours=24", headers=exec_h)
    assert summary.status_code == 200, summary.text
    s = summary.json()
    assert s["requests"] >= 2
    assert s["input_tokens"] > 0
    assert s["cost_usd"] == 0.0, "fake provider must not report spend"
    assert any(u["user"] == "exec@agfabric.test" for u in s["by_user"])
    assert any(e["endpoint"] == "/query" for e in s["by_endpoint"])
    assert client.get("/audit/summary", headers=wh_h).status_code == 403

    # Warehouse is scoped to its own requests and sees no cost.
    scoped = client.get("/audit", headers=wh_h).json()
    assert scoped["scoped_to_self"] is True
    assert all(e["user"] == "warehouse@agfabric.test" for e in scoped["entries"])
    assert all("cost_usd" not in e for e in scoped["entries"])
    # And cannot read another user's entry — 404, not 403, so it does not confirm
    # the entry exists.
    assert client.get(f"/audit/{latest['id']}", headers=wh_h).status_code == 404
    assert client.get("/audit").status_code == 401
    assert client.get("/audit/999999", headers=exec_h).status_code == 404

    # An inbound correlation id is honoured rather than replaced.
    supplied = "abc123def456"
    echoed = client.post(
        "/query",
        headers={**exec_h, "X-Request-Id": supplied},
        json={"question": "contract C-2026-1000"},
    )
    assert echoed.headers["X-Request-Id"] == supplied


def check_metrics() -> None:
    exec_h = bearer(login("exec@agfabric.test"))
    client.post("/agents/monitoring/run", headers=exec_h)

    r = client.get("/metrics")
    assert r.status_code == 200, r.text
    body = r.text

    for metric in (
        "agfabric_http_requests_total",
        "agfabric_http_request_duration_seconds_bucket",
        "agfabric_llm_tokens_total",
        "agfabric_agent_runs_total",
        "agfabric_agent_duration_seconds_bucket",
        "agfabric_open_alerts",
        "agfabric_documents_total",
        "agfabric_chunks_pending_embedding",
        "agfabric_audit_cost_usd_lifetime",
    ):
        assert metric in body, f"{metric} missing from /metrics"

    # Paths are route templates. A raw id in a label would mint one series per
    # document and eventually take the scraper down.
    assert 'path="/documents/{document_id}"' in body or "/documents" in body
    assert "/documents/1" not in body, "raw path leaked into a metric label"

    # The gauges reflect real state, not zeros.
    assert "agfabric_documents_total " in body
    docs = float(
        next(
            line.split()[-1]
            for line in body.splitlines()
            if line.startswith("agfabric_documents_total ")
        )
    )
    assert docs > 0, "documents gauge should be non-zero after uploads"

    # Agent counters are labelled and non-zero after the runs above.
    assert 'agfabric_agent_runs_total{agent="monitoring",status="ok"}' in body

    # Token counter moved because /query ran during check_audit.
    assert 'agfabric_llm_tokens_total{kind="input"' in body


def check_login_rate_limit() -> None:
    # Runs last: it deliberately exhausts the window for this client address.
    login_limiter.reset()
    codes = [
        client.post(
            "/login", data={"username": "ops@agfabric.test", "password": "wrong"}
        ).status_code
        for _ in range(login_limiter.limit + 2)
    ]
    assert codes[: login_limiter.limit] == [401] * login_limiter.limit, codes
    assert codes[login_limiter.limit :] == [429, 429], codes

    blocked = client.post(
        "/login",
        data={"username": "ops@agfabric.test", "password": settings.seed_password},
    )
    # Correct credentials are still refused while throttled, with Retry-After.
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0

    login_limiter.reset()
    assert (
        client.post(
            "/login",
            data={"username": "ops@agfabric.test", "password": settings.seed_password},
        ).status_code
        == 200
    )


if __name__ == "__main__":
    for check in (
        check_health,
        check_auth,
        check_dashboard_and_rbac,
        check_storage,
        check_graph,
        check_documents,
        check_search,
        check_query,
        check_text_to_sql,
        check_sql_readonly_enforced,
        check_agents,
        check_persisted_alerts,
        check_webhooks,
        check_audit,
        check_metrics,
        check_login_rate_limit,
    ):
        check()
        print(f"ok  {check.__name__}")
    print("smoke test passed")
