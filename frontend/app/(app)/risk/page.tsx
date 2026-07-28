"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { PageHeader } from "@/components/shell";
import {
  Badge,
  Button,
  Card,
  Confidence,
  Empty,
  ErrorNote,
  Loading,
  SeverityBadge,
} from "@/components/ui";
import { type Alert, get, post } from "@/lib/api";
import { canRunAgents, useAuth } from "@/lib/auth";

const STATUSES = ["open", "acknowledged", "resolved", "all"] as const;
type StatusFilter = (typeof STATUSES)[number];

const KIND_LABELS: Record<string, string> = {
  duplicate_invoice: "Duplicate invoice",
  inventory_mismatch: "Inventory mismatch",
  moisture_anomaly: "Moisture anomaly",
  contract_expiration: "Contract expiring",
  missing_deliveries: "Missing deliveries",
  data_inconsistency: "Data inconsistency",
};

export default function RiskPage() {
  const session = useAuth((s) => s.session);
  const token = session?.token ?? null;
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<StatusFilter>("open");

  const alerts = useQuery({
    queryKey: ["alerts", status],
    queryFn: () =>
      get<{ count: number; open_total: number; alerts: Alert[] }>(
        `/alerts?status=${status}`,
        token,
      ),
    enabled: Boolean(token),
    // The risk agent rescans on its own schedule, so poll to pick up new findings
    // without the operator needing to reload.
    refetchInterval: 20_000,
  });

  const act = useMutation({
    mutationFn: ({ id, action }: { id: number; action: "acknowledge" | "resolve" }) =>
      post(`/alerts/${id}/${action}`, token),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const rescan = useMutation({
    mutationFn: () => post("/agents/risk/run", token),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  return (
    <>
      <PageHeader
        title="Risk Center"
        description="Automatically detected operational and financial risk. Every finding carries evidence, a confidence score and a recommended action."
        action={
          canRunAgents(session?.role) && (
            <Button
              variant="secondary"
              onClick={() => rescan.mutate()}
              disabled={rescan.isPending}
            >
              {rescan.isPending ? "Scanning…" : "Run scan now"}
            </Button>
          )
        }
      />

      {/* Radio group, not buttons: these are mutually exclusive states, and
          radios give arrow-key navigation for free. */}
      <fieldset className="mb-4">
        <legend className="sr-only">Filter alerts by status</legend>
        <div className="flex flex-wrap gap-2">
          {STATUSES.map((option) => (
            <label
              key={option}
              className="cursor-pointer rounded-full border px-3 py-1.5 text-xs font-medium capitalize"
              style={
                status === option
                  ? {
                      borderColor: "var(--accent)",
                      background: "var(--accent-soft)",
                      color: "var(--accent-text)",
                    }
                  : {
                      borderColor: "var(--border)",
                      color: "var(--text-muted)",
                    }
              }
            >
              <input
                type="radio"
                name="status"
                value={option}
                checked={status === option}
                onChange={() => setStatus(option)}
                className="sr-only"
              />
              {option}
            </label>
          ))}
        </div>
      </fieldset>

      <div aria-live="polite">
        {alerts.isLoading && <Loading label="Loading alerts" />}
        {alerts.error && <ErrorNote message={(alerts.error as Error).message} />}
        {act.error && <ErrorNote message={(act.error as Error).message} />}

        {alerts.data && alerts.data.alerts.length === 0 && (
          <Empty>
            {status === "open"
              ? "No open alerts. Everything the rules check is currently clean."
              : `No ${status} alerts.`}
          </Empty>
        )}

        <ul className="space-y-3">
          {alerts.data?.alerts.map((alert) => (
            <li key={alert.id}>
              <article className="rounded-[var(--radius)] border border-[var(--border)] bg-[var(--surface)] p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="mb-1.5 flex flex-wrap items-center gap-2">
                      <SeverityBadge severity={alert.severity} />
                      <Badge tone="neutral">
                        {KIND_LABELS[alert.kind] ?? alert.kind}
                      </Badge>
                      {alert.status !== "open" && (
                        <Badge tone={alert.status === "resolved" ? "accent" : "info"}>
                          {alert.status}
                        </Badge>
                      )}
                    </div>
                    <h2 className="text-sm font-semibold">{alert.title}</h2>
                  </div>
                  <Confidence value={alert.confidence} />
                </div>

                <p className="mt-2.5 text-sm text-[var(--text-muted)]">
                  {alert.recommendation}
                </p>

                <details className="mt-3 group">
                  <summary className="cursor-pointer rounded-md text-xs font-medium text-[var(--accent-text)]">
                    Evidence
                  </summary>
                  <dl className="mt-2 grid gap-1.5 rounded-lg bg-[var(--surface-2)] p-3 text-xs">
                    {Object.entries(alert.evidence).map(([key, value]) => (
                      <div key={key} className="flex gap-3">
                        <dt className="w-36 shrink-0 text-[var(--text-faint)]">
                          {key.replace(/_/g, " ")}
                        </dt>
                        <dd className="tabular min-w-0 break-words">
                          {Array.isArray(value) ? value.join(", ") : String(value)}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </details>

                <footer className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] pt-3">
                  <p className="text-xs text-[var(--text-faint)]">
                    First seen{" "}
                    <time dateTime={alert.first_seen_at}>
                      {new Date(alert.first_seen_at).toLocaleString()}
                    </time>
                  </p>
                  {alert.status !== "resolved" && (
                    <div className="flex gap-2">
                      {alert.status === "open" && (
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() =>
                            act.mutate({ id: alert.id, action: "acknowledge" })
                          }
                          disabled={act.isPending}
                        >
                          Acknowledge
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => act.mutate({ id: alert.id, action: "resolve" })}
                        disabled={act.isPending}
                      >
                        Resolve
                      </Button>
                    </div>
                  )}
                </footer>
              </article>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}
