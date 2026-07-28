"use client";

/** Currency ribbon — the strip that sits at the very top of the dashboard.
 *
 * Form choice, per the dataviz rules: six exchange rates are "a handful of
 * headline numbers", so this is a **KPI row of stat tiles**, not a chart. Each
 * tile follows the stat-tile contract — label, value, delta against a named
 * period — and nothing here is a plot, so no hover layer is required.
 *
 * Direction is carried by an **arrow glyph plus a signed number**, never by
 * colour alone: the arrow and the sign both survive greyscale and colour
 * blindness, and the tint is only reinforcement.
 *
 * Numbers use `tabular-nums` because they sit in a row and must align, and each
 * rate is formatted to a sensible precision for its magnitude — ARS at ~1,494
 * does not want four decimals, EUR at 0.878 does.
 *
 * On a fresh sync every `change_pct` is null (only one observation exists), so
 * the delta reads "first sync" rather than a fabricated 0.0%.
 */

import { motion, useReducedMotion } from "motion/react";

import type { FxRate } from "@/lib/api";

/** Rates span 0.87 (EUR) to 1,494 (ARS); one format for both would be wrong. */
function formatRate(rate: number): string {
  if (rate >= 100)
    return rate.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (rate >= 10) return rate.toFixed(2);
  return rate.toFixed(4);
}

const ARROW: Record<FxRate["direction"], string> = {
  up: "▲",
  down: "▼",
  flat: "—",
};

export function FxRibbon({ rates, base }: { rates: FxRate[]; base: string }) {
  const reduce = useReducedMotion();
  if (rates.length === 0) return null;

  return (
    <section
      aria-label={`${base} exchange rates against export-relevant currencies`}
      className="widget mb-4 p-1.5"
    >
      <div className="flex items-center justify-between gap-3 px-2.5 pb-1.5 pt-1">
        <h2 className="text-xs font-semibold tracking-tight">
          {base} strength
          <span className="ml-2 font-normal text-[var(--text-faint)]">
            per 1 {base} · drives export competitiveness
          </span>
        </h2>
        {rates[0]?.quoted_on && (
          <time
            dateTime={rates[0].quoted_on}
            className="text-[0.65rem] text-[var(--text-faint)]"
          >
            {new Date(`${rates[0].quoted_on}T00:00:00Z`).toLocaleDateString(
              undefined,
              {
                timeZone: "UTC",
              },
            )}
          </time>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-6">
        {rates.map((r, i) => {
          const tone =
            r.direction === "flat"
              ? "var(--text-muted)"
              : r.direction === "up"
                ? "var(--accent)"
                : "var(--danger)";
          return (
            <motion.div
              key={r.currency}
              // One line per pair. The country and the reason it matters moved
              // to the title attribute: BRL already implies Brazil, so printing
              // both made every tile four lines tall for one number.
              title={r.note}
              className="widget-tile flex items-baseline justify-between gap-2 px-2.5 py-1.5"
              initial={reduce ? false : { opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3, delay: reduce ? 0 : i * 0.04 }}
            >
              <dt className="text-[0.7rem] font-medium text-[var(--text-muted)]">
                {r.currency}
              </dt>
              <dd className="flex items-baseline gap-1.5">
                <span className="tabular text-sm font-semibold leading-none">
                  {formatRate(r.rate)}
                </span>
                <span
                  className="tabular text-[0.65rem] font-medium leading-none"
                  style={{ color: tone }}
                >
                  {r.change_pct === null ? (
                    <>
                      <span
                        aria-hidden="true"
                        className="text-[var(--text-faint)]"
                      >
                        —
                      </span>
                      {/* Kept for assistive tech: a bare dash does not say why
                          there is no delta. */}
                      <span className="sr-only">no prior close yet</span>
                    </>
                  ) : (
                    <>
                      {/* Arrow is decorative; the signed number carries it. */}
                      <span aria-hidden="true">{ARROW[r.direction]}</span>
                      {r.change_pct > 0 ? "+" : ""}
                      {r.change_pct.toFixed(2)}%
                    </>
                  )}
                </span>
              </dd>
            </motion.div>
          );
        })}
      </dl>
    </section>
  );
}
