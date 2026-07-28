/** Typed client for the FastAPI backend.
 *
 * One place that knows about auth headers, error shapes, and the base URL. Errors
 * are normalised into ApiError with the backend's `detail` message, so a UI can
 * show what actually went wrong instead of "something failed".
 */

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function authHeader(token: string | null): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Drop a dead session and send the user back to sign in.
 *
 * A token can stop working for reasons the browser cannot see: it expired, the
 * account was deactivated, or the database was rebuilt under it. Without this,
 * every page renders "Could not validate credentials" over a nav bar that also
 * fails, and the only way out is clearing localStorage by hand.
 *
 * Only called when a token was actually sent — a wrong password on the login
 * form is also a 401, and that must stay on the form with its own message.
 */
function discardDeadSession(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem("agfabric.session");
  if (window.location.pathname !== "/login") {
    window.location.replace("/login?expired=1");
  }
}

async function unwrap<T>(
  response: Response,
  authenticated = false,
): Promise<T> {
  if (response.ok) return (await response.json()) as T;
  if (response.status === 401 && authenticated) discardDeadSession();

  let detail = `${response.status} ${response.statusText}`;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") detail = body.detail;
    else if (Array.isArray(body.detail)) {
      // FastAPI validation errors arrive as a list of objects.
      detail = body.detail
        .map((d: { msg?: string }) => d.msg ?? "invalid")
        .join("; ");
    }
  } catch {
    // Non-JSON error body; the status line is all we have.
  }
  throw new ApiError(response.status, detail);
}

export async function get<T>(path: string, token: string | null): Promise<T> {
  return unwrap<T>(
    await fetch(`${BASE}${path}`, {
      headers: authHeader(token),
      cache: "no-store",
    }),
    token !== null,
  );
}

export async function post<T>(
  path: string,
  token: string | null,
  body?: unknown,
): Promise<T> {
  return unwrap<T>(
    await fetch(`${BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeader(token) },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
    token !== null,
  );
}

export async function login(email: string, password: string) {
  // OAuth2 password flow expects form encoding, not JSON.
  const form = new URLSearchParams({ username: email, password });
  return unwrap<{
    access_token: string;
    user: { email: string; name: string; role: string };
  }>(
    await fetch(`${BASE}/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
    }),
  );
}

export async function uploadDocument(file: File, token: string | null) {
  const form = new FormData();
  form.append("file", file);
  return unwrap<UploadResult>(
    await fetch(`${BASE}/documents/upload`, {
      method: "POST",
      // No Content-Type: the browser must set the multipart boundary itself.
      headers: authHeader(token),
      body: form,
    }),
    token !== null,
  );
}

/* ------------------------------------------------------------------ types */

export type Role = "ops" | "accountant" | "warehouse" | "exec";
export type Severity = "high" | "medium" | "low";

export interface Dashboard {
  storage: {
    capacity_bu: number;
    stored_bu: number;
    utilization_pct: number;
    bins: number;
  };
  deliveries: { last_7_days: number; unverified: number };
  contracts: { open: number; expiring_30d: number };
  recent_events: {
    ticket: string;
    customer: string;
    truck_id: string;
    net_bu: number;
    moisture_pct: number;
    delivered_at: string;
    verified: boolean;
  }[];
  open_alerts: number;
  alerts_by_severity: Partial<Record<Severity, number>>;
  deliveries_daily: { date: string; count: number }[];
  financial_summary?: Record<string, { amount: number; count: number }>;
}

export interface Storage {
  bins: {
    name: string;
    facility: string;
    commodity: string | null;
    capacity_bu: number;
    current_bu: number;
    moisture_pct: number | null;
  }[];
}

export interface Alert {
  id: number;
  kind: string;
  severity: Severity;
  title: string;
  confidence: number;
  evidence: Record<string, unknown>;
  recommendation: string;
  status: "open" | "acknowledged" | "resolved";
  first_seen_at: string;
  last_seen_at: string;
  resolved_at: string | null;
}

export interface DocumentRow {
  id: number;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  version: number;
  text_chars: number;
  chunk_count: number;
  status: string;
  note: string | null;
  uploaded_at: string;
}

export interface UploadResult {
  duplicate: boolean;
  document: DocumentRow;
  chunks_indexed?: number;
  index_error?: string | null;
  message?: string;
}

export interface ChunkHit {
  score: number;
  text: string;
  source: {
    document_id: number | null;
    filename: string | null;
    sha256: string | null;
    chunk_ordinal: number | null;
    chunk_id: number;
  };
}

export interface QueryResult {
  question: string;
  answer: string;
  confidence: number;
  explanation: {
    sql_evidence: Record<string, unknown>[];
    generated_sql: {
      attempted: boolean;
      sql: string | null;
      rejected: string | null;
      error: string | null;
      columns: string[];
      rows: Record<string, unknown>[];
      row_count: number;
      truncated: boolean;
    };
    graph_relationships: {
      source: string;
      source_id: string;
      relationship: string;
      target: string;
      target_id: string;
    }[];
    retrieved_chunks: ChunkHit[];
    resolved: {
      identifiers: Record<string, string[]>;
      customers: { id: number; name: string }[];
    };
    financials_visible: boolean;
    retrieval_error: string | null;
  };
  model: {
    provider: string;
    chat_model: string;
    embedding_model: string;
    input_tokens: number;
    output_tokens: number;
    cost_usd: number;
    llm_calls: number;
  };
  took_ms: number;
}

export interface GraphSummary {
  node_count: number;
  edge_count: number;
  nodes_by_kind: Record<string, number>;
  edges_by_label: Record<string, number>;
}

export interface GraphNode {
  id: string;
  kind: string;
  label: string;
  hops: number;
  subtype?: string;
  status?: string;
  location?: string;
  truck_id?: string;
}

export interface GraphExpansion {
  root: string;
  depth: number;
  nodes: GraphNode[];
  edges: { source: string; target: string; label: string }[];
}

export interface AgentRun {
  id: number;
  agent: string;
  status: "ok" | "failed";
  trigger: string;
  started_at: string;
  duration_ms: number;
  items: number;
  detail: Record<string, unknown> | null;
  error: string | null;
}

export interface AgentInfo {
  name: string;
  description: string;
  scheduled: boolean;
  last_run: AgentRun | null;
}

export interface AuditEntry {
  id: number;
  request_id: string;
  user: string;
  role: string;
  endpoint: string;
  question: string;
  confidence: number | null;
  provider: string | null;
  chat_model: string | null;
  record_count: number;
  chunk_count: number;
  graph_edge_count: number;
  took_ms: number;
  created_at: string;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
}

export interface AuditSummary {
  since_hours: number;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  avg_took_ms: number;
  by_user: { user: string; requests: number; cost_usd: number }[];
  by_endpoint: { endpoint: string; requests: number }[];
}

export interface WeatherDay {
  date: string;
  temp_max_c: number | null;
  temp_min_c: number | null;
  precipitation_mm: number | null;
  wind_max_kmh: number | null;
  humidity_pct: number | null;
}

export interface Weather {
  source: string;
  fetched_at: string | null;
  facilities: { facility: string; forecast: WeatherDay[] }[];
}

export interface MarketPosition {
  commodity: string;
  commodity_id: number;
  market_usd_per_bu: number | null;
  long_bu: number;
  short_bu: number;
  net_bu: number;
  direction: "long" | "short" | "flat";
  open_contracts: number;
  unrealised_usd?: number;
}

export interface Market {
  source: string;
  fetched_at: string | null;
  financials_visible: boolean;
  board: {
    commodity: string;
    symbol: string | null;
    close_usd_per_bu: number | null;
  }[];
  history: { symbol: string; date: string; close_usd_per_bu: number }[];
  positions: MarketPosition[];
  contracts: {
    number: string;
    commodity: string;
    customer: string;
    side: string;
    remaining_bu: number;
    price_per_bu: number;
    market_usd_per_bu: number;
    basis_usd_per_bu: number;
    unrealised_usd: number;
  }[];
  totals: { unrealised_usd: number | null; open_contracts: number | null };
}

export interface FxRate {
  currency: string;
  note: string;
  rate: number;
  quoted_on: string;
  change_pct: number | null;
  direction: "up" | "down" | "flat";
}

export interface Feeds {
  fx: { base: string; rates: FxRate[] };
  news: {
    title: string;
    url: string;
    publisher: string | null;
    published_at: string | null;
  }[];
}

export interface Health {
  status: "ok" | "degraded" | "unhealthy";
  checks: Record<string, string>;
  degraded: string[];
}
