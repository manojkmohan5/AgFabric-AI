"use client";

import { useQuery } from "@tanstack/react-query";
import { motion, useReducedMotion } from "motion/react";
import Link from "next/link";

import {
  BarChart,
  Hero,
  Sparkline,
  TrendChart,
  VizRoot,
  type BarDatum,
} from "@/components/charts";
import { BoardStrip } from "@/components/board-strip";
import { FxRibbon } from "@/components/fx-ribbon";
import { PageHeader } from "@/components/shell";
import {
  Badge,
  Card,
  Empty,
  ErrorNote,
  Loading,
  Table,
  Td,
} from "@/components/ui";
import {
  CONDITION_LABEL,
  CONDITION_NOTE,
  WeatherIcon,
  conditionFor,
} from "@/components/weather-icon";
import {
  type Dashboard,
  type Feeds,
  type Health,
  type Market,
  type Storage,
  type Weather,
  get,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});
const num = new Intl.NumberFormat("en-US");

/** Cards fade+rise in sequence. Subtle and once-only — a dashboard that
 *  re-animates on every poll would be unusable. */
function Reveal({
  children,
  delay = 0,
  className,
}: {
  children: React.ReactNode;
  delay?: number;
  /** Pass "h-full" when this sits in a grid row whose card must fill the
   *  height. The motion.div is the grid item, so without it the item stretches
   *  while the Card inside keeps its natural height — which is exactly what left
   *  dead space under the short card in every two-column row. */
  className?: string;
}) {
  const reduce = useReducedMotion();
  return (
    <motion.div
      className={className}
      initial={reduce ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, delay, ease: "easeOut" }}
    >
      {children}
    </motion.div>
  );
}

export default function DashboardPage() {
  const session = useAuth((s) => s.session);
  const token = session?.token ?? null;

  const dash = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => get<Dashboard>("/dashboard", token),
    enabled: Boolean(token),
    refetchInterval: 20_000,
  });

  const storage = useQuery({
    queryKey: ["storage"],
    queryFn: () => get<Storage>("/storage", token),
    enabled: Boolean(token),
    refetchInterval: 60_000,
  });

  const weather = useQuery({
    queryKey: ["weather"],
    queryFn: () => get<Weather>("/weather", token),
    enabled: Boolean(token),
    refetchInterval: 300_000,
  });

  const marketQ = useQuery({
    queryKey: ["market"],
    queryFn: () => get<Market>("/market", token),
    enabled: Boolean(token),
    // The board only settles daily; the agent syncs hourly.
    refetchInterval: 300_000,
  });

  const feedsQ = useQuery({
    queryKey: ["feeds"],
    queryFn: () => get<Feeds>("/feeds", token),
    enabled: Boolean(token),
    // FX settles daily and the news feed is polled hourly by the agent.
    refetchInterval: 300_000,
  });

  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => get<Health>("/health", token),
    enabled: Boolean(token),
    refetchInterval: 30_000,
  });

  if (dash.isLoading) return <Loading label="Loading dashboard" />;
  if (dash.error) return <ErrorNote message={(dash.error as Error).message} />;
  const d = dash.data!;

  const utilisation = d.storage.utilization_pct;
  const financials = d.financial_summary;
  const daily = d.deliveries_daily ?? [];
  const severities = d.alerts_by_severity ?? {};

  // Bins: magnitude across nominal categories → one hue for every bar. Over
  // capacity is a status, so it rides an icon + label, not a second fill colour.
  const binBars: BarDatum[] =
    storage.data?.bins.map((b) => {
      const pct = b.capacity_bu > 0 ? (b.current_bu / b.capacity_bu) * 100 : 0;
      return {
        label: b.name,
        value: b.current_bu,
        hint: `${b.facility}${b.commodity ? ` · ${b.commodity}` : ""} · ${pct.toFixed(0)}% of ${num.format(b.capacity_bu)} bu`,
        status:
          b.current_bu > b.capacity_bu
            ? { tone: "critical" as const, note: "Over capacity" }
            : b.moisture_pct !== null && b.moisture_pct > 15
              ? { tone: "warn" as const, note: `Moisture ${b.moisture_pct}%` }
              : undefined,
      };
    }) ?? [];

  const financeBars: BarDatum[] = financials
    ? Object.entries(financials).map(([status, v]) => ({
        label: status.charAt(0).toUpperCase() + status.slice(1),
        value: v.amount,
        hint: `${v.count} invoice${v.count === 1 ? "" : "s"}`,
        status:
          status === "overdue"
            ? { tone: "critical" as const, note: "Past due" }
            : undefined,
      }))
    : [];

  return (
    <VizRoot>
      <PageHeader
        title="Operational Health"
        description="Live position across storage, deliveries, contracts and detected risk."
        action={
          health.data && (
            <Badge tone={health.data.status === "ok" ? "accent" : "warn"}>
              {health.data.status === "ok"
                ? "All systems operational"
                : `Degraded: ${health.data.degraded.join(", ")}`}
            </Badge>
          )
        }
      />

      {/* Currency ribbon sits at the very top: it is market context the rest of
          the page is read against, so it belongs above the operational figures
          rather than buried at the bottom. */}
      {feedsQ.data && (
        <FxRibbon rates={feedsQ.data.fx.rates} base={feedsQ.data.fx.base} />
      )}

      {/* The board opens the page: it is the market context every figure below
          is read against, and it doubles as the position summary. */}
      {marketQ.data && <BoardStrip market={marketQ.data} />}

      {/* Hero + KPI row. Exactly one hero figure per view. */}
      <Reveal>
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
          <Card>
            <Hero
              label="Storage used"
              value={`${utilisation}%`}
              sub={`${num.format(Math.round(d.storage.stored_bu))} of ${num.format(Math.round(d.storage.capacity_bu))} bu across ${d.storage.bins} bins`}
              tone={
                utilisation > 90
                  ? "danger"
                  : utilisation > 75
                    ? "warn"
                    : "accent"
              }
            />
            <div
              role="meter"
              aria-valuenow={utilisation}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={`Storage ${utilisation} percent full`}
              className="mt-4 h-2.5 overflow-hidden rounded-full"
              style={{ background: "var(--surface-2)" }}
            >
              <motion.div
                className="h-full rounded-r-[4px]"
                style={{
                  background:
                    utilisation > 90 ? "var(--danger)" : "var(--series)",
                }}
                initial={{ width: 0 }}
                animate={{ width: `${Math.min(utilisation, 100)}%` }}
                transition={{ duration: 0.7, ease: "easeOut" }}
              />
            </div>
          </Card>

          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
            <Card className="flex flex-col justify-between">
              <div>
                <p className="text-[0.7rem] font-medium uppercase tracking-wider text-[var(--text-muted)]">
                  Deliveries (7d)
                </p>
                <p className="mt-1.5 text-2xl font-semibold leading-none">
                  {d.deliveries.last_7_days}
                </p>
              </div>
              {daily.length > 0 && (
                <div className="mt-2">
                  <Sparkline data={daily.slice(-12).map((p) => p.count)} />
                </div>
              )}
              <p className="mt-1.5 text-xs text-[var(--text-faint)]">
                {d.deliveries.unverified} unverified
              </p>
            </Card>

            <Card className="flex flex-col justify-between">
              <div>
                <p className="text-[0.7rem] font-medium uppercase tracking-wider text-[var(--text-muted)]">
                  Open contracts
                </p>
                <p className="mt-1.5 text-2xl font-semibold leading-none">
                  {d.contracts.open}
                </p>
              </div>
              <p className="mt-2 text-xs text-[var(--text-faint)]">
                {d.contracts.expiring_30d} expiring in 30 days
              </p>
            </Card>

            <Card className="col-span-2 flex flex-col justify-between sm:col-span-1">
              <div>
                <p className="text-[0.7rem] font-medium uppercase tracking-wider text-[var(--text-muted)]">
                  Open alerts
                </p>
                <p
                  className="mt-1.5 text-2xl font-semibold leading-none"
                  style={{
                    color:
                      d.open_alerts > 0 ? "var(--danger)" : "var(--accent)",
                  }}
                >
                  {d.open_alerts}
                </p>
              </div>
              {/* Severity as icon + word — never colour alone. */}
              <div className="mt-2 flex flex-wrap gap-1.5">
                {(["high", "medium", "low"] as const)
                  .filter((s) => severities[s])
                  .map((s) => (
                    <span
                      key={s}
                      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[0.65rem] font-semibold"
                      style={{
                        color: s === "high" ? "var(--danger)" : "var(--warn)",
                        background:
                          s === "high"
                            ? "var(--danger-soft)"
                            : "var(--warn-soft)",
                      }}
                    >
                      <span aria-hidden="true">{s === "high" ? "▲" : "◆"}</span>
                      {severities[s]} {s}
                    </span>
                  ))}
              </div>
              <Link
                href="/risk"
                className="mt-2 rounded text-xs font-medium text-[var(--accent-text)]"
              >
                Review risk →
              </Link>
            </Card>
          </div>
        </div>
      </Reveal>

      {/* Trend + weather */}
      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Reveal delay={0.06} className="h-full">
          <Card title="Deliveries per day — last 14 days" className="h-full">
            {daily.length > 0 ? (
              <TrendChart data={daily} label="Deliveries" height={380} />
            ) : (
              <Empty>No delivery history yet.</Empty>
            )}
          </Card>
        </Reveal>

        <Reveal delay={0.1} className="h-full">
          <Card title="Weather — Open-Meteo" className="h-full">
            {weather.isLoading && <Loading label="Loading forecast" />}
            {weather.data && weather.data.facilities.length === 0 && (
              <p className="text-sm text-[var(--text-muted)]">
                No forecast synced yet. Run the{" "}
                <span className="font-medium">weather</span> agent from the
                Agents page.
              </p>
            )}
            {/* Six facilities of card + 3-day strip would run ~3x the height of
                the chart beside it, so the list scrolls in place. tabIndex makes
                the region keyboard-reachable — a scroll container that only a
                mouse can reach is a WCAG 2.1.1 failure. */}
            <div
              className="max-h-[17rem] space-y-4 overflow-y-auto pr-1"
              role="region"
              aria-label="Forecast by facility, scrollable"
              tabIndex={0}
            >
              {weather.data?.facilities.map((entry) => {
                const today = entry.forecast[0];
                if (!today) return null;
                const condition = conditionFor(today);
                return (
                  <div key={entry.facility}>
                    <div className="flex items-center gap-3">
                      <WeatherIcon condition={condition} />
                      <div className="min-w-0">
                        <p className="truncate text-xs font-semibold">
                          {entry.facility}
                        </p>
                        <p className="tabular text-lg font-semibold leading-tight">
                          {today.temp_max_c?.toFixed(0)}°
                          <span className="ml-1 text-xs font-normal text-[var(--text-muted)]">
                            / {today.temp_min_c?.toFixed(0)}°
                          </span>
                        </p>
                        {/* The condition is always stated in text — the glyph
                            never carries it alone. */}
                        <p className="text-[0.7rem] text-[var(--text-muted)]">
                          {CONDITION_LABEL[condition]} ·{" "}
                          {CONDITION_NOTE[condition]}
                        </p>
                      </div>
                    </div>

                    <ul className="mt-2 flex gap-2">
                      {entry.forecast.slice(1, 4).map((day) => {
                        const c = conditionFor(day);
                        return (
                          <li
                            key={day.date}
                            className="flex-1 rounded-lg border border-[var(--border)] px-2 py-1.5 text-center"
                          >
                            <p className="text-[0.65rem] text-[var(--text-muted)]">
                              <time dateTime={day.date}>
                                {new Date(
                                  `${day.date}T00:00:00Z`,
                                ).toLocaleDateString(undefined, {
                                  weekday: "short",
                                  timeZone: "UTC",
                                })}
                              </time>
                            </p>
                            <div className="my-0.5 flex justify-center">
                              <WeatherIcon condition={c} size={20} />
                            </div>
                            <p className="tabular text-[0.7rem] font-semibold">
                              {day.temp_max_c?.toFixed(0)}°
                            </p>
                            <span className="sr-only">
                              {CONDITION_LABEL[c]}
                            </span>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                );
              })}
            </div>
            {weather.data?.fetched_at && (
              <p className="mt-3 border-t border-[var(--border)] pt-2 text-[0.65rem] text-[var(--text-faint)]">
                Synced{" "}
                <time dateTime={weather.data.fetched_at}>
                  {new Date(weather.data.fetched_at).toLocaleString()}
                </time>
              </p>
            )}
          </Card>
        </Reveal>
      </div>

      {/* Bins + financials */}
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Reveal delay={0.14} className="h-full">
          <Card title="Bin inventory" className="h-full">
            {storage.isLoading && <Loading label="Loading bins" />}
            {binBars.length > 0 ? (
              // Every bin across every facility, so this list grows with the
              // business. Scrolls in place rather than stretching the row to
              // three times the height of the card beside it. Same tabIndex
              // reasoning as the forecast list — scroll must be keyboard-reachable.
              <div
                className="max-h-[24rem] overflow-y-auto pr-1"
                role="region"
                aria-label="Bushels stored per bin, scrollable"
                tabIndex={0}
              >
                <BarChart
                  data={binBars}
                  label="Bushels stored per bin"
                  format={(v) => `${num.format(Math.round(v))} bu`}
                />
              </div>
            ) : (
              !storage.isLoading && <Empty>No bins configured.</Empty>
            )}
          </Card>
        </Reveal>

        <Reveal delay={0.18} className="h-full">
          {/* Stacked, so this column fills the height that Bin inventory sets
              instead of stopping halfway and leaving the row half empty. */}
          <div className="flex h-full flex-col gap-4">
            {financials ? (
              <Card title="Receivables by status">
                <BarChart
                  data={financeBars}
                  label="Invoice value by status"
                  format={(v) => money.format(v)}
                />
              </Card>
            ) : (
              <Card title="Receivables by status">
                <p className="text-sm text-[var(--text-muted)]">
                  Hidden for the{" "}
                  <span className="font-medium capitalize">
                    {session?.role}
                  </span>{" "}
                  role. Financial figures are limited to accountant and
                  executive accounts.
                </p>
              </Card>
            )}
            <Card title="System">
              {health.data && (
                <dl className="grid gap-2 sm:grid-cols-3">
                  {Object.entries(health.data.checks).map(([name, state]) => (
                    <div
                      key={name}
                      className="widget-tile flex items-center justify-between gap-3 px-3 py-2"
                    >
                      <dt className="text-sm capitalize text-[var(--text-muted)]">
                        {name.replace(/_/g, " ")}
                      </dt>
                      <dd>
                        <Badge tone={state === "ok" ? "accent" : "danger"}>
                          {state === "ok" ? "OK" : "Failing"}
                        </Badge>
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </Card>
          </div>
        </Reveal>
      </div>

      {/* Position & board — the autohedge surface. */}
      <Reveal delay={0.2}>
        <Card
          title="Position detail — per commodity"
          className="mt-4"
          action={
            marketQ.data?.fetched_at && (
              <span className="text-[0.65rem] text-[var(--text-faint)]">
                synced{" "}
                <time dateTime={marketQ.data.fetched_at}>
                  {new Date(marketQ.data.fetched_at).toLocaleTimeString()}
                </time>
              </span>
            )
          }
        >
          {marketQ.isLoading && <Loading label="Loading board" />}
          {marketQ.data && marketQ.data.board.length === 0 && (
            <Empty>
              No prices synced yet. Run the{" "}
              <span className="font-medium">market</span> agent from the Agents
              page.
            </Empty>
          )}

          {marketQ.data && marketQ.data.board.length > 0 && (
            <>
              {/* Prices and the position summary are in the board strip at the
                  top of the page; this is the row-level breakdown behind it. */}
              {marketQ.data.positions.length > 0 && (
                <div>
                  <Table
                    caption="Net open grain position per commodity with mark-to-market valuation"
                    headers={
                      marketQ.data.financials_visible
                        ? [
                            "Commodity",
                            "Direction",
                            "Net bu",
                            "Long",
                            "Short",
                            "Unrealised",
                          ]
                        : ["Commodity", "Direction", "Net bu", "Long", "Short"]
                    }
                  >
                    {marketQ.data.positions.map((p) => (
                      <tr key={p.commodity}>
                        <Td className="font-medium">{p.commodity}</Td>
                        <Td>
                          {/* Direction as a word + arrow, never colour alone. */}
                          <span className="inline-flex items-center gap-1 text-xs font-semibold">
                            <span aria-hidden="true">
                              {p.direction === "long"
                                ? "↑"
                                : p.direction === "short"
                                  ? "↓"
                                  : "—"}
                            </span>
                            {p.direction}
                          </span>
                        </Td>
                        <Td className="tabular font-semibold">
                          {num.format(Math.round(Math.abs(p.net_bu)))}
                        </Td>
                        <Td className="tabular text-[var(--text-muted)]">
                          {num.format(Math.round(p.long_bu))}
                        </Td>
                        <Td className="tabular text-[var(--text-muted)]">
                          {num.format(Math.round(p.short_bu))}
                        </Td>
                        {marketQ.data!.financials_visible && (
                          <Td
                            className="tabular font-semibold"
                            style={{
                              color:
                                (p.unrealised_usd ?? 0) < 0
                                  ? "var(--danger)"
                                  : "var(--accent)",
                            }}
                          >
                            {/* Sign is explicit, so the figure does not rely on
                                colour to say which way it went. */}
                            {(p.unrealised_usd ?? 0) < 0 ? "-" : "+"}
                            {money.format(Math.abs(p.unrealised_usd ?? 0))}
                          </Td>
                        )}
                      </tr>
                    ))}
                  </Table>
                  <p className="mt-2 text-[0.65rem] text-[var(--text-faint)]">
                    Mark-to-market on undelivered balances only. Exposure and
                    valuation — not a hedge recommendation.
                  </p>
                </div>
              )}

              {!marketQ.data.financials_visible && (
                <p className="mt-3 text-xs text-[var(--text-muted)]">
                  Valuations hidden for the{" "}
                  <span className="font-medium capitalize">
                    {session?.role}
                  </span>{" "}
                  role — board prices and bushel exposure shown.
                </p>
              )}
            </>
          )}
        </Card>
      </Reveal>

      {/* Market news — context for the board above it. */}
      <Reveal delay={0.22}>
        <Card
          title="Grain market news"
          className="mt-4"
          action={
            <span className="text-[0.65rem] text-[var(--text-faint)]">
              Google News · agricultural trade press
            </span>
          }
        >
          {feedsQ.isLoading && <Loading label="Loading headlines" />}
          {feedsQ.data && feedsQ.data.news.length === 0 && (
            <Empty>
              No headlines synced yet. Run the{" "}
              <span className="font-medium">news</span> agent from the Agents
              page.
            </Empty>
          )}
          {feedsQ.data && feedsQ.data.news.length > 0 && (
            <ul className="grid gap-1.5 sm:grid-cols-2">
              {feedsQ.data.news.map((n, i) => (
                <li
                  key={n.url}
                  // The lead story spans both columns, the way a news widget
                  // gives its top item the full tile and stacks the rest.
                  className={i === 0 ? "sm:col-span-2" : undefined}
                >
                  <a
                    href={n.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="widget-tile group block h-full px-3 py-2.5 transition-colors hover:bg-[var(--surface)]"
                  >
                    <span
                      className={`block leading-snug group-hover:text-[var(--accent-text)] ${
                        i === 0 ? "text-[0.95rem] font-semibold" : "text-sm"
                      }`}
                    >
                      {n.title}
                    </span>
                    <span className="mt-1 flex flex-wrap items-center gap-1.5 text-[0.65rem] text-[var(--text-faint)]">
                      {n.publisher && (
                        <span className="rounded-full bg-[var(--surface)] px-1.5 py-0.5 font-medium">
                          {n.publisher}
                        </span>
                      )}
                      {n.published_at && (
                        <time dateTime={n.published_at}>
                          {new Date(n.published_at).toLocaleDateString(
                            undefined,
                            { month: "short", day: "numeric" },
                          )}
                        </time>
                      )}
                      {/* Stated in text, so the external destination is not
                          signalled by an icon alone. */}
                      <span aria-hidden="true">·</span>
                      <span>opens in a new tab</span>
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </Reveal>

      {/* Row-level detail stays a table — that is the right form for it. */}
      <Reveal delay={0.22}>
        <Card
          title="Recent deliveries"
          className="mt-4"
          action={
            <Link
              href="/risk"
              className="rounded-md text-xs font-medium text-[var(--accent-text)]"
            >
              Review risk →
            </Link>
          }
        >
          {d.recent_events.length === 0 ? (
            <Empty>No deliveries recorded yet.</Empty>
          ) : (
            <Table
              caption="The ten most recent deliveries with weight, moisture and verification status"
              headers={[
                "Ticket",
                "Customer",
                "Truck",
                "Net bu",
                "Moisture",
                "Status",
              ]}
            >
              {d.recent_events.map((event) => (
                <tr key={event.ticket}>
                  <Td className="tabular font-medium">{event.ticket}</Td>
                  <Td>{event.customer}</Td>
                  <Td className="tabular text-[var(--text-muted)]">
                    {event.truck_id}
                  </Td>
                  <Td className="tabular">{num.format(event.net_bu)}</Td>
                  <Td className="tabular">
                    {event.moisture_pct > 15 ? (
                      <span style={{ color: "var(--warn)", fontWeight: 600 }}>
                        ◆ {event.moisture_pct}% high
                      </span>
                    ) : (
                      `${event.moisture_pct}%`
                    )}
                  </Td>
                  <Td>
                    <Badge tone={event.verified ? "accent" : "warn"}>
                      {event.verified ? "Verified" : "Unverified"}
                    </Badge>
                  </Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      </Reveal>
    </VizRoot>
  );
}
