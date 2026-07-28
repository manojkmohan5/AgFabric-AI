# AgFabric AI

Enterprise agricultural intelligence platform. See
[AgFabric_AI_Build_Plan.md](AgFabric_AI_Build_Plan.md) for the full design.

Runs entirely on localhost. No cloud account, no paid service.

## Status

Sprint 1 (backend foundation) is built and verified:

| Piece | State |
| --- | --- |
| Postgres schema + demo dataset | done |
| JWT auth + RBAC + login rate limit | done |
| `/health`, `/login`, `/logout` | done |
| `/dashboard`, `/alerts`, `/storage` | done |
| `/graph`, `/graph/entity/{id}` | done |
| Risk rules (6), persisted alerts, agent registry + scheduler | done |
| CI (lint, format, unit, integration, docker) | done |
| MinIO + document upload, extraction, dedup, versioning, chunking | done |
| Embeddings + Qdrant + `/search` with traceability | done |
| `/query` hybrid SQL + vector + graph, explainable envelope | done |
| Webhooks (HMAC, idempotent) + Open-Meteo ETL | done |
| Celery worker + beat (optional, Redis) | done |
| Indexes with `EXPLAIN ANALYZE` evidence | done |
| Audit log, `/metrics`, Prometheus rules, Grafana dashboard | done |
| Next.js frontend | Phase 7 |

No graph database. The knowledge graph is derived from PostgreSQL foreign keys
in [graph.py](backend/app/graph.py) — nodes are rows (`customer:3`), edges are
the FKs that already exist, traversal is breadth-first up to 3 hops. PostgreSQL
stays the only source of truth, so there is no projection job and no dual-write
consistency problem.

## Run it

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into JWT_SECRET
```

Postgres, MinIO, Qdrant, and the API are what's needed now. Redis is declared
for Phase 5 — leave it stopped to save RAM.

```bash
docker compose up -d postgres minio qdrant
docker compose run --rm api python -m app.seed
docker compose up api
```

API docs: <http://localhost:8000/docs>

### Without Docker for the API

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements-dev.txt
python -m app.seed
uvicorn app.main:app --reload
```

## Demo logins

Password is whatever `SEED_PASSWORD` is set to.

| Email | Role | Sees financials |
| --- | --- | --- |
| `ops@agfabric.test` | ops | no |
| `accounting@agfabric.test` | accountant | yes |
| `warehouse@agfabric.test` | warehouse | no |
| `exec@agfabric.test` | exec | yes |

## Checks

```bash
cd backend
ruff check . && ruff format --check .
python selfcheck.py     # auth + risk rules, no database needed
python -m app.seed      # requires Postgres
python smoketest.py     # API end to end against the seeded database
```

`selfcheck.py` covers password hashing, JWT handling, the rate limiter (with an
injected clock, so no sleeping), every risk rule, and graph traversal —
including the negative cases. `smoketest.py` covers auth failures, RBAC
boundaries, graph endpoints, the 429 path, and confirms the four seeded risk
conditions are actually detected.

The same sequence runs in [CI](.github/workflows/ci.yml) on every push and PR.

## Documents

`POST /documents/upload` accepts `.pdf .docx .xlsx .csv .txt .md` up to 25MB.
Text is extracted, chunked, and stored inline on the request — no queue yet.

Three things worth knowing:

- **Duplicates are exact, not fuzzy.** The SHA-256 of the content is the key, so
  re-uploading the same bytes under a different name returns the original.
- **Same filename + different bytes = a new version.** No overwriting.
- **The object key is the content hash, never the filename.** A filename like
  `../../etc/passwd.txt` is stored as a label only; it cannot influence where
  the bytes land.

### Images and OCR

`.png .jpg .jpeg .webp .gif` are accepted too — a photographed scale ticket or
contract becomes an ordinary searchable document.

**OCR runs through the vision API, not a local engine.** That was the whole
decision, and it was about weight:

| option | cost to install |
| --- | --- |
| easyocr / paddleocr | pulls PyTorch, ~2GB |
| rapidocr-onnxruntime | ~65MB of wheels + a model download |
| pytesseract | needs the tesseract **binary** in the image |
| `gpt-4.1-nano` vision | **zero new dependencies** |

An OpenAI key is already needed for embeddings and answers, so this added nothing
to the image. `OCR_PROVIDER=auto|openai|fake`, same pattern as everything else —
the checks run offline and free on `fake`.

**Scanned PDFs work now.** pypdf can pull the embedded images out of a page
without rasterising, so those get OCR'd directly — no poppler, no pdfium. Bounded
to the first 5 pages, since each page is a billed vision call.

**Images are validated by magic bytes, never by extension or content-type.** Both
are attacker-controlled, and these bytes get sent to a paid API and stored. A
Windows executable named `photo.png` with `Content-Type: image/png` is refused;
so is a WAV file claiming to be WEBP, since `RIFF` fronts both. Ten cases asserted
in `selfcheck.py`.

Separate 6MB ceiling for images, below the 25MB document limit: vision is billed
per image and a 20MB photo costs real money for no extra legibility.

## Search

`POST /search` with `{"query": "...", "limit": 10, "document_id": null}`. Every
hit traces back to document id, filename, content hash, and chunk ordinal, and
the response reports provider, model, dimensions, and latency.

**Embeddings work without an OpenAI key.** `EMBEDDING_PROVIDER=auto` (the
default) uses `text-embedding-3-small` when `OPENAI_API_KEY` is set and a local
deterministic fake otherwise. The fake is the hashing trick over word tokens,
not random padding, so shared vocabulary really does score higher — the search
tests assert actual ranking, and CI runs at $0 with no key.

Switching providers changes what the vectors mean. Delete the Qdrant collection
and `POST /search/reindex` (ops or exec only) after changing it. A dimension
mismatch raises a clear error rather than silently dropping a populated index.

Upload embeds inline. If embedding fails — no key, rate limit, Qdrant down — the
document is still stored and the response carries a non-null `index_error`;
`POST /search/reindex` picks it up later. An upload is never lost because the
index was unavailable.

## Hybrid query

`POST /query` with `{"question": "...", "limit": 6, "graph_depth": 1}` runs three
retrievals and answers from their union:

1. **SQL** — identifiers in the question (`C-2026-1000`, `T-80011`, `INV-5099`,
   `ELK-04`) and customer names resolve to actual rows.
2. **Vector** — semantically similar document chunks.
3. **Graph** — what each resolved entity connects to.

The response is the full explainable envelope: answer, confidence, `sql_evidence`,
`graph_relationships`, `retrieved_chunks` with source traceability, what was
resolved, plus provider, model, tokens, cost, and latency.

Plus **text-to-SQL**: the model writes a query for anything the fixed lookups
cannot express — aggregates, rankings, comparisons. `"total invoice amount by
status?"` and `"which customer delivered the most bushels?"` both work now.

Generated SQL is untrusted input that happens to be executable, so three
independent layers sit between generation and results:

1. **A static gate** ([sqlgen.py](backend/app/sqlgen.py) `validate()`) — one
   statement only, `SELECT`/`WITH` only, no comments, no DML/DDL, no dangerous
   functions, every table on an allowlist, `users` unreachable by any route
   (including `UNION` and subqueries), and a `LIMIT` imposed if absent.
2. **A Postgres `READ ONLY` transaction** — the layer that does not depend on
   those regexes being complete. A write is refused by the database itself.
3. **`statement_timeout`** — a pathological query cannot hold a connection.

Nothing is silently dropped: a rejected query reports the reason in
`explanation.generated_sql.rejected`, and the structured, graph, and document
context still answer the question. Blocked queries log at `WARNING`; a model
declining an unanswerable question logs at `INFO`, so Phase 6 can alert on the
first without drowning in the second.

The gate has four dedicated test groups in `selfcheck.py` covering writes,
`users` access, catalogue tables, comment smuggling, and multi-statement
chaining. `smoketest.py` additionally bypasses the gate entirely and hands
writes straight to the executor, asserting Postgres refuses them.

Entity resolution ([resolve.py](backend/app/resolve.py)) is kept alongside it.
It costs no LLM call and is exact for identifier questions, so it runs first and
text-to-SQL handles the rest.

RBAC applies to answers too. Contract prices and invoice amounts come back as
`"redacted"` for ops and warehouse, the same rule as `/dashboard` and `/alerts`.

`LLM_PROVIDER=auto` uses `gpt-4.1-nano` when `OPENAI_API_KEY` is set, and a local
extractive fake otherwise. The fake scores context lines by word overlap, so it
is a real (crude) QA baseline — the grounding tests prove retrieval fed the
answer. It reports token counts but **always zero cost**, because pricing a free
call at OpenAI rates would put fictional spend in the audit trail.

## Agents and alerts

Four agents in [agents.py](backend/app/agents.py), each a plain function taking a
Session: `risk`, `embedding`, `entity_resolution`, `monitoring`. Not the nine the
plan lists — the other five have no work to do yet, and registering empty shells
would report green for jobs that do nothing.

```
GET  /agents              registry + each agent's last run
POST /agents/{name}/run   trigger on demand (ops or exec)
GET  /agents/runs         run history, filterable
```

Every run records status, trigger, duration, and detail — including failures, so
a crashed agent cannot leave a stale green from its last success.

**Scheduling is an in-process asyncio task, not Celery.** What is needed is
"call two functions every few minutes"; Celery would add a broker plus worker and
beat processes for that. `ponytail:` in [scheduler.py](backend/app/scheduler.py)
names the ceiling — with N replicas the agents run N times per interval, and a
restart resets the clock. Moving to Celery means writing tasks that call
`agents.execute` and deleting that file; no agent changes.

`ENABLE_SCHEDULER=false` and `RUN_AGENTS_ON_STARTUP=false` turn it off; CI sets
both so a timer cannot race assertions.

### Alerts are persisted now

`/alerts` reads stored rows rather than recomputing per request, so an alert has
an id, a first-seen time, and a status a human can move:

```
GET  /alerts?status=open&severity=high&kind=moisture_anomaly
POST /alerts/{id}/acknowledge
POST /alerts/{id}/resolve
```

Each finding carries a `fingerprint` — a stable hash of the condition's identity,
never its wording or confidence. Re-running a scan therefore **updates** the
existing alert instead of inserting a near-duplicate every few minutes. A
condition that disappears is auto-resolved; a condition a human acknowledged
stays acknowledged, because a scan must not silently undo a human decision.

Six rules now: duplicate invoices, inventory mismatch, moisture anomaly, contract
expiration, **missing deliveries**, and **data inconsistency** (net bushels that
contradict the scale weights, or tare ≥ gross). The seed plants one of each.

## Webhooks (event-driven ingestion)

`POST /webhooks/deliveries` is how an ERP or scale system pushes a delivery as it
happens, instead of someone uploading a file later.

```bash
BODY='{"event":"delivery.recorded","source":"scale-house-1","data":{
  "ticket_number":"WH-90001","customer":"Halvorsen Family Farms",
  "commodity":"Corn","facility":"Chicago Terminal","truck_id":"IL-4242",
  "gross_lbs":"62000","tare_lbs":"31000","moisture_pct":"13.9"}}'

curl -X POST localhost:8000/webhooks/deliveries \
  -H "X-AgFabric-Event-Id: evt-1" \
  -H "X-AgFabric-Signature: sha256=$(printf '%s' "$BODY" \
      | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -r | cut -d' ' -f1)" \
  -d "$BODY"
```

Four properties it needs, and how each is met:

1. **Authenticated.** The URL is public, so unsigned means anonymous write
   access. HMAC-SHA256 over the **raw** body, compared with
   `hmac.compare_digest`. Re-serialising parsed JSON would change the bytes and
   never match. No `WEBHOOK_SECRET` set → 503, never "accept anyway".
2. **Idempotent.** Senders retry on timeout. `X-AgFabric-Event-Id` is stored
   unique, so a replay returns the original outcome instead of a second
   delivery. There is a second layer too: a different event id carrying the same
   ticket number returns `ignored`.
3. **Durable before processed.** The event row commits on arrival, then the
   handler runs. A handler crash marks the event `failed` with the reason and
   keeps the payload, so it is replayable rather than lost.
4. **Fast to accept.** Validate, store, `202`. Anything slow belongs in an agent.

It rejects rather than guesses: an unknown customer name fails the event instead
of quietly creating a customer, and `tare_lbs >= gross_lbs` is refused as
physically impossible. `net_bu` is always derived from the weights, never trusted
from the payload.

## Open-Meteo (third-party ETL)

`POST /agents/weather/run`, or hourly via Celery beat. Real API, no key, no cost.

Fetch → validate shape → normalise → upsert on `(facility, date)`. The upsert is
the point: re-running refreshes a forecast rather than duplicating it, so a retry
after a partial failure is safe. Verified live — 6 observations across 2
facilities, then a re-sync left it at 6 rows.

Bounded timeout, and the response shape is checked before the transform so an
upstream change gives one clear error instead of a `KeyError` three layers down.
One facility failing does not abort the others.

## Background work: two options

Default is the in-process asyncio scheduler — no broker needed.

Setting `CELERY_BROKER_URL` switches to Celery **and disables the in-process
loop**, so agents never run twice:

```bash
docker compose --profile celery up -d worker beat
```

[tasks.py](backend/app/tasks.py) is deliberately thin — every task opens a
session and calls `agents.execute`, the same function the API and the asyncio
scheduler call. One implementation per job, no drift. `task_acks_late` is safe
because the agents are idempotent: the risk scan reconciles by fingerprint and
the embedding backfill skips chunks already indexed.

## Indexes and query performance

```bash
python benchmark.py --grow 60000   # grow the tables, then measure
python benchmark.py                # measure the current schema
```

Compares each real query with its index against the same query with
`enable_indexscan`/`enable_bitmapscan` off — isolating the index's effect without
dropping anything. Measured at 60,060 deliveries and 6,009 contracts:

| query | no index | with index | gain | plan change |
| --- | --- | --- | --- | --- |
| unverified deliveries | 4.29ms | 0.19ms | **22.5x** | Seq Scan → Index Only Scan |
| 10 most recent deliveries | 7.30ms | 0.10ms | **75.2x** | Seq Scan → Index Scan Backward |
| open contracts | 0.66ms | 0.16ms | 4.1x | Seq Scan → Index Only Scan |
| contracts expiring in 30d | 0.36ms | 0.11ms | 3.2x | Seq Scan → Index Only Scan |
| financial summary by status | 0.03ms | 0.04ms | — | Seq Scan (both) |
| documents newest first | 0.07ms | 0.05ms | — | Seq Scan (both) |

The `(both)` rows are the honest part: on a 10-row invoices table Postgres
sequential-scans whichever way, because that genuinely is faster. Those indexes
exist for when the tables grow, and the tool reports that rather than claiming a
win it cannot measure.

`ix_contracts_status_end_date` is composite in that order because the
expiring-30d query filters `status` then ranges over `end_date`; one index serves
both, and the leading column alone still serves the plain open-contracts count.

## Audit Center

Every `/query` is recorded: who asked, what was retrieved, what came back, which
model, how many tokens, what it cost, and how long it took.

```
GET /audit?endpoint=/query&since_hours=24
GET /audit/{id}        full detail — answer, sources, generated SQL
GET /audit/summary     spend and volume rollup (accountant or exec)
```

Access rule matches the rest of the app: **finance roles read everything
including cost; everyone else reads only their own requests and sees no cost
figures.** Reading someone else's entry returns 404, not 403 — a 403 would
confirm it exists.

The detail view stores the *evidence*, not just the answer: chunk ids with their
document and sha256, what the question resolved to, and the SQL that actually
ran. An answer is only defensible if it can be re-examined later.

**Writing an audit row never fails a request.** The answer was already produced
and is owed to the caller, so a failed write is logged loudly instead.

Every response carries `X-Request-Id`, stored on the audit row. An inbound
`X-Request-Id` is honoured so a proxy or frontend can supply its own. That is a
cheap slice of what tracing gives you — one id links a log line, an audit row, and
the response a user is looking at.

## Metrics and dashboards

```bash
docker compose --profile observability up -d prometheus grafana
# Grafana  http://localhost:3001  (admin/admin), dashboard pre-provisioned
# Prometheus http://localhost:9090
```

`GET /metrics` exports ~99 series: request rate and latency histograms, LLM
tokens and spend, agent runs and durations, SQL-gate rejections, webhook events,
open alerts by severity, documents, and the embedding backlog.

Set `METRICS_TOKEN` to require `Authorization: Bearer <token>` on `/metrics`
(Prometheus sends it via `bearer_token` in its scrape config). Left blank the
endpoint is open, which is fine on localhost and not fine anywhere public.

**The `path` label is the route template, never the raw URL.** Labelling by raw
path would mint a new time series per document id and eventually take the scraper
down — that is the usual way self-hosted Prometheus falls over. Asserted in
`smoketest.py`.

Six alert rules in [rules.yml](observability/rules.yml), validated with
`promtool`: API down, SQL gate blocking queries, embedding backlog, repeated agent
failures, p95 `/query` latency, and an hourly LLM spend spike. These are system
conditions, as distinct from the grain conditions the risk agent raises.

**OpenTelemetry is deliberately not here.** It would add a dozen packages plus a
collector to run, and without a collector it is dead weight. `prometheus-client`
is ~50KB of pure Python and covers what is actually needed; the request-id
middleware covers the correlation part.

## Migrations

Alembic owns the schema. `migrations/env.py` reads `DATABASE_URL` and
`Base.metadata` from the app, so neither is defined twice.

```bash
alembic upgrade head      # apply
alembic check             # fails if models and migrations disagree
alembic revision --autogenerate -m "what changed"
alembic downgrade -1      # verified reversible
```

`seed.py` runs `alembic upgrade head` rather than `create_all` — deliberately. If
the seed built the schema itself, the migrations could drift from the models with
nothing noticing. This way the demo database and a deployed one are built by
exactly the same path, and `alembic check` in CI catches drift.

This is what makes moving to Neon or Supabase a `DATABASE_URL` change plus
`alembic upgrade head`.

## CORS

Explicit origins, methods and headers — no wildcards:

```
CORS_ORIGINS=http://localhost:3000,https://your-app.vercel.app
```

`allow_credentials` is **False** on purpose. Auth is a Bearer token in a header,
not a cookie, so the browser never needs to send credentials cross-origin;
enabling it would only widen what a hostile page could do. Methods are limited to
`GET, POST, OPTIONS` and headers to `Authorization, Content-Type,
X-AgFabric-Signature`.

Verified: a preflight from `http://localhost:3000` returns 200 with the origin
echoed, one from `https://evil.example.com` returns 400 with no
`Access-Control-Allow-Origin`.

## Demo data

`python -m app.seed` **drops all tables, runs the migrations**, then loads a fixed
synthetic dataset: 4 users, 8 customers, 2 facilities, 6 bins, 8 contracts,
60 deliveries, 9 invoices.

It deliberately plants one of each risk condition so the Risk Center returns
real findings rather than mocks:

- duplicate invoice pair (same customer, same amount, 3 days apart)
- bin recorded above physical capacity
- bin above the 15% moisture ceiling
- open contract expiring within 30 days
- open contract 200 days past its start with no deliveries
- a delivery whose net bushels contradict its scale weights

## Deferred

- **Cloud host (Azure vs AWS) and hosted Postgres (Neon vs Supabase)** — all
  undecided, and nothing depends on any of them. `DATABASE_URL` is the only
  coupling point, and MinIO speaks the S3 API.
- **Redis-backed rate limiting** — the login limiter counts in-process, so the
  limit is per API worker. Move it to Redis before running more than one.
- **Nine AI agents** — five are built. Notification, forecast, analytics and
  graph maintenance have no work to do yet.
- **Prometheus / Grafana / audit log** — Phase 6.
- **Camera Intelligence — dropped.** It had no data source, so its overlay could
  only ever be a mock over seeded rows. Replaced by image OCR, which reads the
  paperwork that actually arrives as a photo.
