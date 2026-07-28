"use client";

import { useMutation } from "@tanstack/react-query";
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
  Table,
  Td,
  inputClass,
} from "@/components/ui";
import { type QueryResult, post } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const EXAMPLES = [
  "What is the status of contract C-2026-1000?",
  "What is the total invoice amount by status?",
  "Which customer delivered the most bushels?",
  "What contracts does Halvorsen have?",
  // ANOM-01 is the bin seeded above the 15% moisture ceiling, so this example
  // always returns a real reading and a matching risk finding.
  "Bin ANOM-01 moisture reading",
];

export default function SearchPage() {
  const token = useAuth((s) => s.session?.token ?? null);
  const [question, setQuestion] = useState("");

  const ask = useMutation({
    mutationFn: (q: string) =>
      post<QueryResult>("/query", token, { question: q }),
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = question.trim();
    if (trimmed) ask.mutate(trimmed);
  }

  const result = ask.data;

  return (
    <>
      <PageHeader
        title="Hybrid AI Search"
        description="Every answer is grounded in database records, document excerpts and knowledge-graph relationships — and shows all three."
      />

      <form onSubmit={submit} className="mb-4">
        <label htmlFor="question" className="mb-1.5 block text-sm font-medium">
          Ask a question
        </label>
        <div className="flex flex-wrap gap-2">
          <input
            id="question"
            name="question"
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. What is the status of contract C-2026-1000?"
            maxLength={1000}
            className={`${inputClass} min-w-0 flex-1`}
          />
          <Button type="submit" disabled={ask.isPending || !question.trim()}>
            {ask.isPending ? "Thinking…" : "Ask"}
          </Button>
        </div>
      </form>

      <div className="mb-6 flex flex-wrap items-center gap-2">
        <span className="text-xs text-[var(--text-muted)]">Try:</span>
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => {
              setQuestion(example);
              ask.mutate(example);
            }}
            className="rounded-full border border-[var(--border)] px-2.5 py-1 text-xs text-[var(--text-muted)] hover:border-[var(--border-strong)] hover:text-[var(--text)]"
          >
            {example}
          </button>
        ))}
      </div>

      {/* Results announce themselves when they arrive. */}
      <div aria-live="polite" aria-atomic="false">
        {ask.isPending && <Loading label="Retrieving and generating" />}
        {ask.error && <ErrorNote message={(ask.error as Error).message} />}

        {result && (
          <div className="space-y-4">
            <Card title="Answer">
              <p className="whitespace-pre-wrap text-sm leading-relaxed">
                {result.answer}
              </p>

              <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-[var(--border)] pt-3.5 text-xs text-[var(--text-muted)]">
                <div className="flex items-center gap-2">
                  <span>Confidence</span>
                  <Confidence value={result.confidence} />
                </div>
                <span className="tabular">
                  {result.model.provider} / {result.model.chat_model}
                </span>
                <span className="tabular">
                  {result.model.input_tokens}&nbsp;in ·{" "}
                  {result.model.output_tokens}&nbsp;out
                </span>
                <span className="tabular">
                  ${result.model.cost_usd.toFixed(6)}
                </span>
                <span className="tabular">
                  {result.took_ms.toFixed(0)}&nbsp;ms
                </span>
                {!result.explanation.financials_visible && (
                  <Badge tone="warn">
                    Financial values redacted for your role
                  </Badge>
                )}
              </div>
            </Card>

            {result.explanation.generated_sql.attempted && (
              <Card title="Generated SQL">
                {result.explanation.generated_sql.sql ? (
                  <>
                    <pre className="overflow-x-auto rounded-lg bg-[var(--surface-2)] p-3 text-xs leading-relaxed">
                      <code>{result.explanation.generated_sql.sql}</code>
                    </pre>
                    <p className="mt-2 text-xs text-[var(--text-muted)]">
                      Passed the validation gate and ran in a read-only
                      transaction. {result.explanation.generated_sql.row_count}{" "}
                      row
                      {result.explanation.generated_sql.row_count === 1
                        ? ""
                        : "s"}{" "}
                      returned.
                    </p>
                    {result.explanation.generated_sql.rows.length > 0 && (
                      <div className="mt-3">
                        <Table
                          caption="Rows returned by the generated SQL query"
                          headers={result.explanation.generated_sql.columns}
                        >
                          {result.explanation.generated_sql.rows
                            .slice(0, 12)
                            .map((row, i) => (
                              <tr key={i}>
                                {result.explanation.generated_sql.columns.map(
                                  (col) => (
                                    <Td key={col} className="tabular">
                                      {String(row[col] ?? "—")}
                                    </Td>
                                  ),
                                )}
                              </tr>
                            ))}
                        </Table>
                      </div>
                    )}
                  </>
                ) : (
                  <p className="text-xs text-[var(--text-muted)]">
                    No query ran.{" "}
                    <span className="font-medium">
                      {result.explanation.generated_sql.rejected ??
                        result.explanation.generated_sql.error ??
                        "Nothing was generated."}
                    </span>
                  </p>
                )}
              </Card>
            )}

            <div className="grid gap-4 lg:grid-cols-2">
              {/* Named-entity lookups, NOT "everything the database returned".
                  This panel is the deterministic path: resolve.py pulls
                  identifiers out of the question (contract numbers, ticket
                  numbers, bin names, customer names) and _fetch_records loads
                  exactly those rows — no LLM involved. An aggregate question
                  like "which customer delivered the most" names no entity, so
                  this is empty while the generated SQL above still answers it
                  from the same database. Titled "Database records (0)" it read
                  as "the database had nothing", which is the opposite of what
                  happened. */}
              <Card
                title={`Named-entity lookups (${result.explanation.sql_evidence.length})`}
              >
                {result.explanation.sql_evidence.length === 0 ? (
                  <Empty>
                    This question did not name a specific contract, ticket, bin
                    or invoice, so there was nothing to look up directly. The
                    answer came from the generated SQL above, which queries the
                    same database.
                  </Empty>
                ) : (
                  <ul className="space-y-3">
                    {result.explanation.sql_evidence.map((record, i) => (
                      <li
                        key={i}
                        className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-3"
                      >
                        <Badge tone="info">{String(record.kind)}</Badge>
                        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                          {Object.entries(record)
                            .filter(([key]) => key !== "kind")
                            .map(([key, value]) => (
                              <div key={key} className="col-span-2 flex gap-3">
                                <dt className="w-28 shrink-0 text-[var(--text-faint)]">
                                  {key.replace(/_/g, " ")}
                                </dt>
                                <dd className="tabular min-w-0 break-words">
                                  {value === "redacted" ? (
                                    <span style={{ color: "var(--warn)" }}>
                                      redacted
                                    </span>
                                  ) : (
                                    String(value ?? "—")
                                  )}
                                </dd>
                              </div>
                            ))}
                        </dl>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card
                title={`Document sources (${result.explanation.retrieved_chunks.length})`}
              >
                {result.explanation.retrieved_chunks.length === 0 ? (
                  <Empty>
                    No documents matched. Upload some to enable this.
                  </Empty>
                ) : (
                  <ul className="space-y-3">
                    {result.explanation.retrieved_chunks.map((hit) => (
                      <li
                        key={hit.source.chunk_id}
                        className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-3"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="text-xs font-semibold">
                            {hit.source.filename}
                          </span>
                          <span className="tabular text-xs text-[var(--text-faint)]">
                            chunk {hit.source.chunk_ordinal} · score{" "}
                            {hit.score.toFixed(3)}
                          </span>
                        </div>
                        <p className="mt-1.5 line-clamp-3 text-xs leading-relaxed text-[var(--text-muted)]">
                          {hit.text}
                        </p>
                        <p className="tabular mt-1.5 text-[0.65rem] text-[var(--text-faint)]">
                          sha256 {hit.source.sha256?.slice(0, 16)}…
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </div>

            {result.explanation.graph_relationships.length > 0 && (
              <Card
                title={`Related entities (${result.explanation.graph_relationships.length})`}
              >
                <ul className="flex flex-wrap gap-2">
                  {result.explanation.graph_relationships
                    .slice(0, 40)
                    .map((triple, i) => (
                      <li
                        key={i}
                        className="rounded-lg border border-[var(--border)] px-2.5 py-1.5 text-xs"
                      >
                        <span className="font-medium">{triple.source}</span>
                        <span
                          className="mx-1.5 text-[var(--text-faint)]"
                          aria-label={`relationship ${triple.relationship}`}
                        >
                          {triple.relationship.toLowerCase().replace(/_/g, " ")}
                        </span>
                        <span className="font-medium">{triple.target}</span>
                      </li>
                    ))}
                </ul>
              </Card>
            )}
          </div>
        )}
      </div>
    </>
  );
}
