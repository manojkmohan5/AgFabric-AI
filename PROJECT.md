# AgFabric AI — Project Document

An agricultural operations intelligence platform for grain handling. It ingests a
grain business's own records alongside live market, weather, currency and news
data, detects problems in that data automatically, and answers plain-English
questions about it with citations back to the source.

Status: **complete.** Backend, frontend, migrations, background agents, CI and
both check suites are in place and running.

---

## 1. What it does

| Capability | Summary |
|---|---|
| **Operational dashboard** | Storage utilisation, deliveries, contracts, open risk, mark-to-market grain position, currency strength and market news — all live. |
| **Risk Center** | Seven rules detect data-integrity and commercial problems, each tracked as a stateful condition with a full lifecycle. |
| **Hybrid AI search** | One question fans out to structured lookups, generated SQL, vector search over documents and graph traversal, then answers with citations. |
| **Knowledge graph** | Entity relationships derived from PostgreSQL foreign keys, explorable by hops out from any record. |
| **Document pipeline** | Upload → text extraction → chunking → embedding → vector index, deduplicated by content hash and versioned. |
| **Background agents** | Eight agents on independent schedules keep market, weather, currency, news, risk findings and embeddings current. |
| **Webhook ingestion** | Signed inbound delivery events from scale-house systems, idempotent on replay. |

---

## 2. Technology stack

### Backend — Python 3.11

| Component | Technology | Role |
|---|---|---|
| Web framework | **FastAPI** ≥0.115 | REST API, dependency injection, automatic OpenAPI |
| ASGI server | **Uvicorn** ≥0.32 (standard) | Production server |
| ORM | **SQLAlchemy 2.0** | Typed `Mapped` / `mapped_column` models, 2.0 `select()` style |
| Database driver | **psycopg 3.2** (binary) | PostgreSQL connectivity |
| Migrations | **Alembic** ≥1.13 | Versioned schema, single source of truth |
| Configuration | **pydantic-settings** ≥2.6 | Typed settings with validation at startup |
| Auth | **PyJWT** ≥2.9 + `hashlib.scrypt` | Bearer tokens, password hashing |
| Vector store client | **qdrant-client** ≥1.12 | Embedding storage and similarity search |
| Object storage client | **minio** ≥7.2 | S3-compatible file storage |
| LLM client | **openai** ≥1.57 | Chat completions and embeddings |
| HTTP client | **httpx** ≥0.27 | Third-party API calls with bounded timeouts |
| Task queue | **Celery** ≥5.4 (Redis broker) | Optional distributed agent execution |
| Metrics | **prometheus-client** ≥0.21 | Counters and histograms at `/metrics` |
| Document parsing | **pypdf**, **python-docx**, **openpyxl** | PDF, Word, Excel text extraction |
| XML parsing | **defusedxml** ≥0.7 | Hardened RSS parsing |
| Uploads | **python-multipart** ≥0.0.18 | Multipart form handling |

Dependencies were kept deliberately light: `minio` (~1 MB) rather than `boto3`
(~50 MB with botocore) for the same S3 API, and the three document parsers are
pure Python, so nothing in the backend pulls a native toolchain.

### Frontend — TypeScript

| Component | Technology | Role |
|---|---|---|
| Framework | **Next.js 15** (App Router) | Routing, server components, build pipeline |
| UI library | **React 19** | Component model |
| Language | **TypeScript 5.7** | End-to-end typing, including the API client |
| Styling | **Tailwind CSS 4** | Utility styling over a CSS custom-property token set |
| Server state | **TanStack Query 5** | Fetching, caching, polling, cache invalidation |
| Client state | **Zustand 5** | Auth session store |
| Animation | **motion** 12 (Framer Motion) | Reveal transitions, reduced-motion aware |
| Graph layout | **d3-force** 3 | Force-directed knowledge-graph positioning |

Charts are hand-built SVG rather than a charting library, which keeps full control
over marks, labelling and accessibility, and adds no bundle weight.

### Data stores and infrastructure

| Component | Technology | Role |
|---|---|---|
| Relational database | **PostgreSQL 17** (alpine) | Operational records; also the knowledge graph, via foreign keys |
| Vector database | **Qdrant** | Document chunk embeddings, cosine similarity |
| Object storage | **MinIO** | Original uploaded files, S3 API compatible |
| Cache / broker | **Redis 7** (alpine) | Celery broker and result backend |
| Metrics | **Prometheus** + **Grafana** | Scraping and dashboards |
| Containers | **Docker Compose** | Nine services for local development |
| CI | **GitHub Actions** | Lint, type check, both suites against real services |

### AI models

| Purpose | Model | Notes |
|---|---|---|
| Chat and text-to-SQL | **gpt-4.1-nano** | $0.10 / $0.40 per million tokens |
| Embeddings | **text-embedding-3-small** | 1536 dimensions, $0.02 per million tokens |
| Image OCR | **gpt-4.1-nano** vision | No local model, no native binary |

Measured cost in normal use: **~$0.00015 per question.**

### Tooling

**Ruff** for linting and formatting, **Prettier** for the frontend, `tsc
--noEmit` for type checking, and two runnable check suites (`selfcheck.py`,
`smoketest.py`) plus an index benchmark.

---

## 3. Architecture

```
Next.js 15 / React 19 / TypeScript          7 pages, role-aware
        │  REST over HTTPS, Bearer JWT
FastAPI  ─┬─ PostgreSQL 17    operational records + knowledge graph (foreign keys)
          ├─ Qdrant           chunk embeddings
          ├─ MinIO (S3)       original uploaded files
          ├─ Redis            optional Celery broker
          └─ OpenAI           chat + embeddings
```

**Scale:** 35 backend modules · 18 ORM models · 28 API routes · 5 Alembic
migrations · 7 frontend pages.

---

## 4. Design decisions

**Provider seams with deterministic fakes.** Embeddings, chat, OCR, market data
and feeds each sit behind an `auto | real | fake` switch. With no API key
configured the fakes activate, and the fake chat provider is a genuine extractive
baseline rather than a stub — it scores context lines by overlap with the question
and returns the best ones. Both check suites pin the fakes explicitly, so the
full suite runs offline at zero cost.

**The knowledge graph is the relational schema.** No graph database. A node is
`kind:pk`, an edge is a foreign key, and traversal is a bounded breadth-first
search over those keys in both directions. The relationships already existed in
Postgres, so nothing needed duplicating into a second store.

**Text-to-SQL is guarded.** The model writes a query, then a static gate enforces
single-statement, `SELECT`/`WITH` only, no comments, no DML or DDL, and a table
allowlist that excludes `users`. Execution happens inside a PostgreSQL
`READ ONLY` transaction under a statement timeout. A rejected query is reported
back with the reason rather than run.

**Structured lookups run before the model.** Identifiers named in a question —
contract numbers, ticket numbers, bin names, customer names — are resolved and
loaded directly from the database. A question about a specific record therefore
never depends on the model composing correct SQL; text-to-SQL serves aggregate
questions.

**Alerts are conditions, not events.** Each finding carries a `blake2b`
fingerprint over its identity alone, never its wording or confidence score. Every
scan reconciles four ways — `created`, `updated`, `reopened`, `auto_resolved` — so
scans are safe to run continuously: recurring problems reopen rather than
duplicating, and conditions that stop being true close themselves.

**Per-agent schedules.** Each agent carries its own cadence, matched to how often
its source actually changes: risk scanning every 5 minutes, market every 15,
weather and news every 30, currency every 6 hours. All eight also run once at
startup, in the background, so the application serves requests immediately while
its panels populate.

**Idempotent ingestion.** Every third-party sync is an upsert on a natural key,
so a retry after a partial failure cannot double-write. Inbound webhooks
deduplicate on an event key. First observations report a null change rather than
a fabricated zero percent.

**Cost accounting is per-model.** Pricing is keyed by model name, with dated
snapshots resolving to their base model, and an unknown model priced
conservatively high so the spend cap stays effective. A rolling 24-hour cap sums
actual logged spend and returns 503 at the ceiling.

**Accessibility is verified.** WCAG 2.1 AA throughout: semantic landmarks, a skip
link, `aria-live` regions, keyboard-reachable scroll containers, and a data table
alongside every chart. Chart palettes are validated by script for colour-vision
separation and contrast. Status is always carried by an icon *and* a label, never
colour alone.

---

## 5. Live data sources

All free tiers; no keys required except OpenAI.

| Source | Provides | Notes |
|---|---|---|
| **Open-Meteo** | Per-facility daily forecasts | No API key; six facilities mapped by coordinate |
| **Yahoo Finance** | CBOT corn, soybean and wheat futures | `ZC=F` `ZS=F` `ZW=F`; cents-per-bushel normalised at ingest |
| **open.er-api.com** | USD rates for six export currencies | Brazil, China, Argentina, euro area, Canada, Mexico |
| **Google News RSS** | Agricultural trade headlines | Parsed with `defusedxml` |

---

## 6. Security

- JWT bearer authentication, scrypt password hashing, and a startup refusal if
  `JWT_SECRET` is shorter than 32 characters.
- Four roles — `ops`, `accountant`, `warehouse`, `exec`. Financial values are
  redacted server-side for roles that may not see them, not merely hidden in the
  interface.
- Account deactivation invalidates a live token immediately. The last active
  administrator cannot be deactivated, and no account can deactivate itself.
- Inbound webhooks require an HMAC-SHA256 signature computed over the raw request
  body.
- Sliding-window rate limits on both sign-in and query endpoints.
- Seeding drops every table, so it requires an explicit
  `ALLOW_DESTRUCTIVE_SEED` confirmation to run.
- A full audit log records user, role, endpoint, question, model, token counts,
  cost and latency for every AI request.

---

## 7. Verification

| Suite | Count | Scope |
|---|---|---|
| `selfcheck.py` | 34 checks | Pure logic — password hashing, token handling, all risk rules, the SQL validation gate, graph construction. No database, no network. |
| `smoketest.py` | 16 checks | Full API against a seeded database: auth, role enforcement, retrieval, text-to-SQL, read-only enforcement, agents, webhooks, audit, metrics, rate limits. |
| `benchmark.py` | — | Reports query timing with and without indexes, and the plan Postgres chose in each case. |

Both suites run offline at zero cost, with assertions derived from the seed
constants. GitHub Actions runs linting, type checks and both suites against real
Postgres, Qdrant and MinIO service containers.

Prometheus metrics use route-template labels to keep cardinality bounded, and can
be placed behind a bearer token.

---

## 8. Running it

```bash
docker compose up -d                  # Postgres 5433, Qdrant 6333, MinIO 9000
cp .env.example .env                  # set JWT_SECRET; OPENAI_API_KEY optional
cd backend && alembic upgrade head
ALLOW_DESTRUCTIVE_SEED=true python -m app.seed    # optional demo dataset
uvicorn app.main:app --port 8000
cd ../frontend && npm run dev         # port 3000
```

`CORS_ORIGINS` must list the frontend origin exactly.

**Two ways to start.** Either seed the demo dataset — 6 facilities, 20 customers,
21 bins, 22 contracts, 260 deliveries and 23 invoices, including one instance of
every risk condition so the findings are real — or start from empty: set
`BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD`, sign in, then create
records through `/admin/users` and `/provision/*`. The second path holds no
synthetic data at all.

---

## 9. Repository layout

```
backend/
  app/          35 modules — API, agents, risk rules, retrieval, providers
  migrations/   5 Alembic revisions (the single source of schema truth)
  selfcheck.py  34 pure-logic checks
  smoketest.py  16 end-to-end checks
  benchmark.py  index effectiveness report
frontend/
  app/          7 pages plus sign-in
  components/   charts, graph view, widgets, shell, weather icons
  lib/          typed API client, auth store
.github/        CI pipeline
docker-compose.yml
```
