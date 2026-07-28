"use client";

/** Animated weather glyphs.
 *
 * Hand-drawn SVG rather than an icon pack: there are five conditions, each is a
 * few shapes, and the animation has to be driven by the actual numbers (rain
 * amount moves the drops, wind speed moves the gust lines). An icon font would
 * be static and a weather-icon library would be more bytes than the shapes.
 *
 * The condition is derived from the real Open-Meteo fields — precipitation, wind
 * and temperature — so the glyph is a reading of the data, not decoration.
 *
 * Motion respects `prefers-reduced-motion`: with it set, every glyph renders in
 * its rest state and nothing loops. A weather icon is not worth a vestibular
 * complaint.
 *
 * The glyph is `aria-hidden` throughout — the condition is always also stated in
 * adjacent text, so this never carries meaning alone.
 */

import { type TargetAndTransition, motion, useReducedMotion } from "motion/react";

export type Condition = "storm" | "rain" | "wind" | "hot" | "clear";

/** Thresholds. Ordered most-severe first so the worst condition wins. */
export function conditionFor(day: {
  precipitation_mm: number | null;
  wind_max_kmh: number | null;
  temp_max_c: number | null;
}): Condition {
  const rain = day.precipitation_mm ?? 0;
  const wind = day.wind_max_kmh ?? 0;
  const temp = day.temp_max_c ?? 0;
  if (rain >= 25) return "storm";
  if (rain >= 1) return "rain";
  if (wind >= 35) return "wind";
  if (temp >= 32) return "hot";
  return "clear";
}

export const CONDITION_LABEL: Record<Condition, string> = {
  storm: "Heavy rain",
  rain: "Rain",
  wind: "Windy",
  hot: "Hot",
  clear: "Clear",
};

/** Grain-relevant note, since that is what a weather panel is for here. */
export const CONDITION_NOTE: Record<Condition, string> = {
  storm: "Delay outdoor handling",
  rain: "Cover loads",
  wind: "Watch dust and aeration",
  hot: "Good drying conditions",
  clear: "Normal operations",
};

const TONE: Record<Condition, string> = {
  storm: "var(--danger)",
  rain: "var(--info)",
  wind: "var(--info)",
  hot: "var(--warn)",
  clear: "var(--accent)",
};

export function WeatherIcon({
  condition,
  size = 34,
}: {
  condition: Condition;
  size?: number;
}) {
  const reduce = useReducedMotion();
  const tone = TONE[condition];
  // Every loop is gated on `reduce` — an empty object means "render at rest",
  // so the glyph still draws, it just never animates.
  const loop = (
    animate: TargetAndTransition,
    duration: number,
    delay = 0,
  ): { animate?: TargetAndTransition; transition?: object } =>
    reduce
      ? {}
      : {
          animate,
          transition: {
            duration,
            delay,
            repeat: Infinity,
            ease: "easeInOut" as const,
          },
        };

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 40 40"
      aria-hidden="true"
      style={{ overflow: "visible" }}
    >
      {condition === "clear" && (
        <>
          <motion.circle
            cx={20}
            cy={20}
            r={7.5}
            fill={tone}
            {...loop({ scale: [1, 1.06, 1] }, 3)}
            style={{ transformOrigin: "20px 20px" }}
          />
          <motion.g
            stroke={tone}
            strokeWidth={2}
            strokeLinecap="round"
            {...loop({ rotate: 360 }, 26)}
            style={{ transformOrigin: "20px 20px" }}
          >
            {[0, 45, 90, 135, 180, 225, 270, 315].map((deg) => {
              const rad = (deg * Math.PI) / 180;
              return (
                <line
                  key={deg}
                  x1={20 + Math.cos(rad) * 11.5}
                  y1={20 + Math.sin(rad) * 11.5}
                  x2={20 + Math.cos(rad) * 15}
                  y2={20 + Math.sin(rad) * 15}
                />
              );
            })}
          </motion.g>
        </>
      )}

      {condition === "hot" && (
        <>
          <motion.circle
            cx={20}
            cy={18}
            r={8.5}
            fill={tone}
            {...loop({ opacity: [1, 0.78, 1] }, 2.4)}
          />
          {/* Heat shimmer rising off the disc. */}
          {[12, 20, 28].map((cx, i) => (
            <motion.path
              key={cx}
              d={`M${cx} 33 q 2.5 -3 0 -6 q -2.5 -3 0 -6`}
              fill="none"
              stroke={tone}
              strokeWidth={1.6}
              strokeLinecap="round"
              opacity={0.65}
              {...loop({ y: [0, -3, 0], opacity: [0.65, 0.25, 0.65] }, 2, i * 0.25)}
            />
          ))}
        </>
      )}

      {(condition === "rain" || condition === "storm") && (
        <>
          <motion.path
            d="M11 24 a6 6 0 0 1 1.4 -11.8 a8 8 0 0 1 15.1 2.2 a5.5 5.5 0 0 1 -0.6 10.9 z"
            fill="var(--text-muted)"
            opacity={0.9}
            {...loop({ x: [0, 1.5, 0] }, 5)}
          />
          {/* Drop count and speed follow severity — the animation reads the data. */}
          {(condition === "storm" ? [11, 16, 21, 26, 31] : [14, 20, 26]).map((cx, i) => (
            <motion.line
              key={cx}
              x1={cx}
              y1={27}
              x2={cx - 1.5}
              y2={condition === "storm" ? 36 : 33}
              stroke={tone}
              strokeWidth={2}
              strokeLinecap="round"
              initial={{ opacity: 0 }}
              {...loop(
                { opacity: [0, 1, 0], y: [0, 5, 9] },
                condition === "storm" ? 0.85 : 1.3,
                i * 0.16,
              )}
            />
          ))}
          {condition === "storm" && (
            <motion.path
              d="M21 25 l-4 6 h3 l-2 5 l6 -7 h-3 z"
              fill="var(--warn)"
              initial={{ opacity: 0 }}
              {...loop({ opacity: [0, 0, 1, 0] }, 3.4)}
            />
          )}
        </>
      )}

      {condition === "wind" && (
        <>
          <motion.path
            d="M10 22 a5.5 5.5 0 0 1 1.3 -10.9 a7.4 7.4 0 0 1 14 2 a5 5 0 0 1 -0.5 10 z"
            fill="var(--text-muted)"
            opacity={0.85}
            {...loop({ x: [0, 2.5, 0] }, 4)}
          />
          {[
            { y: 28, len: 17 },
            { y: 32, len: 12 },
            { y: 36, len: 20 },
          ].map((gust, i) => (
            <motion.line
              key={gust.y}
              x1={6}
              y1={gust.y}
              x2={6 + gust.len}
              y2={gust.y}
              stroke={tone}
              strokeWidth={2}
              strokeLinecap="round"
              opacity={0.75}
              {...loop({ x: [-4, 8, -4], opacity: [0.2, 0.8, 0.2] }, 2.1, i * 0.3)}
            />
          ))}
        </>
      )}
    </svg>
  );
}
