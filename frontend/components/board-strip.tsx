"use client";

/** Compact CBOT board — the widget that opens the dashboard.
 *
 * Modelled on the Stocks widget: one row per commodity, each carrying a symbol,
 * a headline price, a sparkline of recent closes, and the net position. Dense on
 * purpose — this is the first thing read, so it has to answer "where is the
 * market and where am I" without scrolling.
 *
 * Form, per the dataviz rules: the price is a **stat tile** (a single headline
 * number, not a plot) and the sparkline beside it is deliberately unlabelled
 * context, not a chart to read values off — so it carries no axes and no hover
 * layer, and the exact figures live in the position table further down the page.
 *
 * Direction is an **arrow plus the word** long/short/flat, and unrealised P&L
 * carries an explicit sign. Neither relies on colour, which matters here because
 * profit-green and loss-red are the pair that fails deuteranope separation.
 */

import { motion, useReducedMotion } from "motion/react";

import type { Market, MarketPosition } from "@/lib/api";

const num = new Intl.NumberFormat("en-US");
const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const ARROW: Record<MarketPosition["direction"], string> = {
  long: "↑",
  short: "↓",
  flat: "—",
};

/** Sparkline over recent closes. Decorative context for the number beside it. */
function MiniSpark({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const W = 76;
  const H = 26;
  const min = Math.min(...values);
  const max = Math.max(...values);
  // A flat series would divide by zero; render it mid-height instead.
  const span = max - min || 1;
  const step = W / (values.length - 1);
  const points = values.map(
    (v, i) => `${i * step},${H - ((v - min) / span) * H}`,
  );
  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      className="overflow-visible"
      aria-hidden="true"
    >
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="var(--series)"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        // Keeps the 1.5px stroke at 1.5px however the svg is scaled.
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

export function BoardStrip({ market }: { market: Market }) {
  const reduce = useReducedMotion();
  if (market.board.length === 0) return null;

  const positionFor = (commodity: string) =>
    market.positions.find((p) => p.commodity === commodity);

  // Closes per symbol, oldest first, for the sparklines.
  const seriesFor = (symbol: string | null) =>
    symbol
      ? market.history
          .filter((h) => h.symbol === symbol)
          .sort((a, b) => a.date.localeCompare(b.date))
          .map((h) => h.close_usd_per_bu)
      : [];

  return (
    <section
      aria-label="CBOT grain board with net position per commodity"
      className="widget mb-4 p-1.5"
    >
      <div className="flex items-center justify-between px-2.5 pb-1.5 pt-1">
        <h2 className="text-xs font-semibold tracking-tight">
          Grain board
          <span className="ml-2 font-normal text-[var(--text-faint)]">
            CBOT futures · mark to market
          </span>
        </h2>
        {market.fetched_at && (
          <span className="text-[0.65rem] text-[var(--text-faint)]">
            synced{" "}
            <time dateTime={market.fetched_at}>
              {new Date(market.fetched_at).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </time>
          </span>
        )}
      </div>

      <div className="grid gap-1.5 sm:grid-cols-3">
        {market.board.map((b, i) => {
          const pos = positionFor(b.commodity);
          const series = seriesFor(b.symbol);
          const pnl = pos?.unrealised_usd;
          return (
            <motion.div
              key={b.commodity}
              initial={reduce ? false : { opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3, delay: i * 0.05, ease: "easeOut" }}
              className="widget-tile flex items-center justify-between gap-3 px-3 py-2.5"
            >
              <div className="min-w-0">
                <p className="flex items-baseline gap-1.5">
                  <span className="text-sm font-semibold">{b.commodity}</span>
                  <span className="tabular text-[0.65rem] text-[var(--text-faint)]">
                    {b.symbol}
                  </span>
                </p>
                <p className="tabular mt-0.5 text-xl font-semibold leading-none">
                  {b.close_usd_per_bu !== null
                    ? `$${b.close_usd_per_bu.toFixed(2)}`
                    : "—"}
                  <span className="ml-1 text-[0.65rem] font-normal text-[var(--text-faint)]">
                    /bu
                  </span>
                </p>
                {pos && (
                  <p className="mt-1 flex flex-wrap items-center gap-x-1.5 text-[0.65rem] text-[var(--text-muted)]">
                    {/* Arrow + word, never colour alone. */}
                    <span className="font-semibold">
                      <span aria-hidden="true">{ARROW[pos.direction]}</span>{" "}
                      {pos.direction}
                    </span>
                    <span className="tabular">
                      {num.format(Math.round(Math.abs(pos.net_bu)))} bu
                    </span>
                    {pnl !== undefined && pnl !== null && (
                      <span className="tabular font-semibold">
                        {pnl >= 0 ? "+" : "−"}
                        {money.format(Math.abs(pnl))}
                      </span>
                    )}
                  </p>
                )}
              </div>
              <MiniSpark values={series.slice(-14)} />
            </motion.div>
          );
        })}
      </div>
    </section>
  );
}
