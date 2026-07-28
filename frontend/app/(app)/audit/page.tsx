"use client";

import { useQuery } from "@tanstack/react-query";

import { PageHeader } from "@/components/shell";
import {
  Badge,
  Card,
  Empty,
  ErrorNote,
  Loading,
  Stat,
  Table,
  Td,
} from "@/components/ui";
import { type AuditEntry, type AuditSummary, get } from "@/lib/api";
import { canSeeMoney, useAuth } from "@/lib/auth";

export default function AuditPage() {
  const session = useAuth((s) => s.session);
  const token = session?.token ?? null;
  const showCost = canSeeMoney(session?.role);

  const entries = useQuery({
    queryKey: ["audit"],
    queryFn: () =>
      get<{ count: number; scoped_to_self: boolean; entries: AuditEntry[] }>(
        "/audit?limit=50",
        token,
      ),
    enabled: Boolean(token),
  });

  const summary = useQuery({
    queryKey: ["audit-summary"],
    queryFn: () => get<AuditSummary>("/audit/summary?since_hours=24", token),
    // Finance-only endpoint; do not fire a request that is known to 403.
    enabled: Boolean(token) && showCost,
  });

  return (
    <>
      <PageHeader
        title="Audit Center"
        description="Every AI request recorded: who asked, what was retrieved, which model answered, and what it cost."
      />

      {summary.data && (
        <dl className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat
            label="Requests (24h)"
            value={summary.data.requests}
            hint={`avg ${summary.data.avg_took_ms.toFixed(0)} ms`}
          />
          <Stat
            label="Input tokens"
            value={summary.data.input_tokens.toLocaleString()}
          />
          <Stat
            label="Output tokens"
            value={summary.data.output_tokens.toLocaleString()}
          />
          <Stat
            label="Spend (24h)"
            value={`$${summary.data.cost_usd.toFixed(4)}`}
            hint={
              summary.data.cost_usd === 0
                ? "Local fake provider — no billing"
                : undefined
            }
            tone={summary.data.cost_usd > 1 ? "warn" : "accent"}
          />
        </dl>
      )}

      {entries.data?.scoped_to_self && (
        <p className="mb-4 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2.5 text-sm text-[var(--text-muted)]">
          Showing only your own requests. Full audit access and cost figures are
          limited to accountant and executive roles.
        </p>
      )}

      <Card title="Request log">
        <div aria-live="polite">
          {entries.isLoading && <Loading label="Loading audit log" />}
          {entries.error && <ErrorNote message={(entries.error as Error).message} />}
          {entries.data && entries.data.entries.length === 0 && (
            <Empty>
              Nothing logged yet. Ask something on the AI Search page and it will
              appear here.
            </Empty>
          )}
        </div>

        {entries.data && entries.data.entries.length > 0 && (
          <Table
            caption="Recorded AI requests with the user, evidence counts, model, latency and cost"
            headers={
              showCost
                ? ["When", "User", "Question", "Evidence", "Model", "Tokens", "Cost", "Time"]
                : ["When", "User", "Question", "Evidence", "Model", "Time"]
            }
          >
            {entries.data.entries.map((entry) => (
              <tr key={entry.id}>
                <Td className="whitespace-nowrap text-xs text-[var(--text-muted)]">
                  <time dateTime={entry.created_at}>
                    {new Date(entry.created_at).toLocaleTimeString()}
                  </time>
                </Td>
                <Td className="text-xs">
                  {entry.user}
                  <span className="block capitalize text-[var(--text-faint)]">
                    {entry.role}
                  </span>
                </Td>
                <Td className="max-w-xs">
                  <span className="line-clamp-2 text-xs">{entry.question}</span>
                  <span className="tabular mt-0.5 block text-[0.65rem] text-[var(--text-faint)]">
                    {entry.request_id.slice(0, 8)}…
                  </span>
                </Td>
                <Td className="text-xs text-[var(--text-muted)]">
                  <span className="tabular">
                    {entry.record_count} rec · {entry.chunk_count} chunk ·{" "}
                    {entry.graph_edge_count} edge
                  </span>
                </Td>
                <Td className="text-xs">
                  <Badge tone="neutral">{entry.provider ?? "—"}</Badge>
                </Td>
                {showCost && (
                  <Td className="tabular text-xs">
                    {entry.input_tokens ?? 0}/{entry.output_tokens ?? 0}
                  </Td>
                )}
                {showCost && (
                  <Td className="tabular text-xs">
                    ${(entry.cost_usd ?? 0).toFixed(6)}
                  </Td>
                )}
                <Td className="tabular text-xs">{entry.took_ms.toFixed(0)} ms</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      {summary.data && summary.data.by_user.length > 0 && (
        <Card title="Spend by user (24h)" className="mt-4">
          <Table
            caption="Requests and cost grouped by user over the last 24 hours"
            headers={["User", "Requests", "Cost"]}
          >
            {summary.data.by_user.map((row) => (
              <tr key={row.user}>
                <Td>{row.user}</Td>
                <Td className="tabular">{row.requests}</Td>
                <Td className="tabular">${row.cost_usd.toFixed(6)}</Td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </>
  );
}
