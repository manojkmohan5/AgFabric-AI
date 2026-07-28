# AgFabric AI

## Enterprise Agricultural Intelligence Platform

### Product Design & Build Plan (Version 1.0)

## Executive Summary

AgFabric AI is an AI-native enterprise intelligence platform that
unifies operational systems, business data, documents, AI services, and
analytics into a single operational platform for agricultural
organizations. Rather than acting as a chatbot, it serves as the backend
intelligence layer that powers future AI applications.

------------------------------------------------------------------------

# Vision

**Build the AI Data Foundation for Agriculture**

Core principles:

-   Enterprise-first architecture
-   Explainable AI
-   Source traceability
-   Production-grade engineering
-   AI as infrastructure, not as the product

------------------------------------------------------------------------

# Objectives

-   Build an enterprise AI data fabric
-   Connect multiple operational systems
-   Provide hybrid AI search
-   Create a business knowledge graph
-   Detect operational risks automatically
-   Expose scalable APIs
-   Demonstrate production-ready engineering

------------------------------------------------------------------------

# Users

-   Operations Manager
-   Grain Accountant
-   Warehouse Manager
-   Executive Leadership

------------------------------------------------------------------------

# Platform Architecture

``` text
Users
    │
Next.js Frontend
    │
FastAPI Gateway
    │
Authentication
    │
Enterprise Data Fabric
    │
──────────────────────────────────
│ Event Processing               │
│ Metadata Extraction            │
│ Entity Resolution              │
│ Embedding Pipeline             │
│ SQL Database  (+ graph view)   │
│ Vector Database                │
│ AI Agents                      │
│ Risk Engine                    │
──────────────────────────────────
    │
Dashboard + APIs
```

# Technology Stack

## Frontend

-   Next.js 15
-   React
-   TypeScript
-   TailwindCSS
-   shadcn/ui
-   React Query
-   Zustand

## Backend

-   FastAPI
-   SQLAlchemy
-   Alembic
-   Celery
-   Redis

## Databases

-   PostgreSQL
-   Qdrant
-   Redis

No graph database. The knowledge graph is derived from PostgreSQL foreign
keys rather than mirrored into Neo4j — see Core Module 5.

## AI

-   OpenAI GPT-4o / GPT-4o-mini via `OPENAI_API_KEY`
-   OpenAI `text-embedding-3-small` (1536 dims)
-   Qdrant for vector search, with the score used as the rerank signal

Reached through one thin provider seam so a fake implementation serves
tests and CI — no API key and no network needed to run the checks.

No local model weights. Embeddings come from the API, which keeps the
repo and the Docker image small; there is no ONNX runtime, no PyTorch,
and nothing to download on first run.

LangGraph and LlamaIndex are not used. The OpenAI SDK plus the retrieval
code already here covers this pipeline; add a framework only if agent
orchestration outgrows plain functions.

## Infrastructure

-   Docker
-   Docker Compose
-   GitHub Actions
-   MinIO (S3-compatible object storage, self-hosted)
-   Prometheus
-   Grafana
-   OpenTelemetry

Runs entirely on localhost via `docker compose up`. No cloud account
required. MinIO speaks the S3 API, so swapping in S3/R2/Azure Blob later
is a config change, not a code change.

------------------------------------------------------------------------

# Cost

Free, self-hosted in Docker: PostgreSQL, Qdrant, Redis, MinIO,
Prometheus, Grafana, FastAPI, Next.js.

**Paid: the OpenAI API.** This is the one line item in the build. At demo
scale it is cents rather than dollars — embedding the seeded corpus with
`text-embedding-3-small` costs a fraction of a cent, and answers come
from `gpt-4.1-nano` by default. The audit log records tokens and cost per
request (Core Module 11) so the spend is visible rather than a surprise.

Also paid above their free tiers: weather and commodity APIs. Open-Meteo
is genuinely free and needs no key.

Because tests use a fake provider, the whole check suite and CI run at
$0 with no API key present.

------------------------------------------------------------------------

# Core Modules

## 1. Enterprise Dashboard

-   Operational Health
-   AI Agent Status
-   Storage Capacity
-   Deliveries
-   Contracts
-   Financial Summary
-   Weather
-   Commodity Prices
-   Recent Events

## 2. Enterprise Data Fabric

Supports: - ERP - CSV - Excel - PDF - DOCX - Emails - Camera Metadata -
Weather API - Commodity API - IoT Sensors

Pipeline:

Data → Validation → Metadata → Entity Resolution → Event Queue →
PostgreSQL → Qdrant

## 3. Smart Document Intelligence

Features: - OCR - Chunking - Embeddings - Versioning - Semantic Search -
Duplicate Detection - Source Traceability

## 4. Hybrid AI Search

Combines: - SQL - Vector Search - Knowledge Graph - Live APIs

## 5. Knowledge Graph

Entities: - Customer - Farmer - Contract - Delivery - Invoice -
Payment - Facility - Storage Bin - Commodity

Backed by PostgreSQL, not a graph database. Nodes are table rows
(`customer:3`), edges are the existing foreign keys, and traversal is a
breadth-first expansion over the edge list. No Neo4j, no projection job,
and no dual-write consistency problem — PostgreSQL stays the only source
of truth. Revisit only if traversals deeper than three hops become a
routine query pattern.

## 6. AI Risk Center

Detects: - Duplicate invoices - Inventory mismatch - Moisture
anomalies - Contract expiration - Missing deliveries - Data
inconsistencies

Every alert contains: - Confidence - Evidence - Recommendation - Source
Documents

## 7. Digital Twin

Live operational model for: - Facilities - Storage - Deliveries -
Contracts - Inventory - Weather

## 8. Image & Scanned Document Intelligence

Replaces the original Camera Intelligence module, which was dropped: it
had no data source, so its overlay could only ever have been a mock over
seeded rows.

What it does instead — read the paperwork that arrives as a photo:

-   Scale tickets, contracts and delivery notes photographed on a phone
-   Scanned PDFs with no embedded text layer
-   Accepted formats: PNG, JPEG, WEBP, GIF

OCR runs through the vision API rather than a local engine, so it adds no
install weight — no PyTorch, no ONNX runtime, no tesseract binary. For
scanned PDFs the embedded images are pulled out with pypdf and read
directly, which avoids a PDF-rasterising dependency as well.

Images are validated by magic bytes, never by the claimed extension or
content-type, so a renamed executable is refused before it is uploaded,
billed for, or stored.

Once transcribed, an image is an ordinary document: chunked, embedded,
searchable, and citable in an answer with the same traceability as a
PDF.

## 9. AI Agent Center

Agents: - Document - Embedding - Entity Resolution - Risk -
Notification - Forecast - Analytics - Knowledge Graph - Monitoring

## 10. Explainable AI

Every response includes: - Confidence - Sources - SQL Evidence -
Retrieved Chunks - Graph Relationships - Model - Latency

## 11. Audit Center

Logs: - User - Prompt - Retrieved Data - Response - Timestamp - Cost -
Sources

## 12. Monitoring

-   API Health
-   Queue Status
-   Token Usage
-   AI Cost
-   Database Health
-   Embedding Latency

------------------------------------------------------------------------

# UI Pages

-   Dashboard
-   Operations
-   Documents
-   Contracts
-   Deliveries
-   Storage
-   AI Search
-   Knowledge Graph
-   Risk Center
-   Forecasting
-   Audit
-   Monitoring
-   Settings

------------------------------------------------------------------------

# API Endpoints

## Auth

-   POST /login
-   POST /logout

## Documents

-   POST /documents/upload
-   GET /documents

## Search

-   POST /search
-   POST /query

## Graph

-   GET /graph
-   GET /graph/entity/{kind}:{id}?depth=N

## Dashboard

-   GET /dashboard

## Alerts

-   GET /alerts

## Monitoring

-   GET /metrics
-   GET /health

------------------------------------------------------------------------

# Folder Structure

``` text
agfabric-ai/
├── frontend/
├── backend/
├── api/
├── agents/
├── services/
├── workers/
├── database/
├── vector/
├── graph/
├── embeddings/
├── monitoring/
├── docker/
├── infra/
├── docs/
└── tests/
```

------------------------------------------------------------------------

# Security

-   JWT Authentication
-   RBAC
-   HTTPS
-   Audit Logs
-   Rate Limiting
-   Secure Uploads

------------------------------------------------------------------------

# Development Roadmap

## Sprint 1

-   Project setup
-   Auth
-   Dashboard
-   PostgreSQL
-   FastAPI

## Sprint 2

-   Upload
-   Embeddings
-   Qdrant
-   Search APIs

## Sprint 3

-   Knowledge Graph (PostgreSQL-derived)
-   AI Agents
-   Risk Center

## Sprint 4

-   Monitoring
-   Demo

Cloud deployment (Azure vs AWS) is deferred — not decided, not needed.
Everything runs on `docker compose up` locally. The stack uses no
provider-specific services, so the choice stays open and costs nothing
to postpone.

------------------------------------------------------------------------

# Interview Demo

1.  Dashboard Overview
2.  Upload a Contract
3.  Automatic Processing
4.  Hybrid AI Search
5.  Knowledge Graph Navigation
6.  Risk Detection
7.  Photograph a Scale Ticket → OCR → Searchable
8.  Monitoring Dashboard

------------------------------------------------------------------------

# Future Roadmap

-   Computer Vision
-   Drone Integration
-   IoT Analytics
-   Predictive Forecasting
-   Voice Assistant
-   Multi-Tenant SaaS
-   Mobile Application
-   Autonomous Workflow Automation

------------------------------------------------------------------------

# Conclusion

AgFabric AI demonstrates a production-oriented AI engineering platform
combining enterprise backend development, data engineering, RAG,
knowledge graphs, explainable AI, monitoring, and scalable APIs. It is
designed to showcase the foundational platform on which future
agricultural AI applications can be built.
