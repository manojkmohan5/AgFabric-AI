"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { PageHeader } from "@/components/shell";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Loading,
  Table,
  Td,
} from "@/components/ui";
import { type AgentInfo, type AgentRun, get, post } from "@/lib/api";
import { canRunAgents, useAuth } from "@/lib/auth";

export default function AgentsPage() {
  const session = useAuth((s) => s.session);
  const token = session?.token ?? null;
  const queryClient = useQueryClient();
  const mayRun = canRunAgents(session?.role);

  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: () => get<{ agents: AgentInfo[] }>("/agents", token),
    enabled: Boolean(token),
  });

  const runs = useQuery({
    queryKey: ["agent-runs"],
    queryFn: () => get<{ runs: AgentRun[] }>("/agents/runs?limit=25", token),
    enabled: Boolean(token),
  });

  const run = useMutation({
    mutationFn: (name: string) => post<AgentRun>(`/agents/${name}/run`, token),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      queryClient.invalidateQueries({ queryKey: ["agent-runs"] });
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  return (
    <>
      <PageHeader
        title="AI Agent Center"
        description="Background jobs and the record of them running. Each agent is callable on demand, on a schedule, or from a Celery worker."
      />

      {!mayRun && (
        <p className="mb-4 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2.5 text-sm text-[var(--text-muted)]">
          Running agents requires the operations or executive role. You can see
          status and history.
        </p>
      )}

      <div aria-live="polite">
        {agents.isLoading && <Loading label="Loading agents" />}
        {agents.error && <ErrorNote message={(agents.error as Error).message} />}
        {run.error && <ErrorNote message={(run.error as Error).message} />}
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        {agents.data?.agents.map((agent) => (
          <article
            key={agent.name}
            className="rounded-[var(--radius)] border border-[var(--border)] bg-[var(--surface)] p-4"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-sm font-semibold capitalize">
                  {agent.name.replace(/_/g, " ")}
                </h2>
                <p className="mt-1 text-xs text-[var(--text-muted)]">
                  {agent.description}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {agent.scheduled && <Badge tone="info">Scheduled</Badge>}
                {agent.last_run ? (
                  <Badge tone={agent.last_run.status === "ok" ? "accent" : "danger"}>
                    {agent.last_run.status === "ok" ? "Healthy" : "Failed"}
                  </Badge>
                ) : (
                  <Badge tone="neutral">Never run</Badge>
                )}
              </div>
            </div>

            {agent.last_run && (
              <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 border-t border-[var(--border)] pt-3 text-xs">
                <div className="flex gap-1.5">
                  <dt className="text-[var(--text-faint)]">Last run</dt>
                  <dd className="tabular">
                    <time dateTime={agent.last_run.started_at}>
                      {new Date(agent.last_run.started_at).toLocaleTimeString()}
                    </time>
                  </dd>
                </div>
                <div className="flex gap-1.5">
                  <dt className="text-[var(--text-faint)]">Duration</dt>
                  <dd className="tabular">{agent.last_run.duration_ms} ms</dd>
                </div>
                <div className="flex gap-1.5">
                  <dt className="text-[var(--text-faint)]">Items</dt>
                  <dd className="tabular">{agent.last_run.items}</dd>
                </div>
                <div className="flex gap-1.5">
                  <dt className="text-[var(--text-faint)]">Trigger</dt>
                  <dd>{agent.last_run.trigger}</dd>
                </div>
              </dl>
            )}

            {agent.last_run?.error && (
              <p className="mt-2 text-xs" style={{ color: "var(--danger)" }}>
                Error: {agent.last_run.error}
              </p>
            )}

            {mayRun && (
              <div className="mt-3">
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => run.mutate(agent.name)}
                  disabled={run.isPending}
                >
                  {run.isPending && run.variables === agent.name
                    ? "Running…"
                    : "Run now"}
                </Button>
              </div>
            )}
          </article>
        ))}
      </div>

      <Card title="Run history" className="mt-4">
        {runs.isLoading && <Loading label="Loading history" />}
        {runs.data && runs.data.runs.length === 0 && (
          <Empty>No runs recorded yet.</Empty>
        )}
        {runs.data && runs.data.runs.length > 0 && (
          <Table
            caption="Recent agent executions with outcome, trigger, duration and item count"
            headers={["Agent", "Outcome", "Trigger", "Duration", "Items", "Started"]}
          >
            {runs.data.runs.map((r) => (
              <tr key={r.id}>
                <Td className="font-medium capitalize">
                  {r.agent.replace(/_/g, " ")}
                </Td>
                <Td>
                  <Badge tone={r.status === "ok" ? "accent" : "danger"}>
                    {r.status === "ok" ? "OK" : "Failed"}
                  </Badge>
                </Td>
                <Td className="text-[var(--text-muted)]">{r.trigger}</Td>
                <Td className="tabular">{r.duration_ms} ms</Td>
                <Td className="tabular">{r.items}</Td>
                <Td className="text-xs text-[var(--text-muted)]">
                  <time dateTime={r.started_at}>
                    {new Date(r.started_at).toLocaleString()}
                  </time>
                </Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
