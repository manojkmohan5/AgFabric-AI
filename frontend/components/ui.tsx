/** UI primitives.
 *
 * Hand-rolled rather than pulled from a component library: it is ~200 lines, it
 * keeps the bundle small, and it means the accessibility behaviour is ours to
 * guarantee rather than to audit.
 *
 * Two rules run through all of it:
 *  - native elements first. <button>, <table>, <label> — ARIA only where HTML
 *    genuinely cannot express the state.
 *  - status is never colour alone. Every severity and state carries a text label
 *    and a shape/glyph, so it survives greyscale and colour blindness.
 */

import type { ReactNode } from "react";

/* ----------------------------------------------------------------- layout */

export function Card({
  title,
  action,
  children,
  className = "",
}: {
  title?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      // card-widget carries the radius, translucency, blur and shadow, so a
      // card reads as a widget floating on the app canvas on every page.
      className={`card-widget ${className}`}
    >
      {title && (
        <header className="flex items-center justify-between gap-3 border-b border-[color-mix(in_oklab,var(--border)_70%,transparent)] px-4 py-3">
          <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: "neutral" | "accent" | "warn" | "danger";
}) {
  const toneColor = {
    neutral: "var(--text)",
    accent: "var(--accent)",
    warn: "var(--warn)",
    danger: "var(--danger)",
  }[tone];

  return (
    <div className="rounded-[var(--radius)] border border-[var(--border)] bg-[var(--surface)] px-4 py-3.5">
      <dt className="text-[0.7rem] font-medium uppercase tracking-wider text-[var(--text-muted)]">
        {label}
      </dt>
      <dd
        className="tabular mt-1.5 text-2xl font-semibold leading-none"
        style={{ color: toneColor }}
      >
        {value}
      </dd>
      {hint && (
        <p className="mt-1.5 text-xs text-[var(--text-faint)]">{hint}</p>
      )}
    </div>
  );
}

/* ----------------------------------------------------------------- controls */

const BUTTON_BASE =
  "inline-flex items-center justify-center gap-1.5 rounded-lg text-sm font-medium " +
  "transition-colors disabled:cursor-not-allowed disabled:opacity-55";

export function Button({
  children,
  variant = "primary",
  size = "md",
  // Pulled out of `rest` deliberately: spreading rest after className would let a
  // caller's className replace the variant styling instead of adding to it.
  className = "",
  ...rest
}: {
  children: ReactNode;
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const variants = {
    primary:
      "bg-[var(--accent)] text-[var(--bg)] hover:opacity-90 border border-transparent",
    secondary:
      "bg-[var(--surface)] text-[var(--text)] border border-[var(--border-strong)] hover:bg-[var(--surface-2)]",
    ghost:
      "bg-transparent text-[var(--text-muted)] border border-transparent hover:bg-[var(--surface-2)] hover:text-[var(--text)]",
    danger:
      "bg-transparent text-[var(--danger)] border border-[var(--danger)] hover:bg-[var(--danger-soft)]",
  }[variant];
  const sizes = { sm: "h-8 px-2.5", md: "h-9 px-3.5" }[size];

  return (
    <button
      type="button"
      className={`${BUTTON_BASE} ${variants} ${sizes} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  error,
  id,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  id: string;
  children: (props: {
    id: string;
    "aria-describedby": string | undefined;
    "aria-invalid": boolean | undefined;
  }) => ReactNode;
}) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  // Both ids when both exist, so a screen reader announces the hint and the error.
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;

  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      {hint && (
        <p id={hintId} className="text-xs text-[var(--text-muted)]">
          {hint}
        </p>
      )}
      {children({
        id,
        "aria-describedby": describedBy,
        "aria-invalid": error ? true : undefined,
      })}
      {error && (
        <p id={errorId} className="text-xs font-medium text-[var(--danger)]">
          {/* Prefixed in text, so the error is not identified by colour alone. */}
          Error: {error}
        </p>
      )}
    </div>
  );
}

export const inputClass =
  "w-full rounded-lg border border-[var(--border-strong)] bg-[var(--surface)] px-3 py-2 " +
  "text-sm text-[var(--text)] placeholder:text-[var(--text-faint)]";

/* ----------------------------------------------------------------- status */

const SEVERITY = {
  high: {
    label: "High",
    glyph: "▲",
    fg: "var(--danger)",
    bg: "var(--danger-soft)",
  },
  medium: {
    label: "Medium",
    glyph: "◆",
    fg: "var(--warn)",
    bg: "var(--warn-soft)",
  },
  low: { label: "Low", glyph: "●", fg: "var(--info)", bg: "var(--info-soft)" },
} as const;

export function SeverityBadge({
  severity,
}: {
  severity: keyof typeof SEVERITY;
}) {
  const s = SEVERITY[severity] ?? SEVERITY.low;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-semibold"
      style={{ color: s.fg, background: s.bg }}
    >
      {/* Glyph is decorative — the adjacent word carries the meaning. */}
      <span aria-hidden="true">{s.glyph}</span>
      {s.label}
    </span>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "warn" | "danger" | "info";
}) {
  const tones = {
    neutral: { color: "var(--text-muted)", background: "var(--surface-2)" },
    accent: { color: "var(--accent-text)", background: "var(--accent-soft)" },
    warn: { color: "var(--warn)", background: "var(--warn-soft)" },
    danger: { color: "var(--danger)", background: "var(--danger-soft)" },
    info: { color: "var(--info)", background: "var(--info-soft)" },
  }[tone];

  return (
    <span
      className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium"
      style={tones}
    >
      {children}
    </span>
  );
}

/** Confidence as a labelled meter. `role="meter"` with explicit now/min/max, so
 *  the value is announced rather than inferred from bar width. */
export function Confidence({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  return (
    <div className="flex items-center gap-2">
      <div
        role="meter"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Confidence ${pct} percent`}
        className="h-1.5 w-24 overflow-hidden rounded-full bg-[var(--surface-2)]"
      >
        <div
          className="h-full rounded-full"
          style={{ width: `${pct}%`, background: "var(--accent)" }}
        />
      </div>
      <span className="tabular text-xs font-medium text-[var(--text-muted)]">
        {pct}%
      </span>
    </div>
  );
}

/* ----------------------------------------------------------------- feedback */

/** Loading state. `role="status"` + aria-busy so it is announced, not silent. */
export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div
      role="status"
      aria-busy="true"
      className="flex items-center gap-2 py-6 text-sm text-[var(--text-muted)]"
    >
      <span
        aria-hidden="true"
        className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-[var(--border-strong)] border-t-[var(--accent)]"
      />
      {label}…
    </div>
  );
}

/** Errors use role="alert" so they interrupt; that is the point of an error. */
export function ErrorNote({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border px-3 py-2.5 text-sm"
      style={{
        borderColor: "var(--danger)",
        background: "var(--danger-soft)",
        color: "var(--danger)",
      }}
    >
      <strong className="font-semibold">Error:</strong> {message}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="py-8 text-center text-sm text-[var(--text-muted)]">
      {children}
    </p>
  );
}

/* ----------------------------------------------------------------- table */

export function Table({
  caption,
  headers,
  children,
}: {
  caption: string;
  headers: string[];
  children: ReactNode;
}) {
  return (
    <div className="-mx-4 overflow-x-auto px-4">
      <table className="w-full min-w-[36rem] border-collapse text-sm">
        {/* Every table names itself. Visually hidden, read aloud. */}
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-[var(--border)]">
            {headers.map((h) => (
              <th
                key={h}
                scope="col"
                className="whitespace-nowrap px-2.5 py-2 text-left text-[0.7rem] font-semibold uppercase tracking-wider text-[var(--text-muted)]"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Td({
  children,
  className = "",
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <td
      className={`border-b border-[var(--border)] px-2.5 py-2.5 align-top ${className}`}
      style={style}
    >
      {children}
    </td>
  );
}
