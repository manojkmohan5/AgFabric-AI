# AgFabric AI

Agricultural operations intelligence for grain handling. It combines a grain
business's own records with live market, weather, currency and news data, detects
problems in that data automatically, and answers plain-English questions with
citations back to the source.

Runs entirely on localhost. No cloud account required; every external data source
is a free tier.

See [PROJECT.md](PROJECT.md) for the project document and
[AgFabric_AI_Build_Plan.md](AgFabric_AI_Build_Plan.md) for the design.

---

## Quick start

```bash
docker compose up -d          # Postgres :5433, Qdrant :6333, MinIO :9000, Redis
cp .env.example .env          # set JWT_SECRET (32+ chars); OPENAI_API_KEY optional

cd backend
pip install -r requirements.txt
alembic upgrade head
ALLOW_DESTRUCTIVE_SEED=true python -m app.seed    # optional demo dataset
uvicorn app.main:app --reload --port 8000

cd ../frontend
npm install
npm run dev                   # http://localhost:3000
```

API docs at `:8000/docs`. Grafana at `:3001` (admin/admin) with the dashboard
pre-provisioned; Prometheus at `:9090`.

Without an `OPENAI_API_KEY` the app runs on deterministic local fakes for
embeddings, chat and OCR, so everything works offline at zero cost.

### Two ways to populate it

**Demo dataset** — `ALLOW_DESTRUCTIVE_SEED=true python -m app.seed` creates 6
facilities, 20 customers, 21 bins, 22 contracts, 260 deliveries and 23 invoices,
including one instance of every risk condition so the findings are real. The flag
is required because seeding drops every table.

**From empty** — set `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD`, start
the API, sign in, then create records through `POST /admin/users` and
`POST /provision/*`. This path holds no synthetic data at all.

Demo accounts all use `SEED_PASSWORD`:

| Account | Role | Sees |
| --- | --- | --- |
| `exec@agfabric.test` | exec | Everything, including financials |
| `accounting@agfabric.test` | accountant | Financials |
| `ops@agfabric.test` | ops | Operations; can run agents |
| `warehouse@agfabric.test` | warehouse | Operations, no financials |

---

## Technology

**Backend** — FastAPI, SQLAlchemy 2.0 (typed `Mapped`), Alembic, psycopg 3,
pydantic-settings, PyJWT, Celery (optional), prometheus-client.

**Frontend** — Next.js 15, React 19, TypeScript 5.7, Tailwind 4, TanStack Query,
Zustand, `motion`, d3-force. Charts are hand-built SVG.

**Data** — PostgreSQL 17, Qdrant (vectors), MinIO (S3-compatible object storage),
Redis (Celery broker).

**AI** — `gpt-4.1-nano` for chat, text-to-SQL and vision OCR;
`text-embedding-3-small` at 1536 dimensions. Roughly $0.00015 per question.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/login`, `/logout` | JWT bearer auth |
| `GET` | `/health` | Per-dependency probe |
| `GET` | `/dashboard` | Operational summary, role-filtered |
| `GET` | `/storage` | Bin inventory |
| `GET` | `/alerts` | Risk findings; filter by status, severity, kind |
| `POST` | `/alerts/{id}/acknowledge`, `/resolve` | Move an alert's status |
| `GET` | `/graph`, `/graph/entity/{node_id}` | Graph overview and traversal |
| `POST` | `/documents/upload` | Upload, extract, chunk, embed |
| `GET` | `/documents/`, `/documents/{id}` | List and detail |
| `POST` | `/search`, `/search/reindex` | Vector search; rebuild the index |
| `GET` | `/search/status` | Index and provider state |
| `POST` | `/query` | Hybrid answer with full explanation |
| `GET` | `/market` | CBOT board and mark-to-market position |
| `GET` | `/weather` | Per-facility forecasts |
| `GET` | `/feeds` | Currency rates and market news |
| `GET` | `/agents`, `/agents/runs` | Registry and run history |
| `POST` | `/agents/{name}/run` | Trigger an agent (ops, exec) |
| `POST` | `/provision/*` | Create commodities, facilities, bins, customers, contracts |
| `GET`/`POST` | `/admin/users` | User provisioning (exec) |
| `POST` | `/webhooks/deliveries` | HMAC-signed delivery ingestion |
| `GET` | `/audit/`, `/audit/summary` | AI request audit trail |
| `GET` | `/metrics` | Prometheus exposition |

Contract prices and invoice amounts come back as `"redacted"` for ops and
warehouse. Redaction happens server-side, so the API never sends figures the
caller is not entitled to.

---

## Knowledge graph

No graph database. Nodes are rows (`customer:3`), edges are the foreign keys that
already exist, and traversal is breadth-first up to 3 hops —
[graph.py](backend/app/graph.py). PostgreSQL stays the only source of truth, so
there is no projection job and no dual-write consistency problem.

---

## Hybrid query

`POST /query` with `{"question": "...", "limit": 6, "graph_depth": 1}` runs four
retrievals and answers from their union.

1. **Named-entity lookup** — identifiers in the question (`C-2026-1000`,
   `T-80011`, `INV-5099`, `ANOM-01`) and customer names resolve to actual rows.
   This is exact and costs no model call, so it runs first; a question about a
   specific record never depends on the model writing correct SQL.
2. **Text-to-SQL** — the model writes a query for what fixed lookups cannot
   express: aggregates, rankings, comparisons.
3. **Vector** — semantically similar document chunks.
4. **Graph** — what each resolved entity connects to.

The response is a full explainable envelope: answer, confidence, `sql_evidence`,
`generated_sql`, `graph_relationships`, `retrieved_chunks` with source
traceability, what was resolved, plus provider, model, tokens, cost and latency.

### Guarding generated SQL

Generated SQL is untrusted input that happens to be executable, so three
independent layers sit between generation and results:

1. **A static gate** ([sqlgen.py](backend/app/sqlgen.py) `validate()`) — one
   statement only, `SELECT`/`WITH` only, no comments, no DML/DDL, no dangerous
   functions, every table on an allowlist, `users` unreachable by any route
   including `UNION` and subqueries, and a `LIMIT` imposed if absent.
2. **A Postgres `READ ONLY` transaction** — the layer that does not depend on
   those regexes being complete. A write is refused by the database itself.
3. **`statement_timeout`** — a pathological query cannot hold a connection.

Nothing is silently discarded: a rejected query reports its reason in
`explanation.generated_sql.rejected`, and the structured, graph and document
context still answer the question. Blocked queries log at `WARNING`; a model
declining an unanswerable question logs at `INFO`, so alerting can target the
first without drowning in the second.

The gate has four dedicated test groups in `selfcheck.py` covering writes,
`users` access, catalogue tables, comment smuggling and multi-statement chaining.
`smoketest.py` additionally bypasses the gate and hands writes straight to the
executor, asserting Postgres refuses them.

---

## Documents

`POST /documents/upload` accepts `.pdf .docx .xlsx .csv .txt .md` up to 25 MB,
plus `.png .jpg .jpeg .webp .gif`. Text is extracted, chunked and embedded on the
request.

- **Duplicates are exact, not fuzzy.** The SHA-256 of the content is the key, so
  re-uploading the same bytes under a different name returns the original.
- **Same filename with different bytes is a new version.** Nothing is overwritten.
- **The object key is the content hash, never the filename.** A name like
  `../../etc/passwd.txt` is stored as a label only; it cannot influence where the
  bytes land.

If embedding fails — no key, rate limit, Qdrant unreachable — the document is
still stored and the response carries a non-null `index_error`, which
`POST /search/reindex` picks up later. An upload is never lost because the index
was unavailable.

### Images and OCR

OCR runs through the vision API rather than a local engine, which keeps the
install weight at zero:

| Option | Cost to install |
| --- | --- |
| easyocr / paddleocr | Pulls PyTorch, ~2 GB |
| rapidocr-onnxruntime | ~65 MB of wheels plus a model download |
| pytesseract | Needs the tesseract **binary** in the image |
| `gpt-4.1-nano` vision | **Zero new dependencies** |

An OpenAI key is already needed for embeddings and answers, so this adds nothing.
`OCR_PROVIDER=auto|openai|fake` follows the same pattern as everything else, so
the checks run offline and free on `fake`.

Scanned PDFs are handled too: pypdf pulls embedded images out of a page without
rasterising, so those are OCR'd directly — no poppler, no pdfium. Bounded to the
first 5 pages, since each page is a billed vision call.

**Images are validated by magic bytes, never by extension or content-type.** Both
are attacker-controlled, and these bytes get sent to a paid API and stored. A
Windows executable named `photo.png` declaring `Content-Type: image/png` is
refused; so is a WAV file claiming to be WEBP, since `RIFF` fronts both. Ten
cases are asserted in `selfcheck.py`.

Images have a separate 6 MB ceiling, below the 25 MB document limit, because
vision is billed per image and a 20 MB photo costs real money for no extra
legibility.

---

## Search

`POST /search` with `{"query": "...", "limit": 10, "document_id": null}`. Every
hit traces back to document id, filename, content hash and chunk ordinal, and the
response reports provider, model, dimensions and latency.

`EMBEDDING_PROVIDER=auto` uses `text-embedding-3-small` when `OPENAI_API_KEY` is
set and a local deterministic fake otherwise. The fake is the hashing trick over
word tokens, not random padding, so shared vocabulary genuinely scores higher —
the search tests assert real ranking, and CI runs at $0 with no key.

Switching providers changes what the vectors mean. Delete the Qdrant collection
and `POST /search/reindex` (ops or exec) after changing it. A dimension mismatch
raises a clear error rather than silently discarding a populated index.

---

## Risk engine

Seven rules produce eight kinds of finding: duplicate invoices, inventory
mismatch, moisture anomaly, contract expiration, missing deliveries, data
inconsistency, off-market contract pricing and unhedged position.

```
GET  /alerts?status=open&severity=high&kind=moisture_anomaly
POST /alerts/{id}/acknowledge
POST /alerts/{id}/resolve
```

`/alerts` reads stored rows rather than recomputing per request, so a finding has
an id, a first-seen time and a status a human can move.

Each finding carries a `fingerprint` — a stable hash of the condition's identity,
never its wording or confidence. Re-running a scan therefore **updates** the
existing alert rather than inserting a near-duplicate; a condition that
disappears is auto-resolved; a condition a human acknowledged stays
acknowledged, because a scan must not silently undo a human decision.

Every finding carries evidence, a confidence score and a recommended action.

---

## Agents

Eight agents, each on a cadence matched to how often its source changes:

| Agent | Interval | Work |
| --- | --- | --- |
| `risk` | 5 min | Run all rules, reconcile alerts |
| `embedding` | 5 min | Embed chunks awaiting indexing |
| `market` | 15 min | CBOT closes, reprice positions |
| `monitoring` | 15 min | System counts |
| `weather` | 30 min | Open-Meteo forecasts per facility |
| `news` | 30 min | Agricultural headlines |
| `entity_resolution` | 30 min | Deliveries with no contract |
| `fx` | 6 h | USD rates for export currencies |

All eight run once at startup, in the background, so the app serves requests
immediately while its panels populate.

The default scheduler is an in-process asyncio loop, so no broker is needed.
Setting `CELERY_BROKER_URL` switches to Celery **and disables the in-process
loop**, so agents never run twice:

```bash
docker compose --profile celery up -d worker beat
```

[tasks.py](backend/app/tasks.py) is deliberately thin — every task opens a session
and calls `agents.execute`, the same function the API and the asyncio scheduler
call. One implementation per job, no drift. `task_acks_late` is safe because the
agents are idempotent: the risk scan reconciles by fingerprint and the embedding
backfill skips chunks already indexed.

---

## Live data sources

| Source | Provides |
| --- | --- |
| Open-Meteo | Per-facility daily forecasts (no key) |
| Yahoo Finance | CBOT corn, soybean, wheat futures (`ZC=F` `ZS=F` `ZW=F`) |
| open.er-api.com | USD rates for six export currencies |
| Google News RSS | Agricultural trade headlines |

Every sync is an idempotent upsert on a natural key, so a retry after a partial
failure cannot double-write. Cents-per-bushel quotes are normalised to dollars at
ingest. Responses are validated rather than trusted: a missing key gives one clear
error instead of a `KeyError` three layers down, and calls are bounded by a
timeout so a hanging upstream cannot pile up requests.

---

## Webhooks

`POST /webhooks/deliveries` accepts a delivery as it happens, rather than waiting
for someone to upload a file afterwards.

```bash
BODY='{"event":"delivery.recorded","source":"scale-house-1","data":{...}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -r | cut -d' ' -f1)
curl -X POST localhost:8000/webhooks/deliveries \
  -H "X-AgFabric-Event-Id: evt-1" \
  -H "X-AgFabric-Signature: sha256=$SIG" \
  -H 'Content-Type: application/json' -d "$BODY"
```

The signature is verified over the raw body, and the event id is unique, so a
replay returns the original outcome rather than creating a second delivery. With
`WEBHOOK_SECRET` unset the endpoint refuses everything rather than accepting
unsigned writes.

---

## Audit and cost control

Every `/query` is recorded: who asked, what was retrieved, what came back, which
model, how many tokens, what it cost and how long it took — `GET /audit/` and
`/audit/summary`.

**Writing an audit row never fails a request.** The answer was already produced,
so a logging error is recorded server-side and swallowed rather than surfaced to
the caller.

Pricing is per-model, keyed by name, with dated snapshots resolving to their base
model. A rolling 24-hour cap sums logged spend and returns 503 at the ceiling, so
a runaway loop cannot exhaust a budget. The fake provider reports token counts but
always zero cost, because pricing a free call at OpenAI rates would put fictional
spend in the audit trail.

---

## Indexes and query performance

```bash
python benchmark.py --grow 60000   # grow the tables, then measure
python benchmark.py                # measure the current schema
```

Compares each real query with its index against the same query with
`enable_indexscan`/`enable_bitmapscan` off — isolating the index's effect without
dropping anything. Measured at 60,060 deliveries and 6,009 contracts:

| Query | No index | With index | Gain | Plan change |
| --- | --- | --- | --- | --- |
| Unverified deliveries | 4.29 ms | 0.19 ms | **22.5x** | Seq Scan → Index Only Scan |
| 10 most recent deliveries | 7.30 ms | 0.10 ms | **75.2x** | Seq Scan → Index Scan Backward |
| Open contracts | 0.66 ms | 0.16 ms | 4.1x | Seq Scan → Index Only Scan |
| Contracts expiring in 30d | 0.36 ms | 0.11 ms | 3.2x | Seq Scan → Index Only Scan |
| Financial summary by status | 0.03 ms | 0.04 ms | — | Seq Scan (both) |
| Documents newest first | 0.07 ms | 0.05 ms | — | Seq Scan (both) |

The `(both)` rows are the honest part: on a small invoices table Postgres
sequential-scans whichever way, because that genuinely is faster. Those indexes
exist for when the tables grow, and the tool reports the plan rather than claiming
a win it cannot measure.

`ix_contracts_status_end_date` is composite in that order because the
expiring-30d query filters `status` then ranges over `end_date`; one index serves
both, and the leading column alone still serves the plain open-contracts count.

---

## Checks

```bash
cd backend
python selfcheck.py      # 34 pure-logic checks, no DB, no network
python smoketest.py      # 16 end-to-end checks (run after seeding)
python benchmark.py      # index effectiveness with EXPLAIN ANALYZE
```

Both suites pin the fake providers, so they run offline at zero cost regardless of
environment. CI runs lint, format, both suites and a Docker build against real
Postgres, Qdrant and MinIO containers, and verifies migrations apply, reverse and
match the models.

---

## Configuration

`.env.example` documents every setting. The ones that matter most:

| Variable | Purpose |
| --- | --- |
| `JWT_SECRET` | Required, 32+ characters; the app refuses to start without it |
| `DATABASE_URL` | The only database coupling point |
| `OPENAI_API_KEY` | Optional; blank selects the local fakes |
| `CORS_ORIGINS` | Comma-separated, no wildcards; must list the frontend origin exactly |
| `WEBHOOK_SECRET` | HMAC key; unset disables webhook ingestion |
| `ALLOW_DESTRUCTIVE_SEED` | Required for `app.seed`, which drops every table |
| `DEMO_MODE` | Lists demo accounts on the sign-in page |
| `CELERY_BROKER_URL` | Set to move agents onto Celery |
| `METRICS_TOKEN` | Requires a bearer token on `/metrics` |
| `DAILY_SPEND_CAP_USD` | Rolling 24-hour ceiling on AI spend |

### Migrations

Alembic is the single source of schema truth. The seed runs `alembic upgrade head`
rather than `create_all`, so the demo database and a deployed one are built by
exactly the same path.

```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic downgrade -1
alembic check            # fails if a model changed without a migration
```

### Metrics

`/metrics` exposes Prometheus counters and histograms with route-template labels
to keep cardinality bounded. `METRICS_TOKEN` puts it behind a bearer token, which
Prometheus supplies via `bearer_token` in its scrape config.

---

## Layout

```
backend/
  app/          35 modules — API, agents, risk rules, retrieval, providers
  migrations/   5 Alembic revisions
  selfcheck.py  34 pure-logic checks
  smoketest.py  16 end-to-end checks
  benchmark.py  index effectiveness report
frontend/
  app/          7 pages plus sign-in
  components/   charts, graph view, widgets, shell, weather icons
  lib/          typed API client, auth store
observability/  Prometheus config, alert rules, Grafana dashboard
.github/        CI pipeline
```
