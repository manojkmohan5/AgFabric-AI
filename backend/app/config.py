from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # No default: a blank/missing JWT_SECRET must fail startup, not ship a
    # guessable signing key.
    jwt_secret: str
    jwt_ttl_minutes: int = 60
    database_url: str = "postgresql+psycopg://agfabric:agfabric@localhost:5432/agfabric"
    # Dev/demo seed users only — never a real credential.
    seed_password: str = "agfabric-dev"  # noqa: S105

    # `python -m app.seed` DROPS EVERY TABLE. It refuses to run unless this is
    # explicitly true, so a deploy script or a stray shell command cannot wipe a
    # live database by accident.
    allow_destructive_seed: bool = False

    # Shows the demo-account buttons on the login page. Must be off once real
    # people have real credentials — otherwise the login screen lists staff
    # emails to anyone who loads it.
    demo_mode: bool = False

    # First-run bootstrap. With an empty users table and these set, startup
    # creates one exec account so the app is reachable without seed data. It is a
    # no-op the moment any user exists, so it cannot be used to add accounts
    # later or to reset a password.
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # Minimum length for any password this app accepts. Applied at creation, so a
    # weak password never reaches the hasher.
    min_password_length: int = 12

    # Hard ceiling on LLM spend per rolling window. This REFUSES requests rather
    # than only alerting — an alert nobody reads is not a cost control. Counted
    # from the audit log, so it survives a restart.
    enable_spend_cap: bool = True
    daily_spend_cap_usd: float = 10.0

    # Per-user throttle on the paid endpoints. /login has had one since Phase 1;
    # /query bills money on every call and had none.
    query_rate_limit: int = 30
    query_rate_window_seconds: float = 300.0

    # Object storage. Defaults point at the MinIO container; setting
    # s3_endpoint_url to "" uses real AWS S3 with no other code change.
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "agfabric"
    s3_secret_key: str = "agfabric-dev"  # noqa: S105
    s3_bucket: str = "agfabric-documents"
    s3_region: str = "us-east-1"

    max_upload_bytes: int = 25 * 1024 * 1024
    chunk_size: int = 1200
    chunk_overlap: int = 150

    # Vector search.
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "agfabric_chunks"

    # "auto" uses OpenAI when a key is present and the deterministic fake
    # otherwise, so tests and CI need no key and cost nothing. Force either with
    # EMBEDDING_PROVIDER=openai|fake.
    embedding_provider: str = "auto"
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    max_query_chars: int = 1000

    # Answer generation. Same auto/openai/fake selection as embeddings.
    llm_provider: str = "auto"
    # gpt-4.1-nano is the cheapest chat model this key can call: $0.10/$0.40 per
    # Mtok against gpt-4o-mini's $0.15/$0.60 and gpt-5.4-nano's $0.20/$1.25.
    # Answers here are extractive over retrieved context, not open-ended writing,
    # so the smallest model is a genuine fit rather than a sacrifice. sqlgen.py
    # validates whatever SQL it writes, so a weak query is rejected, not run.
    openai_chat_model: str = "gpt-4.1-nano"
    # Output bills at 4x input, so this is the knob that actually controls spend.
    # 400 tokens is ~300 words — ample for a grounded answer with citations.
    llm_max_output_tokens: int = 400

    # OCR for uploaded images, via the vision API — no local model, no binary.
    ocr_provider: str = "auto"
    openai_vision_model: str = "gpt-4.1-nano"
    ocr_max_output_tokens: int = 800
    # Tighter than max_upload_bytes: vision is billed per image, and a 20MB photo
    # costs real money for no extra legibility.
    max_image_bytes: int = 6 * 1024 * 1024
    # Every char here is an input token on every single query. 8k chars is ~2k
    # tokens, which still fits the SQL rows, graph edges and top chunks that
    # /query assembles — the retrieval limits below cap what can arrive anyway.
    max_context_chars: int = 8_000

    # Fallback USD per million tokens, used only when a caller prices a call
    # without naming its model. Real pricing lives in llm.MODEL_PRICES, keyed by
    # model, because one global pair silently misprices the audit trail and the
    # spend cap the moment OPENAI_CHAT_MODEL changes. Defaults track gpt-4.1-nano.
    price_input_per_mtok: float = 0.10
    price_output_per_mtok: float = 0.40
    price_embedding_per_mtok: float = 0.02

    # Text-to-SQL. The model writes the query; sqlgen.py validates it and runs
    # it in a Postgres read-only transaction with a statement timeout.
    # Background agents. Disabled in tests so a timer cannot race assertions.
    enable_scheduler: bool = True
    agent_interval_seconds: int = 300
    run_agents_on_startup: bool = True
    # Runs kept per agent. Bounds a table the scheduler appends to forever.
    agent_run_retention: int = 50

    # Comma-separated browser origins allowed to call the API. Explicit list, no
    # wildcard: with credentials enabled a wildcard is rejected by browsers, and
    # without them it still lets any site read authenticated responses.
    cors_origins: str = "http://localhost:3000"

    # If set, /metrics requires `Authorization: Bearer <this>`. Prometheus sends it
    # via bearer_token in its scrape config. Left blank the endpoint is open, which
    # is fine on localhost and not fine anywhere public.
    metrics_token: str = ""

    # Inbound webhooks. Unset means the endpoint refuses everything rather than
    # accepting unsigned writes.
    webhook_secret: str = ""
    http_timeout_seconds: float = 10.0

    # Grain futures. "auto" uses the live free Yahoo endpoint when
    # ENABLE_LIVE_MARKET is on, and the deterministic fake otherwise, so tests
    # and CI never depend on an unofficial third-party API being up.
    market_provider: str = "auto"
    enable_live_market: bool = True

    # FX rates and agricultural news — both free and keyless. Same auto/live/fake
    # selection, so the check suite never touches the network.
    feeds_provider: str = "auto"
    enable_live_feeds: bool = True

    # Celery. Falls back to the in-process scheduler when no broker is set, so
    # the app still runs standalone.
    celery_broker_url: str = ""
    celery_result_backend: str = ""

    enable_text_to_sql: bool = True
    sql_statement_timeout_ms: int = 5000
    sql_max_rows: int = 200

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()


def cors_origin_list() -> list[str]:
    """Parsed, de-blanked origins. A stray comma must not become an empty origin."""
    return [o.strip() for o in settings.cors_origins.split(",") if o.strip()]


if len(settings.jwt_secret) < 32:
    raise RuntimeError(
        "JWT_SECRET must be at least 32 chars. "
        'Generate: python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )
