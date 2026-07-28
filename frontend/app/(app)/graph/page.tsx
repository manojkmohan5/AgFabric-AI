"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { GraphView } from "@/components/graph-view";
import { PageHeader } from "@/components/shell";
import {
  Button,
  Card,
  ErrorNote,
  Loading,
  Stat,
  inputClass,
} from "@/components/ui";
import { type GraphExpansion, type GraphSummary, get } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function GraphPage() {
  const token = useAuth((s) => s.session?.token ?? null);
  const [nodeId, setNodeId] = useState("customer:1");
  const [depth, setDepth] = useState(2);
  const [query, setQuery] = useState({ nodeId: "customer:1", depth: 2 });

  const summary = useQuery({
    queryKey: ["graph-summary"],
    queryFn: () => get<GraphSummary>("/graph", token),
    enabled: Boolean(token),
  });

  const expansion = useQuery({
    queryKey: ["graph", query.nodeId, query.depth],
    queryFn: () =>
      get<GraphExpansion>(
        `/graph/entity/${encodeURIComponent(query.nodeId)}?depth=${query.depth}`,
        token,
      ),
    enabled: Boolean(token),
  });

  return (
    <>
      <PageHeader
        title="Knowledge Graph"
        description="Derived from PostgreSQL foreign keys — no graph database. Rows are nodes, foreign keys are edges, and traversal follows them in both directions."
      />

      {summary.data && (
        <dl className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat label="Entities" value={summary.data.node_count} />
          <Stat label="Relationships" value={summary.data.edge_count} />
          <Stat
            label="Entity types"
            value={Object.keys(summary.data.nodes_by_kind).length}
          />
          <Stat
            label="Relationship types"
            value={Object.keys(summary.data.edges_by_label).length}
          />
        </dl>
      )}

      <Card title="Explore" className="mb-4">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setQuery({ nodeId: nodeId.trim(), depth });
          }}
          className="flex flex-wrap items-end gap-3"
        >
          <div className="min-w-0 flex-1">
            <label htmlFor="node" className="mb-1.5 block text-sm font-medium">
              Entity
            </label>
            <p id="node-hint" className="mb-1.5 text-xs text-[var(--text-muted)]">
              Format is <code>kind:id</code> — for example{" "}
              <code>customer:1</code>, <code>contract:1</code>, or{" "}
              <code>invoice:3</code>.
            </p>
            <input
              id="node"
              name="node"
              type="text"
              value={nodeId}
              onChange={(e) => setNodeId(e.target.value)}
              aria-describedby="node-hint"
              className={inputClass}
            />
          </div>

          <div>
            <label htmlFor="depth" className="mb-1.5 block text-sm font-medium">
              Depth
            </label>
            <select
              id="depth"
              name="depth"
              value={depth}
              onChange={(e) => setDepth(Number(e.target.value))}
              className={`${inputClass} w-28`}
            >
              <option value={0}>0 hops</option>
              <option value={1}>1 hop</option>
              <option value={2}>2 hops</option>
              <option value={3}>3 hops</option>
            </select>
          </div>

          <Button type="submit">Explore</Button>
        </form>

        {summary.data && (
          <div className="mt-4 flex flex-wrap gap-2 border-t border-[var(--border)] pt-3.5">
            <span className="text-xs text-[var(--text-muted)]">Jump to:</span>
            {Object.keys(summary.data.nodes_by_kind)
              .sort()
              .map((kind) => (
                <button
                  key={kind}
                  type="button"
                  onClick={() => {
                    setNodeId(`${kind}:1`);
                    setQuery({ nodeId: `${kind}:1`, depth });
                  }}
                  className="rounded-full border border-[var(--border)] px-2.5 py-1 text-xs capitalize text-[var(--text-muted)] hover:border-[var(--border-strong)] hover:text-[var(--text)]"
                >
                  {kind}
                </button>
              ))}
          </div>
        )}
      </Card>

      <Card
        title={
          expansion.data
            ? `${expansion.data.root} — ${expansion.data.nodes.length} entities, ${expansion.data.edges.length} relationships`
            : "Traversal"
        }
      >
        <div aria-live="polite">
          {expansion.isLoading && <Loading label="Walking the graph" />}
          {expansion.error && (
            <ErrorNote message={(expansion.error as Error).message} />
          )}
          {expansion.data && (
            <GraphView
              data={expansion.data}
              onRecentre={(id) => {
                setNodeId(id);
                setQuery({ nodeId: id, depth });
              }}
            />
          )}
        </div>
      </Card>
    </>
  );
}
