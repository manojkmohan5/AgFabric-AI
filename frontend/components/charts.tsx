"use client";

/** Chart primitives.
 *
 * Colour was computed, not eyeballed. Every value below was run through the
 * dataviz validator (`validate_palette.js`) against this app's real surfaces:
 *
 *   series hue    light #1f6f43   dark #3ea472   — ALL CHECKS PASS both modes
 *   ordinal ramp  light #84c1a0 → #4f9c74 → #1f6f43   (2.08:1 light end)
 *                 dark  #245f40 → #31855a → #3ea472   (2.28:1 dark end)
 *
 * Two findings from that run drove the design:
 *
 *  1. The app's brand green vs its danger red measure CVD ΔE 4.0 (deuteranope) —
 *     *below* the 6–8 band where secondary encoding could rescue them. So no
 *     chart here ever uses two mark colours to mean two things. Every chart is
 *     ONE series in ONE hue, which is also what the anti-pattern list prescribes
 *     for nominal categories ("one series → one colour for every bar"). Status —
 *     over capacity, overdue, high severity — rides an **icon + text label**,
 *     never a second fill colour.
 *  2. The UI's dark accent (#5cc98d) sits at OKLCH L 0.756, outside the dark
 *     band's 0.48–0.67. It stays fine as UI chrome; charts use the darker
 *     #3ea472 step so marks sit properly against the dark surface.
 *
 * Mark specs are the fixed ones: bars capped at 24px with a 4px rounded
 * data-end square at the baseline, 2px lines, ≥8px markers with a 2px surface
 * ring, hairline solid gridlines one step off surface, 2px surface gaps between
 * touching fills. Text never wears the series colour.
 *
 * Every chart ships a hover layer and a table-view twin — tooltips enhance, they
 * never gate a value.
 */

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { type ReactNode, useId, useMemo, useState } from "react";

/* ------------------------------------------------------------------ palette */

/** Validated chart roles. Declared as CSS custom properties on a wrapper so the
 *  light/dark swap happens in one place and chart bodies reference roles. */
export function VizRoot({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  // The `.viz-root` custom properties live in globals.css alongside the rest of
  // the design tokens, so the light/dark swap happens in one place.
  return <div className={`viz-root ${className}`}>{children}</div>;
}

/* ------------------------------------------------------------------ helpers */

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / magnitude) * magnitude;
}

const compact = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});
const plain = new Intl.NumberFormat("en-US");

/* ------------------------------------------------------- area / trend chart */

export interface TrendPoint {
  date: string;
  count: number;
}

/** Trend over time, single series → line + 10% wash. Crosshair + tooltip on
 *  hover, and the whole series is also available as a table. */
export function TrendChart({
  data,
  label,
  height = 200,
}: {
  data: TrendPoint[];
  label: string;
  /** viewBox height. The svg scales to container width, so this is really an
   *  aspect-ratio control: raising it makes the plot taller at the same width.
   *  Needed because in a narrow column the default 900x200 renders barely 96px
   *  tall, which left a visible gap under the chart in a stretched grid row. */
  height?: number;
}) {
  const reduce = useReducedMotion();
  const clipId = useId().replace(/:/g, "");
  const [hover, setHover] = useState<number | null>(null);

  // Wide viewBox on purpose: the svg scales to container width, so a flatter
  // aspect keeps the plot from ballooning to ~300px tall in a full-width card.
  const W = 900;
  const H = height;
  const PAD = { top: 18, right: 18, bottom: 28, left: 38 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;

  const max = niceMax(Math.max(...data.map((d) => d.count), 1));
  const x = (i: number) =>
    PAD.left +
    (data.length === 1 ? plotW / 2 : (i / (data.length - 1)) * plotW);
  const y = (v: number) => PAD.top + plotH - (v / max) * plotH;

  const line = data.map((d, i) => `${x(i)},${y(d.count)}`).join(" ");
  const area = `${PAD.left},${PAD.top + plotH} ${line} ${x(data.length - 1)},${PAD.top + plotH}`;

  // Three ticks is enough context; more competes with the data.
  const ticks = [0, max / 2, max];
  const peak = data.reduce((a, b) => (b.count > a.count ? b : a), data[0]);
  const peakIndex = data.indexOf(peak);
  const active = hover !== null ? data[hover] : null;

  return (
    <div>
      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          className="block h-auto w-full"
          role="img"
          aria-label={`${label} over the last ${data.length} days. Peak of ${peak.count} on ${peak.date}. Full values are in the table below.`}
          onPointerLeave={() => setHover(null)}
        >
          <defs>
            <clipPath id={clipId}>
              <rect x={PAD.left} y={PAD.top} width={plotW} height={plotH + 1} />
            </clipPath>
          </defs>

          {/* Hairline solid gridlines, one step off surface, recessive. */}
          {ticks.map((t) => (
            <g key={t}>
              {/* non-scaling-stroke so the hairline is genuinely 1px on
                  screen. Without it the viewBox scale-up (~1.2× in a full-width
                  card) makes every "hairline" thicker than the spec allows. */}
              <line
                x1={PAD.left}
                y1={y(t)}
                x2={W - PAD.right}
                y2={y(t)}
                stroke="var(--grid)"
                strokeWidth={1}
                vectorEffect="non-scaling-stroke"
              />
              <text
                x={PAD.left - 7}
                y={y(t) + 3.5}
                textAnchor="end"
                fontSize={9.5}
                fill="var(--ink-muted)"
                style={{ fontVariantNumeric: "tabular-nums" }}
              >
                {plain.format(Math.round(t))}
              </text>
            </g>
          ))}

          <g clipPath={`url(#${clipId})`}>
            <motion.polygon
              points={area}
              fill="var(--series-wash)"
              initial={reduce ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.5 }}
            />
            <motion.polyline
              points={line}
              fill="none"
              stroke="var(--series)"
              strokeWidth={2}
              vectorEffect="non-scaling-stroke"
              strokeLinejoin="round"
              strokeLinecap="round"
              initial={reduce ? false : { pathLength: 0 }}
              animate={{ pathLength: 1 }}
              transition={{ duration: 0.7, ease: "easeOut" }}
            />
          </g>

          {/* Direct-label the extreme only — never a number on every point.
              The 2px ring is in the surface colour so the marker stays legible
              where it sits on the line. */}
          <circle
            cx={x(peakIndex)}
            cy={y(peak.count)}
            r={4.5}
            fill="var(--series)"
            stroke="var(--viz-surface)"
            strokeWidth={2}
            vectorEffect="non-scaling-stroke"
          />
          <text
            x={x(peakIndex)}
            y={y(peak.count) - 13}
            textAnchor="middle"
            fontSize={11}
            fontWeight={600}
            fill="var(--ink)"
          >
            {peak.count}
          </text>

          {/* First and last date only; the rest would collide. */}
          <text x={PAD.left} y={H - 8} fontSize={9.5} fill="var(--ink-muted)">
            {new Date(`${data[0].date}T00:00:00Z`).toLocaleDateString(
              undefined,
              {
                month: "short",
                day: "numeric",
                timeZone: "UTC",
              },
            )}
          </text>
          <text
            x={W - PAD.right}
            y={H - 8}
            textAnchor="end"
            fontSize={9.5}
            fill="var(--ink-muted)"
          >
            {new Date(
              `${data[data.length - 1].date}T00:00:00Z`,
            ).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
              timeZone: "UTC",
            })}
          </text>

          {/* Crosshair. Hit bands are full-height so the target is generous —
              never a pinpoint. */}
          {active && (
            <line
              x1={x(hover!)}
              y1={PAD.top}
              x2={x(hover!)}
              y2={PAD.top + plotH}
              stroke="var(--axis)"
              strokeWidth={1}
              vectorEffect="non-scaling-stroke"
            />
          )}
          {data.map((d, i) => (
            <rect
              key={d.date}
              x={x(i) - plotW / data.length / 2}
              y={PAD.top}
              width={plotW / data.length}
              height={plotH}
              fill="transparent"
              onPointerEnter={() => setHover(i)}
            />
          ))}
          {active && (
            <circle
              cx={x(hover!)}
              cy={y(active.count)}
              r={4.5}
              fill="var(--series)"
              stroke="var(--viz-surface)"
              strokeWidth={2}
              vectorEffect="non-scaling-stroke"
            />
          )}
        </svg>

        <AnimatePresence>
          {active && (
            <motion.div
              initial={reduce ? false : { opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.12 }}
              className="pointer-events-none absolute -top-1 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-xs shadow-sm"
              style={{
                left: `${(x(hover!) / W) * 100}%`,
                transform: "translateX(-50%)",
              }}
            >
              <span className="font-semibold tabular">{active.count}</span>{" "}
              <span className="text-[var(--text-muted)]">
                on{" "}
                {new Date(`${active.date}T00:00:00Z`).toLocaleDateString(
                  undefined,
                  {
                    month: "short",
                    day: "numeric",
                    timeZone: "UTC",
                  },
                )}
              </span>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <TableTwin
        summary={`${label} per day, ${data.length} days`}
        headers={["Date", label]}
        rows={data.map((d) => [
          new Date(`${d.date}T00:00:00Z`).toLocaleDateString(undefined, {
            timeZone: "UTC",
          }),
          String(d.count),
        ])}
      />
    </div>
  );
}

/* ----------------------------------------------------------- bar chart (h) */

export interface BarDatum {
  label: string;
  value: number;
  /** Optional status. Carried as icon + text, never as a different fill. */
  status?: { tone: "warn" | "critical"; note: string };
  hint?: string;
}

/** Horizontal bars, magnitude across named categories → ONE hue for every bar.
 *  Long/many category names are why this is horizontal rather than columns. */
export function BarChart({
  data,
  label,
  format = (v) => plain.format(v),
}: {
  data: BarDatum[];
  label: string;
  format?: (v: number) => string;
}) {
  const reduce = useReducedMotion();
  const [hover, setHover] = useState<string | null>(null);
  const max = Math.max(...data.map((d) => d.value), 1);

  return (
    <div>
      <ul className="space-y-2.5" aria-label={label}>
        {data.map((d, i) => {
          const pct = (d.value / max) * 100;
          const isHover = hover === d.label;
          return (
            <li
              key={d.label}
              onPointerEnter={() => setHover(d.label)}
              onPointerLeave={() => setHover(null)}
              className="group"
            >
              <div className="mb-1 flex items-baseline justify-between gap-3">
                <span className="flex min-w-0 items-center gap-1.5 text-xs">
                  <span className="truncate font-medium">{d.label}</span>
                  {d.status && (
                    // Icon + word. This is the whole status channel — the bar
                    // fill stays the single series hue.
                    <span
                      className="inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[0.65rem] font-semibold"
                      style={{
                        color:
                          d.status.tone === "critical"
                            ? "var(--danger)"
                            : "var(--warn)",
                        background:
                          d.status.tone === "critical"
                            ? "var(--danger-soft)"
                            : "var(--warn-soft)",
                      }}
                    >
                      <span aria-hidden="true">
                        {d.status.tone === "critical" ? "▲" : "◆"}
                      </span>
                      {d.status.note}
                    </span>
                  )}
                </span>
                {/* Value at the tip, in ink — never in the series colour. */}
                <span className="tabular shrink-0 text-xs font-semibold">
                  {format(d.value)}
                </span>
              </div>
              {/* Track is a lighter step of the same ramp, per the meter spec. */}
              <div
                className="h-2 overflow-hidden rounded-full"
                style={{ background: "var(--surface-2)" }}
              >
                <motion.div
                  className="h-full"
                  style={{
                    background: "var(--series)",
                    // 4px rounded data-end, square at the baseline (left).
                    borderRadius: "0 4px 4px 0",
                    opacity: isHover ? 1 : 0.9,
                  }}
                  initial={reduce ? false : { width: 0 }}
                  animate={{ width: `${pct}%` }}
                  transition={{
                    duration: 0.55,
                    delay: reduce ? 0 : i * 0.04,
                    ease: "easeOut",
                  }}
                />
              </div>
              {d.hint && (
                <p className="mt-0.5 text-[0.65rem] text-[var(--text-faint)]">
                  {d.hint}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      <TableTwin
        summary={label}
        headers={["Category", "Value", "Status"]}
        rows={data.map((d) => [
          d.label,
          format(d.value),
          d.status?.note ?? "—",
        ])}
      />
    </div>
  );
}

/* ------------------------------------------------------------- table twin */

/** The WCAG-clean equivalent of the chart above it. Collapsed by default so it
 *  does not compete visually, but it is real markup — present, focusable and
 *  reachable, not generated on demand. */
function TableTwin({
  summary,
  headers,
  rows,
}: {
  summary: string;
  headers: string[];
  rows: string[][];
}) {
  return (
    <details className="mt-3">
      <summary className="cursor-pointer text-[0.7rem] text-[var(--text-faint)] hover:text-[var(--text-muted)]">
        View as table
      </summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full border-collapse text-xs">
          <caption className="sr-only">{summary}</caption>
          <thead>
            <tr className="border-b border-[var(--border)]">
              {headers.map((h) => (
                <th
                  key={h}
                  scope="col"
                  className="px-2 py-1.5 text-left font-semibold text-[var(--text-muted)]"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className="border-b border-[var(--border)]">
                {row.map((cell, j) => (
                  <td key={j} className="tabular px-2 py-1.5">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/* ---------------------------------------------------------- hero + sparkline */

/** The one number the dashboard leads with. ≥48px, same sans as everything,
 *  proportional figures (tabular would make it look loose at this size). */
export function Hero({
  value,
  label,
  sub,
  tone = "neutral",
}: {
  value: string;
  label: string;
  sub?: string;
  tone?: "neutral" | "accent" | "warn" | "danger";
}) {
  const reduce = useReducedMotion();
  const color = {
    neutral: "var(--text)",
    accent: "var(--accent)",
    warn: "var(--warn)",
    danger: "var(--danger)",
  }[tone];

  return (
    <div>
      <p className="text-[0.7rem] font-medium uppercase tracking-wider text-[var(--text-muted)]">
        {label}
      </p>
      <motion.p
        className="mt-1 text-5xl font-semibold leading-none"
        style={{ color }}
        initial={reduce ? false : { opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        {value}
      </motion.p>
      {sub && <p className="mt-2 text-xs text-[var(--text-muted)]">{sub}</p>}
    </div>
  );
}

/** 12-point sparkline for a stat tile — de-emphasis hue, no axes, no labels. */
export function Sparkline({ data }: { data: number[] }) {
  const reduce = useReducedMotion();
  const W = 96;
  const H = 26;
  const max = Math.max(...data, 1);
  const points = data
    .map(
      (v, i) =>
        `${(i / Math.max(data.length - 1, 1)) * W},${H - (v / max) * (H - 3) - 1.5}`,
    )
    .join(" ");

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-6 w-24" aria-hidden="true">
      <motion.polyline
        points={points}
        fill="none"
        stroke="var(--series)"
        strokeWidth={1.75}
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity={0.75}
        initial={reduce ? false : { pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={{ duration: 0.6 }}
      />
    </svg>
  );
}

export { compact };
