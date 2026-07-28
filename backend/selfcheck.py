"""Runnable check for the logic that can actually be wrong: password hashing,
token handling, and the risk rules. No DB, no framework.

    python selfcheck.py
"""

import logging
import os

# Set before importing app.config, which refuses to load without a secret.
os.environ.setdefault("JWT_SECRET", "selfcheck-secret-that-is-long-enough-000000")
# Pin the fakes before app.config loads. Without this, a real OPENAI_API_KEY in
# the environment makes `auto` resolve to openai, and a supposedly offline,
# free check suite starts billing tokens.
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["OCR_PROVIDER"] = "fake"
# The corrupt-file checks feed pypdf deliberate garbage; its complaints are the
# expected result, not output worth printing.
logging.getLogger("pypdf").setLevel(logging.CRITICAL)

from dataclasses import dataclass  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402

import jwt  # noqa: E402

from app import extract, llm, resolve, risk, sqlgen  # noqa: E402
from app import graph as kg  # noqa: E402
from app.auth import hash_password, make_token, read_token, verify_password  # noqa: E402
from app.chunk import chunk_text  # noqa: E402
from app.embed import FakeEmbedder  # noqa: E402
from app.ratelimit import SlidingWindow  # noqa: E402


@dataclass
class Inv:
    number: str
    customer_id: int
    amount: Decimal
    issued_date: date


@dataclass
class Bin:
    name: str
    capacity_bu: Decimal
    current_bu: Decimal
    moisture_pct: Decimal | None


@dataclass
class Con:
    number: str
    status: str
    end_date: date


@dataclass
class FakeUser:
    id: int = 1
    email: str = "a@b.test"
    role: str = "ops"


def check_passwords() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)
    assert not verify_password("", stored)
    # A second hash of the same password must differ (unique salt).
    assert stored != hash_password("correct horse battery staple")
    # Malformed or foreign hashes must be rejected, never crash.
    for junk in ("", "notahash", "scrypt$abc", "bcrypt$aa$bb", "scrypt$!!$!!"):
        assert not verify_password("anything", junk), junk


def check_tokens() -> None:
    token = make_token(FakeUser(id=7, role="exec"))
    claims = read_token(token)
    assert claims["sub"] == "7" and claims["role"] == "exec"

    # A token signed with a different secret must not validate.
    forged = jwt.encode(
        {"sub": "7", "role": "exec"},
        "a-different-secret-of-adequate-length-000000",
        algorithm="HS256",
    )
    try:
        read_token(forged)
        raise AssertionError("forged token was accepted")
    except jwt.InvalidTokenError:
        pass

    expired = jwt.encode(
        {"sub": "7", "exp": 1_600_000_000}, os.environ["JWT_SECRET"], algorithm="HS256"
    )
    try:
        read_token(expired)
        raise AssertionError("expired token was accepted")
    except jwt.ExpiredSignatureError:
        pass


def check_duplicate_invoices() -> None:
    d = date(2026, 6, 1)
    amt = Decimal("12500.00")

    pair = [Inv("A", 1, amt, d), Inv("B", 1, amt, d + timedelta(days=3))]
    hits = risk.duplicate_invoices(pair)
    assert len(hits) == 1, hits
    assert hits[0]["severity"] == "high"
    assert set(hits[0]["evidence"]["invoices"]) == {"A", "B"}
    assert 0.0 < hits[0]["confidence"] <= 0.95

    # Different customers, same amount -> not a duplicate.
    assert not risk.duplicate_invoices([Inv("A", 1, amt, d), Inv("B", 2, amt, d)])
    # Same customer, different amounts -> not a duplicate.
    assert not risk.duplicate_invoices(
        [Inv("A", 1, amt, d), Inv("B", 1, Decimal("999.00"), d)]
    )
    # Outside the window -> not a duplicate.
    assert not risk.duplicate_invoices(
        [Inv("A", 1, amt, d), Inv("B", 1, amt, d + timedelta(days=45))]
    )
    # Wider gap inside the window is lower confidence than a same-day pair.
    near = risk.duplicate_invoices([Inv("A", 1, amt, d), Inv("B", 1, amt, d)])[0]
    far = risk.duplicate_invoices(
        [Inv("A", 1, amt, d), Inv("B", 1, amt, d + timedelta(days=28))]
    )[0]
    assert near["confidence"] > far["confidence"]
    # Three in a window -> two consecutive pairs, not one.
    triple = risk.duplicate_invoices(
        [
            Inv("A", 1, amt, d),
            Inv("B", 1, amt, d + timedelta(days=2)),
            Inv("C", 1, amt, d + timedelta(days=4)),
        ]
    )
    assert len(triple) == 2, triple
    assert not risk.duplicate_invoices([])


def check_bin_anomalies() -> None:
    over = Bin("X", Decimal("100"), Decimal("120"), Decimal("12.0"))
    wet = Bin("Y", Decimal("100"), Decimal("50"), Decimal("16.4"))
    fine = Bin("Z", Decimal("100"), Decimal("100"), Decimal("15.0"))
    none_moisture = Bin("W", Decimal("100"), Decimal("10"), None)

    kinds = [a["kind"] for a in risk.bin_anomalies([over, wet, fine, none_moisture])]
    assert kinds == ["inventory_mismatch", "moisture_anomaly"], kinds
    # Exactly at capacity and exactly at the ceiling are not alerts.
    assert not risk.bin_anomalies([fine, none_moisture])


def check_fingerprints() -> None:
    fp = risk.fingerprint

    # Stable across calls: the same condition must key the same on every scan,
    # or every scan would insert a fresh duplicate alert.
    assert fp("moisture_anomaly", "ELK-04") == fp("moisture_anomaly", "ELK-04")
    # Distinct per kind and per subject.
    assert fp("moisture_anomaly", "ELK-04") != fp("moisture_anomaly", "ELK-01")
    assert fp("moisture_anomaly", "ELK-04") != fp("inventory_mismatch", "ELK-04")
    # Multi-part keys are order-sensitive, which is why callers sort pairs.
    assert fp("duplicate_invoice", "A", "B") != fp("duplicate_invoice", "B", "A")
    assert len(fp("x", 1)) == 32

    # Every rule must emit one, or reconciliation cannot dedupe its output.
    today = date(2026, 7, 26)
    amt = Decimal("100.00")
    produced = [
        *risk.duplicate_invoices([Inv("A", 1, amt, today), Inv("B", 1, amt, today)]),
        *risk.bin_anomalies([Bin("X", Decimal("10"), Decimal("20"), Decimal("16"))]),
        *risk.expiring_contracts([Con("C-1", "open", today)], today),
    ]
    assert produced
    for alert in produced:
        assert alert["fingerprint"], alert


@dataclass
class Deliv:
    ticket_number: str
    gross_lbs: Decimal
    tare_lbs: Decimal
    net_bu: Decimal
    commodity_id: int = 1
    contract_id: int | None = None


@dataclass
class Contr:
    id: int
    number: str
    status: str
    start_date: date
    end_date: date
    quantity_bu: Decimal = Decimal("1000")


def check_missing_deliveries() -> None:
    today = date(2026, 7, 26)
    stale = Contr(
        1, "C-1", "open", today - timedelta(days=200), today + timedelta(days=20)
    )
    fresh = Contr(
        2, "C-2", "open", today - timedelta(days=10), today + timedelta(days=90)
    )
    closed = Contr(3, "C-3", "closed", today - timedelta(days=200), today)
    served = Contr(
        4, "C-4", "open", today - timedelta(days=200), today + timedelta(days=20)
    )
    future = Contr(
        5, "C-5", "open", today + timedelta(days=30), today + timedelta(days=200)
    )

    deliveries = [
        Deliv("T-1", Decimal("60000"), Decimal("30000"), Decimal("535.71"), 1, 4)
    ]
    hits = risk.missing_deliveries(
        [stale, fresh, closed, served, future], deliveries, today
    )

    numbers = [h["evidence"]["contract"] for h in hits]
    assert numbers == ["C-1"], numbers
    # 200 of 220 days elapsed with nothing shipped -> high.
    assert hits[0]["severity"] == "high", hits[0]
    assert hits[0]["evidence"]["days_since_start"] == 200

    # Inside the grace period nothing fires, however long the contract runs.
    assert not risk.missing_deliveries([fresh], [], today)
    # A contract that has not started cannot be late.
    assert not risk.missing_deliveries([future], [], today)
    # Early in a long window is medium, not high.
    early = Contr(
        6, "C-6", "open", today - timedelta(days=60), today + timedelta(days=600)
    )
    assert risk.missing_deliveries([early], [], today)[0]["severity"] == "medium"
    assert not risk.missing_deliveries([], [], today)


def check_data_inconsistencies() -> None:
    lbs = {1: Decimal("56.00")}

    # 60000 - 30000 = 30000 lbs / 56 = 535.71 bu. Correct, so silent.
    good = Deliv("T-OK", Decimal("60000"), Decimal("30000"), Decimal("535.71"))
    assert not risk.data_inconsistencies([good], lbs)

    # Recorded bushels far off the weights.
    wrong = Deliv("T-BAD", Decimal("60000"), Decimal("30000"), Decimal("999.00"))
    hits = risk.data_inconsistencies([wrong], lbs)
    assert len(hits) == 1 and hits[0]["kind"] == "data_inconsistency"
    assert hits[0]["severity"] == "high"
    assert hits[0]["evidence"]["expected_net_bu"] == "535.71"

    # Physically impossible weights are caught even before the bushel maths.
    swapped = Deliv("T-SWAP", Decimal("30000"), Decimal("60000"), Decimal("535.71"))
    swap_hits = risk.data_inconsistencies([swapped], lbs)
    assert len(swap_hits) == 1
    assert swap_hits[0]["evidence"]["problem"] == "tare >= gross"
    # Equal weights mean zero net, which is also wrong.
    equal = Deliv("T-EQ", Decimal("30000"), Decimal("30000"), Decimal("0"))
    assert risk.data_inconsistencies([equal], lbs)[0]["severity"] == "high"

    # Small rounding drift is tolerated rather than alerting on every ticket.
    near = Deliv("T-NEAR", Decimal("60000"), Decimal("30000"), Decimal("535.9"))
    assert not risk.data_inconsistencies([near], lbs)
    # Just outside tolerance is a medium, not a high.
    drift = Deliv("T-DRIFT", Decimal("60000"), Decimal("30000"), Decimal("537.0"))
    assert risk.data_inconsistencies([drift], lbs)[0]["severity"] == "medium"

    # An unknown or nonsensical conversion factor is skipped, not divided by.
    assert not risk.data_inconsistencies([wrong], {})
    assert not risk.data_inconsistencies([wrong], {1: Decimal("0")})
    assert not risk.data_inconsistencies([], lbs)


def check_expiring_contracts() -> None:
    today = date(2026, 7, 26)
    soon = Con("C-1", "open", today + timedelta(days=12))
    urgent = Con("C-2", "open", today + timedelta(days=3))
    far = Con("C-3", "open", today + timedelta(days=90))
    closed = Con("C-4", "closed", today + timedelta(days=5))
    past = Con("C-5", "open", today - timedelta(days=1))

    hits = risk.expiring_contracts([soon, urgent, far, closed, past], today)
    assert [h["evidence"]["contract"] for h in hits] == ["C-1", "C-2"], hits
    assert hits[0]["severity"] == "medium" and hits[1]["severity"] == "high"
    # Ending today is still inside the window.
    assert len(risk.expiring_contracts([Con("C-6", "open", today)], today)) == 1


class FakeClock:
    """Controllable monotonic clock, so window expiry is tested without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def check_rate_limiter() -> None:
    clock = FakeClock()
    rl = SlidingWindow(limit=3, window_seconds=60, clock=clock)

    assert all(rl.allow("1.2.3.4") for _ in range(3))
    assert not rl.allow("1.2.3.4"), "4th attempt inside the window must be blocked"
    # A different caller is unaffected — the limit is per key, not global.
    assert rl.allow("5.6.7.8")

    # Blocked callers get a usable Retry-After.
    assert 0 < rl.retry_after("1.2.3.4") <= 61
    assert rl.retry_after("never-seen") == 0

    # Partial expiry frees exactly one slot, not the whole window.
    clock.now += 61
    assert rl.allow("1.2.3.4")
    assert rl.allow("1.2.3.4")
    assert rl.allow("1.2.3.4")
    assert not rl.allow("1.2.3.4")

    for bad in ((0, 60), (3, 0), (-1, 60), (3, -5)):
        try:
            SlidingWindow(limit=bad[0], window_seconds=bad[1])
            raise AssertionError(f"accepted invalid config {bad}")
        except ValueError:
            pass


@dataclass
class Row:
    """Stands in for any ORM row the graph builder reads."""

    id: int
    name: str = "n"
    kind: str = "farmer"
    location: str = "loc"
    number: str = "num"
    status: str = "open"
    ticket_number: str = "tkt"
    truck_id: str = "IL-1"
    facility_id: int | None = None
    commodity_id: int | None = None
    customer_id: int | None = None
    contract_id: int | None = None


def _sample_graph() -> kg.Graph:
    # customer:1 -SIGNED-> contract:1 -FOR_COMMODITY-> commodity:1
    # delivery:1 -FULFILLS-> contract:1, -RECEIVED_AT-> facility:1
    # facility:1 -HAS_BIN-> bin:1 -STORES-> commodity:1
    # invoice:1 -BILLS-> contract:1, -BILLED_TO-> customer:1
    return kg.build(
        customers=[Row(id=1, name="Halvorsen")],
        commodities=[Row(id=1, name="Corn")],
        facilities=[Row(id=1, name="Elkhart")],
        bins=[Row(id=1, name="ELK-01", facility_id=1, commodity_id=1)],
        contracts=[Row(id=1, number="C-1", commodity_id=1, customer_id=1)],
        deliveries=[Row(id=1, facility_id=1, customer_id=1, contract_id=1)],
        invoices=[Row(id=1, number="INV-1", customer_id=1, contract_id=1)],
    )


def check_graph_ids() -> None:
    assert kg.node_id("customer", 3) == "customer:3"
    assert kg.parse_node_id("customer:3") == ("customer", 3)
    for bad in (
        "",
        "customer",
        "customer:",
        ":3",
        "customer:abc",
        "bogus:3",
        "customer:-1",
        "customer:3:4",
    ):
        try:
            kg.parse_node_id(bad)
            raise AssertionError(f"accepted bad node id {bad!r}")
        except ValueError:
            pass


def check_graph_build() -> None:
    g = _sample_graph()
    assert len(g.nodes) == 7, g.nodes.keys()
    labels = sorted(e.label for e in g.edges)
    assert labels == [
        "BILLED_TO",
        "BILLS",
        "DELIVERED_BY",
        "FOR_COMMODITY",
        "FULFILLS",
        "HAS_BIN",
        "RECEIVED_AT",
        "SIGNED",
        "STORES",
    ], labels

    s = kg.summary(g)
    assert s["node_count"] == 7 and s["edge_count"] == 9
    assert s["nodes_by_kind"]["customer"] == 1

    # A null FK must not produce an edge to nowhere.
    loose = kg.build(
        [],
        [],
        [],
        [],
        [],
        [Row(id=9, contract_id=None, customer_id=None, facility_id=None)],
        [],
    )
    assert loose.edges == [], loose.edges
    # Neither must an FK pointing at a row that was not loaded.
    dangling = kg.build(
        [], [], [], [], [Row(id=1, customer_id=404, commodity_id=404)], [], []
    )
    assert dangling.edges == [], dangling.edges


def check_graph_expand() -> None:
    g = _sample_graph()

    # Depth 0 is the root alone.
    only = kg.expand(g, "commodity:1", depth=0)
    assert [n["id"] for n in only["nodes"]] == ["commodity:1"]
    assert only["edges"] == []

    # Traversal is undirected: commodity:1 is reached from customer:1 via
    # contract:1 even though no FK points that way.
    one = kg.expand(g, "customer:1", depth=1)
    assert {n["id"] for n in one["nodes"]} == {
        "customer:1",
        "contract:1",
        "delivery:1",
        "invoice:1",
    }
    two = kg.expand(g, "customer:1", depth=2)
    assert "commodity:1" in {n["id"] for n in two["nodes"]}

    # hops is the real distance, and nodes come back nearest-first.
    hops = {n["id"]: n["hops"] for n in two["nodes"]}
    assert hops["customer:1"] == 0 and hops["contract:1"] == 1
    assert hops["commodity:1"] == 2
    assert [n["hops"] for n in two["nodes"]] == sorted(n["hops"] for n in two["nodes"])

    # Depth is clamped, so a huge value cannot be used to force a full scan.
    assert kg.expand(g, "customer:1", depth=999)["depth"] == kg.MAX_DEPTH
    assert kg.expand(g, "customer:1", depth=-5)["depth"] == 0

    # Every returned edge must have both endpoints in the returned node set.
    ids = {n["id"] for n in two["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in two["edges"])

    try:
        kg.expand(g, "customer:404")
        raise AssertionError("expanded a node that does not exist")
    except KeyError:
        pass


def check_chunking() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []
    assert chunk_text("short") == ["short"]

    # Paragraphs that fit are packed, not split.
    packed = chunk_text("a" * 100 + "\n\n" + "b" * 100, size=1000, overlap=100)
    assert len(packed) == 1 and "\n\n" in packed[0]

    # Paragraphs that do not fit together become separate chunks.
    split = chunk_text("a" * 600 + "\n\n" + "b" * 600, size=1000, overlap=100)
    assert len(split) == 2, [len(c) for c in split]

    # One oversized paragraph is windowed, and every chunk respects the ceiling.
    long = chunk_text("x" * 5000, size=1000, overlap=200)
    assert len(long) > 1
    assert all(len(c) <= 1000 for c in long), [len(c) for c in long]

    # Nothing is lost: every chunk is non-empty and the first content survives.
    assert all(c.strip() for c in long)
    assert long[0].startswith("x")

    # Overlap actually overlaps — consecutive windows share their boundary text.
    windows = chunk_text("abcdefghij" * 30, size=100, overlap=50)
    assert windows[0][50:] == windows[1][:50], "windows should share 50 chars"

    # A tiny size still terminates rather than looping.
    assert len(chunk_text("y" * 50, size=10, overlap=9)) > 1

    for bad in ((0, 0), (-1, 0), (100, 100), (100, 101), (100, -1)):
        try:
            chunk_text("text", size=bad[0], overlap=bad[1])
            raise AssertionError(f"accepted invalid chunk config {bad}")
        except ValueError:
            pass


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def check_fake_embedder() -> None:
    e = FakeEmbedder(dimensions=256)
    assert e.dimensions == 256 and e.name == "fake"

    # Deterministic: identical text must give an identical vector, including
    # across processes (blake2b, not the salted built-in hash).
    v1, v2 = e.embed(["corn moisture 14.2 percent"] * 2)
    assert v1 == v2
    assert FakeEmbedder(256).embed(["corn"])[0] == e.embed(["corn"])[0]

    # Unit length, so cosine is a plain dot product.
    for vector in e.embed(["corn", "a much longer sentence about grain storage"]):
        assert len(vector) == 256
        assert abs(_cosine(vector, vector) - 1.0) < 1e-9

    # Degenerate inputs must not produce NaN and must stay unit length.
    for empty in e.embed(["", "   ", "!!! ??? ---"]):
        assert all(v == v for v in empty), "NaN in embedding"
        assert abs(_cosine(empty, empty) - 1.0) < 1e-9

    # The point of the hashing trick: shared vocabulary ranks higher. Without
    # this the search smoke test would prove nothing.
    query = e.embed(["corn moisture reading"])[0]
    related = e.embed(["the corn moisture reading was high"])[0]
    unrelated = e.embed(["invoice payment terms net thirty days"])[0]
    assert _cosine(query, related) > _cosine(query, unrelated), "no lexical signal"

    # Case and punctuation are normalised away.
    assert e.embed(["Corn, Moisture!"])[0] == e.embed(["corn moisture"])[0]

    # Batching preserves order.
    batch = e.embed(["alpha", "beta", "gamma"])
    assert batch[0] == e.embed(["alpha"])[0]
    assert batch[2] == e.embed(["gamma"])[0]
    assert e.embed([]) == []


def check_identifier_resolution() -> None:
    f = resolve.find_identifiers

    assert f("status of contract C-2026-1000?") == {"contract": ["C-2026-1000"]}
    assert f("ticket T-80011") == {"delivery": ["T-80011"]}
    assert f("bin ELK-04 moisture") == {"bin": ["ELK-04"]}

    # The critical overlap: INV-5099 must resolve as an invoice, and must NOT
    # also be picked up as the bin code "INV-50".
    assert f("invoice INV-5099") == {"invoice": ["INV-5099"]}
    assert "bin" not in f("invoice INV-5099")

    # Several kinds in one question, each to the right bucket.
    both = f("did delivery T-80011 fulfil contract C-2026-1000 and bill INV-5000?")
    assert both == {
        "contract": ["C-2026-1000"],
        "invoice": ["INV-5000"],
        "delivery": ["T-80011"],
    }, both

    # Case-insensitive, normalised upward, de-duplicated.
    assert f("c-2026-1000 and C-2026-1000") == {"contract": ["C-2026-1000"]}
    assert f("elk-04") == {"bin": ["ELK-04"]}

    # Nothing to find is an empty dict, not a crash.
    assert f("") == {}
    assert f("how much grain do we have?") == {}
    # A bare word that looks structural but is not an identifier.
    assert f("contract") == {} and f("T-") == {}


def check_customer_matching() -> None:
    customers = [
        (1, "Halvorsen Family Farms"),
        (2, "Prairie Ridge Ag"),
        (3, "Two Rivers Grain Co"),
        (8, "Gulf Export Partners"),
    ]
    m = resolve.match_customers

    assert m("what does Halvorsen owe us?", customers) == [(1, "Halvorsen Family Farms")]
    assert m("prairie ridge contracts", customers) == [(2, "Prairie Ridge Ag")]

    # Stopwords must not match everyone. "Farms", "Family" and "Grain" appear in
    # several names and carry no signal.
    assert m("show me the family farms", customers) == []
    assert m("grain contracts", customers) == []

    # Short tokens are ignored, so "Ag" and "Co" cannot match.
    assert m("ag co", customers) == []

    # No signal at all.
    assert m("", customers) == []
    assert m("what is our total capacity?", customers) == []
    assert m("Halvorsen", []) == []

    # Ranked by overlap, and capped.
    ranked = m("gulf export partners rivers", customers, limit=2)
    assert ranked[0] == (8, "Gulf Export Partners"), ranked
    assert len(ranked) <= 2


def check_fake_chat() -> None:
    chat = llm.FakeChat()
    assert chat.name == "fake"

    context = (
        "[DB1] contract: number=C-2026-1000, status=open, quantity_bu=45000.0\n"
        "[S1] from moisture-note.txt:\n"
        "Bin ELK-04 corn moisture reading was 16.4 percent on arrival.\n"
        "[S2] from payment-terms.txt:\n"
        "Invoice payment terms are net thirty days from issue.\n"
    )

    # Grounding: the answer must quote the line matching the question, proving
    # retrieval actually fed generation.
    moisture = chat.answer("what was the corn moisture reading?", context)
    assert "16.4" in moisture.text, moisture.text
    assert "thirty days" not in moisture.text

    payment = chat.answer("what are the payment terms?", context)
    assert "thirty days" in payment.text, payment.text

    # A database record outranks graph triples and excerpts at equal relevance,
    # so a question about a contract leads with the row, not a neighbour.
    contract = chat.answer("what is the status of contract C-2026-1000?", context)
    assert contract.text.splitlines()[1].startswith("- [DB1]"), contract.text

    # Deterministic across calls and instances.
    assert (
        chat.answer("moisture", context).text
        == llm.FakeChat().answer("moisture", context).text
    )

    # No overlap must refuse rather than invent.
    empty = chat.answer("what is the capital of France?", "(no matching records)")
    assert "does not contain" in empty.text

    # Usage is reported...
    assert moisture.input_tokens > 0 and moisture.output_tokens > 0
    # ...but the fake bills nothing, however many tokens it estimated. Pricing a
    # free call at OpenAI rates would put fictional spend in the audit trail.
    assert chat.cost_usd(moisture.input_tokens, moisture.output_tokens) == 0.0
    assert chat.cost_usd(10_000_000, 10_000_000) == 0.0

    # The OpenAI rate card itself is still sane: positive, and output dearer
    # than input per token.
    assert llm.openai_cost_usd(0, 0) == 0.0
    assert llm.openai_cost_usd(1_000_000, 0) > 0
    assert llm.openai_cost_usd(0, 1_000_000) > llm.openai_cost_usd(1_000_000, 0)


def _rejects(sql: str) -> str:
    """Assert the gate refuses `sql`, and return the reason it gave."""
    try:
        allowed = sqlgen.validate(sql)
    except sqlgen.UnsafeSQL as exc:
        return str(exc)
    raise AssertionError(f"gate ALLOWED unsafe SQL: {sql!r} -> {allowed!r}")


def check_sql_gate_accepts_valid() -> None:
    v = sqlgen.validate

    # A plain select gets a LIMIT imposed.
    out = v("SELECT name FROM customers")
    assert out == "SELECT name FROM customers LIMIT 200", out

    # An existing LIMIT is respected, not doubled.
    assert v("SELECT name FROM customers LIMIT 5") == "SELECT name FROM customers LIMIT 5"

    # A single-row aggregate needs no LIMIT.
    assert v("SELECT COUNT(*) FROM deliveries") == "SELECT COUNT(*) FROM deliveries"
    assert v("SELECT SUM(amount) FROM invoices") == "SELECT SUM(amount) FROM invoices"

    # Joins across allowed tables, and a CTE whose name is not a real table.
    joined = v(
        "SELECT c.name, SUM(d.net_bu) FROM deliveries d "
        "JOIN customers c ON c.id = d.customer_id GROUP BY c.name"
    )
    assert joined.endswith("LIMIT 200")
    cte = v(
        "WITH totals AS (SELECT customer_id, SUM(net_bu) s FROM deliveries "
        "GROUP BY customer_id) SELECT * FROM totals ORDER BY s DESC LIMIT 10"
    )
    assert "totals" in cte

    # Markdown fences and trailing semicolons are cleaned, not rejected.
    assert v("```sql\nSELECT id FROM invoices LIMIT 1\n```") == (
        "SELECT id FROM invoices LIMIT 1"
    )
    assert v("SELECT id FROM invoices LIMIT 1;") == "SELECT id FROM invoices LIMIT 1"
    assert v("  SELECT id FROM invoices LIMIT 1 ;; ") == "SELECT id FROM invoices LIMIT 1"


def check_sql_gate_blocks_writes() -> None:
    # Every DML/DDL verb, both as a bare statement and smuggled after a select.
    for sql in (
        "DELETE FROM invoices",
        "DROP TABLE contracts",
        "UPDATE invoices SET amount = 0",
        "INSERT INTO customers (name) VALUES ('x')",
        "TRUNCATE deliveries",
        "ALTER TABLE invoices ADD COLUMN x int",
        "CREATE TABLE evil (id int)",
        "GRANT ALL ON invoices TO public",
        "SELECT 1 FROM invoices; DROP TABLE contracts",
        "SELECT 1 FROM invoices LIMIT 1; DELETE FROM invoices",
    ):
        _rejects(sql)

    # Session-state and side-effect functions.
    for sql in (
        "SELECT pg_sleep(30) FROM invoices",
        "SELECT pg_read_file('/etc/passwd') FROM invoices",
        "SET statement_timeout = 0",
        "SELECT * FROM invoices FOR UPDATE",
        "COPY invoices TO '/tmp/out.csv'",
        "SELECT lo_import('/etc/passwd') FROM invoices",
        "SELECT dblink('host=evil', 'select 1') FROM invoices",
        "SELECT current_setting('is_superuser') FROM invoices",
    ):
        _rejects(sql)


def check_sql_gate_blocks_users_table() -> None:
    # The users table holds password hashes. It must be unreachable by any route.
    for sql in (
        "SELECT * FROM users",
        "SELECT email, password_hash FROM users LIMIT 10",
        "SELECT name FROM customers UNION SELECT email FROM users",
        "SELECT (SELECT password_hash FROM users LIMIT 1) FROM invoices",
        "SELECT u.email FROM invoices JOIN users u ON u.id = 1",
        "WITH x AS (SELECT * FROM users) SELECT * FROM x",
        "select * from USERS",
    ):
        reason = _rejects(sql)
        assert "users" in reason.lower(), (sql, reason)

    # Catalogue tables are another way to reach credentials or schema.
    for sql in (
        "SELECT rolname FROM pg_authid",
        "SELECT table_name FROM information_schema.tables",
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT usename FROM pg_user",
    ):
        _rejects(sql)


def check_sql_gate_blocks_evasion() -> None:
    # Comments are the standard way to smuggle text past keyword checks.
    for sql in (
        "SELECT 1 FROM invoices -- harmless",
        "SELECT /* DELETE */ 1 FROM invoices",
        "SELECT 1 FROM invoices /* trailing",
        "SELECT 1 FROM inv/**/oices",
    ):
        _rejects(sql)

    # Unknown or non-allowlisted tables.
    reason = _rejects("SELECT * FROM secrets")
    assert "not allowed" in reason, reason
    _rejects("SELECT * FROM public.pg_shadow")
    _rejects("SELECT 1")  # references no table at all

    # Not a read query, or not a query.
    _rejects("")
    _rejects("   ")
    _rejects("NO_QUERY")
    _rejects("EXPLAIN ANALYZE SELECT 1 FROM invoices")
    _rejects("this is not sql at all")

    # The gate never returns something it just called unsafe.
    for sql in ("DROP TABLE contracts", "SELECT * FROM users", "SELECT 1; SELECT 2"):
        try:
            sqlgen.validate(sql)
            raise AssertionError(f"gate allowed {sql!r}")
        except sqlgen.UnsafeSQL:
            pass


def check_fake_sql_generation() -> None:
    chat = llm.FakeChat()
    prompt = sqlgen.SQL_SYSTEM_PROMPT

    # An aggregate question — exactly what the entity lookups could not do.
    total = chat.generate_sql("what is the total invoice amount by status?", prompt)
    assert "SUM(amount)" in total.text and "invoices" in total.text
    # And it survives the gate, which is the point.
    assert sqlgen.validate(total.text)

    ranked = chat.generate_sql("which customer delivered the most bushels?", prompt)
    assert "JOIN customers" in ranked.text
    assert sqlgen.validate(ranked.text)

    # Deterministic.
    assert (
        chat.generate_sql("total invoice amount", prompt).text
        == llm.FakeChat().generate_sql("total invoice amount", prompt).text
    )

    # No template match must say so rather than invent a query.
    assert chat.generate_sql("what is the capital of France?", prompt).text == "NO_QUERY"
    _rejects("NO_QUERY")

    # Usage is reported; the fake still bills nothing.
    assert total.input_tokens > 0
    assert chat.cost_usd(total.input_tokens, total.output_tokens) == 0.0

    # Every template the fake can emit must pass the gate — a template that
    # cannot execute is a silently dead code path.
    for keywords, sql in llm.FakeChat.SQL_TEMPLATES:
        assert sqlgen.validate(sql), keywords


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"


def check_image_sniffing() -> None:
    """Content-type and extension are attacker-controlled; the bytes are not."""
    from app import ocr

    assert ocr.sniff_image(PNG_1PX, ".png") == "image/png"
    assert ocr.sniff_image(JPEG_HEAD, ".jpg") == "image/jpeg"
    assert ocr.sniff_image(JPEG_HEAD, ".jpeg") == "image/jpeg"
    assert ocr.sniff_image(b"GIF89a" + b"\x00" * 10, ".gif") == "image/gif"
    assert ocr.sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp") == "image/webp"

    def rejects(data: bytes, suffix: str, why: str) -> None:
        try:
            ocr.sniff_image(data, suffix)
        except ocr.OCRError:
            return
        raise AssertionError(f"accepted {why}")

    # A Windows executable renamed to .png — the case that matters most, since
    # these bytes would otherwise be billed to a vision API and stored.
    rejects(b"MZ\x90\x00" + b"\x00" * 60, ".png", "PE executable as .png")
    rejects(b"#!/bin/sh\nrm -rf /", ".jpg", "shell script as .jpg")
    rejects(b"%PDF-1.7", ".png", "PDF as .png")
    rejects(PNG_1PX, ".jpg", "PNG bytes claiming to be JPEG")
    rejects(JPEG_HEAD, ".png", "JPEG bytes claiming to be PNG")
    # RIFF also fronts WAV and AVI, so the container form must be checked.
    rejects(b"RIFF\x00\x00\x00\x00WAVEfmt ", ".webp", "WAV as .webp")
    rejects(b"", ".png", "empty file")
    rejects(b"\x89PN", ".png", "truncated magic")
    rejects(PNG_1PX, ".bmp", "unsupported image suffix")


def check_image_extraction() -> None:
    """The upload path must route images to OCR and reject impostors."""
    from app import extract, ocr

    # Images are on the allowlist alongside documents.
    assert ocr.IMAGE_SUFFIXES <= extract.ALLOWED_SUFFIXES
    assert ".png" in extract.ALLOWED_SUFFIXES
    assert ".pdf" in extract.ALLOWED_SUFFIXES
    # And nothing dangerous crept in with them.
    assert ".exe" not in extract.ALLOWED_SUFFIXES
    assert ".svg" not in extract.ALLOWED_SUFFIXES  # SVG is script-bearing XML

    text, note = extract.extract(PNG_1PX, "scale-ticket.png")
    assert "fake-ocr" in text, text
    assert "image/png" in text
    assert note and "OCR" in note

    # Path traversal in an image filename is still just a name.
    again, _ = extract.extract(PNG_1PX, "../../etc/ticket.png")
    assert "fake-ocr" in again

    # A disguised executable is refused before any OCR call is made.
    try:
        extract.extract(b"MZ\x90\x00" + b"\x00" * 60, "photo.png")
        raise AssertionError("extracted a disguised executable")
    except extract.ExtractionError as exc:
        assert "not image/png" in str(exc), str(exc)

    # Oversized images are refused rather than billed for.
    from app.config import settings as cfg

    try:
        extract.extract(PNG_1PX + b"\x00" * (cfg.max_image_bytes + 1), "big.png")
        raise AssertionError("accepted an oversized image")
    except extract.ExtractionError as exc:
        assert "limit" in str(exc), str(exc)


def check_market_units() -> None:
    """CBOT quotes grains in cents. A 100x unit slip here would silently wreck
    every valuation downstream, so it gets its own checks."""
    from app.market import MarketError, to_usd_per_bu

    # USX is US cents — the CBOT grain convention.
    assert to_usd_per_bu(475.0, "USX") == 4.75
    assert to_usd_per_bu(1213.25, "USX") == 12.1325
    assert to_usd_per_bu(673.5, "usx") == 6.735  # case-insensitive
    # Already dollars, or unspecified, passes through.
    assert to_usd_per_bu(4.75, "USD") == 4.75
    assert to_usd_per_bu(4.75, "") == 4.75
    # An unknown currency must raise, never quietly be off by 100x.
    for bad in ("EUR", "GBP", "cad"):
        try:
            to_usd_per_bu(475.0, bad)
            raise AssertionError(f"accepted unknown currency {bad}")
        except MarketError:
            pass


def check_market_chart_parsing() -> None:
    """The Yahoo endpoint is unofficial, so a shape change must give one clear
    error rather than a KeyError deep in the transform."""
    from app.market import MarketError, parse_chart

    good = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "USX"},
                    "timestamp": [1785000000, 1785086400, 1785172800],
                    "indicators": {"quote": [{"close": [470.0, None, 475.0]}]},
                }
            ]
        }
    }
    quotes = parse_chart(good, days=10)
    # The null (non-trading day) is skipped, not interpolated.
    assert len(quotes) == 2, quotes
    assert quotes[-1].close_usd_per_bu == 4.75
    assert quotes[0].close_usd_per_bu == 4.70
    # Ascending by date, and `days` trims from the newest end.
    assert quotes[0].quoted_on < quotes[1].quoted_on
    assert len(parse_chart(good, days=1)) == 1

    for bad in (
        {},
        {"chart": {}},
        {"chart": {"result": []}},
        {"chart": {"result": [{"meta": {}}]}},
        # All closes null → nothing usable.
        {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USX"},
                        "timestamp": [1785000000],
                        "indicators": {"quote": [{"close": [None]}]},
                    }
                ]
            }
        },
    ):
        try:
            parse_chart(bad, days=5)
            raise AssertionError(f"accepted malformed chart {bad}")
        except MarketError:
            pass


def check_mark_to_market() -> None:
    """Position sign is the thing to get right: an elevator BUYS from farmers and
    SELLS to buyers, so the same price move helps one and hurts the other."""
    from app.market import mark_to_market, position_summary

    prices = {1: 5.00}  # corn at $5.00
    base = {
        "commodity_id": 1,
        "commodity": "Corn",
        "customer": "X",
        "quantity_bu": 10000,
        "delivered_bu": 0,
        "price_per_bu": 4.00,
    }

    # Purchase at $4 with the board at $5 → favourable by $1 x 10,000.
    buy = mark_to_market([{**base, "number": "C-1", "side": "farmer"}], prices)[0]
    assert buy["unrealised_usd"] == 10000.0, buy
    assert buy["basis_usd_per_bu"] == 1.0

    # Sale at $4 with the board at $5 → unfavourable by the same amount.
    sell = mark_to_market([{**base, "number": "C-2", "side": "buyer"}], prices)[0]
    assert sell["unrealised_usd"] == -10000.0, sell

    # Only the undelivered balance is exposed; delivered bushels are settled.
    partial = mark_to_market(
        [{**base, "number": "C-3", "side": "farmer", "delivered_bu": 7500}], prices
    )[0]
    assert partial["remaining_bu"] == 2500
    assert partial["unrealised_usd"] == 2500.0

    # Fully delivered contracts drop out entirely, as does an over-delivery.
    assert (
        mark_to_market(
            [{**base, "number": "C-4", "side": "farmer", "delivered_bu": 10000}], prices
        )
        == []
    )
    assert (
        mark_to_market(
            [{**base, "number": "C-5", "side": "farmer", "delivered_bu": 99999}], prices
        )
        == []
    )
    # A commodity with no price is skipped rather than valued at zero.
    assert mark_to_market([{**base, "number": "C-6", "side": "farmer"}], {}) == []

    # Net position: 10,000 bought against 4,000 sold = 6,000 net long.
    valued = mark_to_market(
        [
            {**base, "number": "C-7", "side": "farmer"},
            {**base, "number": "C-8", "side": "buyer", "quantity_bu": 4000},
        ],
        prices,
    )
    [pos] = position_summary(valued, prices)
    assert pos["long_bu"] == 10000 and pos["short_bu"] == 4000
    assert pos["net_bu"] == 6000 and pos["direction"] == "long"
    assert pos["open_contracts"] == 2
    # +10,000 on the purchase, -4,000 on the sale.
    assert pos["unrealised_usd"] == 6000.0

    # Balanced book reads flat, not long.
    flat = position_summary(
        mark_to_market(
            [
                {**base, "number": "C-9", "side": "farmer"},
                {**base, "number": "C-10", "side": "buyer"},
            ],
            prices,
        ),
        prices,
    )
    assert flat[0]["direction"] == "flat" and flat[0]["net_bu"] == 0
    assert position_summary([], prices) == []


def check_position_alerts() -> None:
    from decimal import Decimal as D

    positions = [
        {
            "commodity": "Corn",
            "net_bu": 45000,
            "long_bu": 45000,
            "short_bu": 0,
            "direction": "long",
            "market_usd_per_bu": 5.0,
            "unrealised_usd": -12000.0,
            "open_contracts": 3,
        },
        # Under the threshold — quiet.
        {
            "commodity": "Wheat",
            "net_bu": 1000,
            "long_bu": 1000,
            "short_bu": 0,
            "direction": "long",
            "market_usd_per_bu": 6.7,
            "unrealised_usd": 500.0,
            "open_contracts": 1,
        },
    ]
    hits = risk.unhedged_position(positions)
    assert len(hits) == 1, hits
    # A losing position of that size is high, not medium.
    assert hits[0]["severity"] == "high"
    assert hits[0]["evidence"]["commodity"] == "Corn"
    assert hits[0]["fingerprint"]
    # A winning position of the same size is still worth flagging, but medium.
    winning = risk.unhedged_position([{**positions[0], "unrealised_usd": 9000.0}])
    assert winning[0]["severity"] == "medium"
    # Short exposure counts too — the rule is about magnitude, not direction.
    short = risk.unhedged_position(
        [{**positions[0], "net_bu": -45000, "direction": "short"}]
    )
    assert len(short) == 1 and "short" in short[0]["title"]
    assert risk.unhedged_position([]) == []

    valued = [
        {
            "number": "C-1",
            "commodity": "Corn",
            "side": "farmer",
            "price_per_bu": 4.00,
            "market_usd_per_bu": 5.00,  # 25% above → flag
            "remaining_bu": 10000,
            "unrealised_usd": 10000.0,
        },
        {
            "number": "C-2",
            "commodity": "Corn",
            "side": "farmer",
            "price_per_bu": 4.90,
            "market_usd_per_bu": 5.00,  # ~2% → quiet
            "remaining_bu": 5000,
            "unrealised_usd": 500.0,
        },
    ]
    off = risk.contract_offmarket(valued)
    assert len(off) == 1 and off[0]["evidence"]["contract"] == "C-1"
    # 25% clears the 15% tolerance but not 2x it, so medium.
    assert off[0]["severity"] == "medium", off[0]["severity"]
    # Past 30% it escalates.
    wide = risk.contract_offmarket([{**valued[0], "price_per_bu": 3.00}])
    assert wide[0]["severity"] == "high", wide[0]
    # A zero or negative price is skipped rather than dividing by zero.
    assert risk.contract_offmarket([{**valued[0], "price_per_bu": 0}]) == []
    assert risk.contract_offmarket([{**valued[0], "market_usd_per_bu": 0}]) == []
    # Tolerance is configurable.
    assert len(risk.contract_offmarket(valued, tolerance_pct=D("1"))) == 2


def check_fx_parsing() -> None:
    """A bad rate must be skipped, never coerced to zero — a zero rate would read
    as 'the dollar buys nothing' on the dashboard."""
    from app.feeds import FeedError, parse_fx

    good = {
        "result": "success",
        "time_last_update_unix": 1785196951,
        "rates": {"BRL": 5.086098, "CNY": 6.777732, "EUR": 0.877986, "ZZZ": 1.0},
    }
    rates = parse_fx(good)
    by = {r.currency: r.rate for r in rates}
    assert by["BRL"] == 5.086098 and by["EUR"] == 0.877986
    # Only tracked currencies are kept; ZZZ is not one.
    assert "ZZZ" not in by
    # The quote date comes from the feed's timestamp, not from "now". Derived
    # rather than hardcoded, so the check states the property instead of a date.
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    expected = _dt.fromtimestamp(1785196951, _UTC).date()
    assert rates[0].quoted_on == expected, (rates[0].quoted_on, expected)
    assert rates[0].quoted_on != _dt.now(_UTC).date() or expected == _dt.now(_UTC).date()

    # Unusable values are dropped, and the good ones still come through.
    partial = parse_fx({**good, "rates": {"BRL": 5.0, "CNY": None, "EUR": 0, "MXN": "x"}})
    assert {r.currency for r in partial} == {"BRL"}, partial

    for bad in (
        {"result": "error"},
        {"result": "success"},
        {"result": "success", "rates": []},
        # None of the tracked currencies present.
        {"result": "success", "rates": {"ZZZ": 1.0}},
        # All tracked values unusable.
        {"result": "success", "rates": {"BRL": 0, "CNY": -1}},
    ):
        try:
            parse_fx(bad)
            raise AssertionError(f"accepted bad fx payload {bad}")
        except FeedError:
            pass


def check_rss_parsing() -> None:
    from app.feeds import FeedError, parse_rss

    feed = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item>
        <title>Soybeans make contract highs</title>
        <link>https://example.test/a</link>
        <guid>guid-a</guid>
        <pubDate>Thu, 23 Jul 2026 22:25:00 GMT</pubDate>
        <source url="https://agweb.com">AgWeb</source>
      </item>
      <item>
        <title>Wheat settles higher</title>
        <link>https://example.test/b</link>
        <pubDate>not a real date</pubDate>
      </item>
      <item><title>No link, dropped</title></item>
      <item><link>https://example.test/d</link></item>
    </channel></rss>"""

    items = parse_rss(feed)
    # Two usable items; the ones missing a title or a link are dropped.
    assert len(items) == 2, [i.title for i in items]
    assert items[0].publisher == "AgWeb"
    assert items[0].guid == "guid-a"
    assert items[0].published_at is not None
    assert items[0].published_at.tzinfo is not None, "must be timezone-aware"
    # A bad date does not discard an otherwise good headline.
    assert items[1].published_at is None and items[1].title == "Wheat settles higher"
    # No guid falls back to the link, so dedup still works.
    assert items[1].guid == "https://example.test/b"
    # The limit is respected.
    assert len(parse_rss(feed, limit=1)) == 1

    for bad in (
        "not xml at all",
        "<rss><channel></channel></rss>",  # no items
        "<rss><channel><item><description>x</description></item></channel></rss>",
    ):
        try:
            parse_rss(bad)
            raise AssertionError(f"accepted bad feed {bad[:30]}")
        except FeedError:
            pass


def check_weather_parsing() -> None:
    """Open-Meteo returns parallel arrays with nulls; the reader must not crash."""
    from app.weather import _at

    series = [1.5, None, 3, "bad"]
    assert _at(series, 0) == 1.5
    assert _at(series, 1) is None  # explicit null
    assert _at(series, 2) == 3.0  # int coerced
    assert _at(series, 3) is None  # wrong type, not a crash
    assert _at(series, 99) is None  # short array
    assert _at(None, 0) is None  # field absent entirely
    assert _at("not a list", 0) is None
    assert _at([], 0) is None


def check_webhook_signature() -> None:
    import hashlib
    import hmac as _hmac

    from fastapi import HTTPException

    from app import webhooks
    from app.config import settings as cfg

    body = b'{"event":"delivery.recorded"}'
    secret = cfg.webhook_secret or "selfcheck-webhook-secret"
    original = cfg.webhook_secret
    cfg.webhook_secret = secret
    try:
        good = _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        # Correct signature, with and without the conventional prefix.
        webhooks.verify_signature(body, good)
        webhooks.verify_signature(body, f"sha256={good}")

        def rejects(sig, why: str) -> None:  # noqa: ANN001
            try:
                webhooks.verify_signature(body, sig)
            except HTTPException as exc:
                assert exc.status_code == 401, (why, exc.status_code)
                return
            raise AssertionError(f"accepted {why}")

        rejects(None, "missing signature")
        rejects("", "empty signature")
        rejects("0" * 64, "wrong signature")
        rejects(good[:-1] + ("0" if good[-1] != "0" else "1"), "one flipped char")
        rejects("sha256=nonsense", "garbage")
        # A signature for different bytes must not validate this body.
        other = _hmac.new(secret.encode(), b"different", hashlib.sha256).hexdigest()
        rejects(other, "signature over other bytes")

        # With no secret configured the endpoint refuses everything rather than
        # accepting unsigned writes.
        cfg.webhook_secret = ""
        try:
            webhooks.verify_signature(body, good)
            raise AssertionError("accepted a webhook with no secret configured")
        except HTTPException as exc:
            assert exc.status_code == 503, exc.status_code
    finally:
        cfg.webhook_secret = original


def check_filename_safety() -> None:
    # Directory components must never survive, on either separator.
    assert extract.safe_suffix("report.pdf") == ".pdf"
    assert extract.safe_suffix("REPORT.PDF") == ".pdf"
    assert extract.safe_suffix("../../etc/passwd.txt") == ".txt"
    assert extract.safe_suffix("..\\..\\windows\\system.ini") == ".ini"
    assert extract.safe_suffix("/absolute/path/contract.docx") == ".docx"
    assert extract.safe_suffix("archive.tar.gz") == ".gz"
    assert extract.safe_suffix("noextension") == ""
    assert extract.safe_suffix("") == ""

    # Anything not on the allowlist is refused, including dangerous types.
    for name in ("shell.exe", "script.sh", "payload.zip", "noextension", "a.pdf.exe"):
        try:
            extract.extract(b"data", name)
            raise AssertionError(f"accepted disallowed file {name}")
        except extract.ExtractionError:
            pass


def check_extraction() -> None:
    text, note = extract.extract(b"hello\nworld", "notes.txt")
    assert text == "hello\nworld" and note is None

    # CSV becomes readable rows, and blank lines are dropped.
    csv_text, _ = extract.extract(b"name,bushels\nCorn,500\n\nWheat,250\n", "d.csv")
    assert csv_text == "name | bushels\nCorn | 500\nWheat | 250", repr(csv_text)

    # Semicolon-delimited CSV is sniffed, not assumed.
    semi, _ = extract.extract(b"a;b;c\n1;2;3\n", "d.csv")
    assert semi == "a | b | c\n1 | 2 | 3", repr(semi)

    # Undecodable bytes are replaced, not fatal.
    dirty, _ = extract.extract(b"ok \xff\xfe bytes", "d.txt")
    assert "ok" in dirty and "bytes" in dirty

    # A corrupt file of an allowed type is a clean error, never a crash.
    for name in ("broken.pdf", "broken.docx", "broken.xlsx"):
        try:
            extract.extract(b"not really a document", name)
            raise AssertionError(f"parsed garbage as {name}")
        except extract.ExtractionError:
            pass


if __name__ == "__main__":
    for check in (
        check_passwords,
        check_tokens,
        check_rate_limiter,
        check_chunking,
        check_fake_embedder,
        check_identifier_resolution,
        check_customer_matching,
        check_fake_chat,
        check_sql_gate_accepts_valid,
        check_sql_gate_blocks_writes,
        check_sql_gate_blocks_users_table,
        check_sql_gate_blocks_evasion,
        check_fake_sql_generation,
        check_image_sniffing,
        check_image_extraction,
        check_fx_parsing,
        check_rss_parsing,
        check_market_units,
        check_market_chart_parsing,
        check_mark_to_market,
        check_position_alerts,
        check_weather_parsing,
        check_webhook_signature,
        check_filename_safety,
        check_extraction,
        check_duplicate_invoices,
        check_bin_anomalies,
        check_expiring_contracts,
        check_fingerprints,
        check_missing_deliveries,
        check_data_inconsistencies,
        check_graph_ids,
        check_graph_build,
        check_graph_expand,
    ):
        check()
        print(f"ok  {check.__name__}")
    print("all checks passed")
