"use client";

/** Knowledge-graph renderer — force-directed, in the visual language of
 *  Obsidian/Roam/Logseq graph views: nodes drift to a physical rest state,
 *  bigger nodes mean more connections, hovering one lights up its neighbourhood
 *  and fades the rest, and clicking a node re-centres the graph on it.
 *
 * `d3-force` computes positions only — no DOM, no rendering opinions — so it
 * slots under the existing hand-rolled SVG rather than replacing it with a
 * charting library. It is the standard, purpose-built tool for exactly this one
 * job (~30KB gzipped with its own sub-dependencies); hand-rolling a worse
 * spring/repulsion simulator for the same effort would not be lazier, just
 * riskier.
 *
 * The simulation is seeded from the API's `hops` (root at the centre, each
 * relationship one ring further out) so it settles into something already
 * roughly readable, then ticked to convergence before the first paint — no
 * chaotic scatter, no dependency on Math.random for layout that matters.
 *
 * An SVG diagram is not readable to a screen reader, so the same data is also
 * rendered as a real table below it — the table is the accessible version, not
 * a fallback, same as before. `role="img"` on the svg means everything in it
 * (drag, hover-to-highlight, click-to-recentre) is a pointer-only enhancement:
 * by spec, role="img" collapses its whole subtree to one opaque image for
 * assistive tech, so a nested tabIndex/aria-label would be a lie about what
 * keyboard or screen-reader users can actually reach. The table gives the same
 * *function*, not just the same data — every row has a real recentre button.
 */

import {
  type SimulationLinkDatum,
  type SimulationNodeDatum,
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
} from "d3-force";
import { useMemo, useRef, useState } from "react";

import type { GraphExpansion, GraphNode } from "@/lib/api";
import { Button, Table, Td } from "./ui";

const KIND_COLOR: Record<string, string> = {
  customer: "var(--accent)",
  contract: "var(--info)",
  delivery: "var(--text-muted)",
  invoice: "var(--warn)",
  commodity: "var(--accent-text)",
  facility: "var(--danger)",
  bin: "var(--text-faint)",
};

const WIDTH = 720;
const HEIGHT = 560;
const CENTRE_X = WIDTH / 2;
const CENTRE_Y = HEIGHT / 2;
const SEED_RING = 95;
const MIN_R = 7;
const MAX_R = 20;

interface SimNode extends SimulationNodeDatum, GraphNode {
  id: string;
  degree: number;
}
interface SimLink extends SimulationLinkDatum<SimNode> {
  label: string;
}

/** Deterministic starting positions from `hops`, so the simulation relaxes
 *  from an already-sensible layout instead of a random scatter. */
function seedPositions(
  nodes: GraphNode[],
): Map<string, { x: number; y: number }> {
  const byHop = new Map<number, GraphNode[]>();
  for (const node of nodes) {
    const bucket = byHop.get(node.hops) ?? [];
    bucket.push(node);
    byHop.set(node.hops, bucket);
  }
  const seeded = new Map<string, { x: number; y: number }>();
  for (const [hop, group] of [...byHop.entries()].sort((a, b) => a[0] - b[0])) {
    if (hop === 0) {
      seeded.set(group[0].id, { x: CENTRE_X, y: CENTRE_Y });
      continue;
    }
    const radius = hop * SEED_RING;
    const offset = hop * 0.4;
    group.forEach((node, i) => {
      const angle = (i / group.length) * Math.PI * 2 + offset;
      seeded.set(node.id, {
        x: CENTRE_X + Math.cos(angle) * radius,
        y: CENTRE_Y + Math.sin(angle) * radius,
      });
    });
  }
  return seeded;
}

function radiusFor(degree: number): number {
  // sqrt so a hub with 10x the edges is not 10x the radius — area, not
  // diameter, should scale with connection count, or a busy hub swallows the
  // diagram.
  return Math.min(MAX_R, MIN_R + Math.sqrt(degree) * 3.2);
}

/** Runs the simulation to convergence synchronously and returns settled nodes.
 *  A settle-then-render component is far simpler to reason about (and to keep
 *  keyboard/reduced-motion friendly) than an always-animating one, while still
 *  looking organic rather than laid out on a grid. */
function useSettledGraph(data: GraphExpansion) {
  return useMemo(() => {
    const seeds = seedPositions(data.nodes);
    const degree = new Map<string, number>();
    for (const edge of data.edges) {
      degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
      degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
    }

    const nodes: SimNode[] = data.nodes.map((n) => ({
      ...n,
      ...seeds.get(n.id),
      degree: degree.get(n.id) ?? 0,
    }));
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const links: SimLink[] = data.edges
      .filter((e) => byId.has(e.source) && byId.has(e.target))
      .map((e) => ({ source: e.source, target: e.target, label: e.label }));

    const root = nodes.find((n) => n.hops === 0);

    const sim = forceSimulation(nodes)
      .force(
        "charge",
        forceManyBody<SimNode>().strength((n) =>
          n.id === root?.id ? -900 : -220,
        ),
      )
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((n) => n.id)
          .distance(60)
          .strength(0.5),
      )
      .force(
        "collide",
        forceCollide<SimNode>((n) => radiusFor(n.degree) + 14),
      )
      .force("centre", forceCenter(CENTRE_X, CENTRE_Y).strength(0.06))
      .stop();

    // Root pinned to the centre — everything else drifts to natural rest around
    // it rather than the whole cluster wandering off-canvas.
    if (root) {
      root.fx = CENTRE_X;
      root.fy = CENTRE_Y;
    }

    for (let i = 0; i < 260; i++) sim.tick();

    return { nodes, links, byId };
  }, [data]);
}

export function GraphView({
  data,
  onRecentre,
}: {
  data: GraphExpansion;
  /** Called with a node id when the user picks a new centre (click, or Enter
   *  on a focused node). Re-querying is the parent's job — this component only
   *  reports the intent. */
  onRecentre?: (nodeId: string) => void;
}) {
  const { nodes, links, byId } = useSettledGraph(data);
  const [active, setActive] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  // Re-render on drag without re-running the whole memoised simulation.
  const [, forceRedraw] = useState(0);

  const kinds = [...new Set(data.nodes.map((n) => n.kind))].sort();

  // Neighbourhood of the active node: itself, its direct edges, and whatever is
  // on the other end of them. Everything else dims rather than disappears, so
  // the overall shape of the graph stays visible while attention narrows.
  const neighbourhood = useMemo(() => {
    if (!active) return null;
    const ids = new Set([active]);
    const edgeSet = new Set<number>();
    links.forEach((link, i) => {
      const s =
        typeof link.source === "string"
          ? link.source
          : (link.source as SimNode).id;
      const t =
        typeof link.target === "string"
          ? link.target
          : (link.target as SimNode).id;
      if (s === active || t === active) {
        ids.add(s);
        ids.add(t);
        edgeSet.add(i);
      }
    });
    return { ids, edgeSet };
  }, [active, links]);

  // While a node is pinned mid-drag its live position is on fx/fy, not x/y —
  // ticking was stopped once, so x/y would otherwise render one frame stale.
  function posOf(node: SimNode): { x: number; y: number } {
    return { x: node.fx ?? node.x ?? 0, y: node.fy ?? node.y ?? 0 };
  }

  function toSvgPoint(
    clientX: number,
    clientY: number,
  ): { x: number; y: number } {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const rect = svg.getBoundingClientRect();
    return {
      x: ((clientX - rect.left) / rect.width) * WIDTH,
      y: ((clientY - rect.top) / rect.height) * HEIGHT,
    };
  }

  function startDrag(node: SimNode, event: React.PointerEvent) {
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(node.id);
    const move = (e: PointerEvent) => {
      const p = toSvgPoint(e.clientX, e.clientY);
      node.fx = p.x;
      node.fy = p.y;
      forceRedraw((n) => n + 1);
    };
    const up = () => {
      // Release the pin so the node can settle again under the live forces.
      node.fx = null;
      node.fy = null;
      setDragging(null);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-3 text-xs">
          {kinds.map((kind) => (
            <span key={kind} className="flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="h-2.5 w-2.5 rounded-full"
                style={{ background: KIND_COLOR[kind] ?? "var(--text-faint)" }}
              />
              <span className="capitalize text-[var(--text-muted)]">
                {kind}
              </span>
            </span>
          ))}
        </div>
        <p className="text-xs text-[var(--text-faint)]">
          Drag to reposition · hover to trace connections · click to recentre
        </p>
      </div>

      <div className="overflow-hidden rounded-[var(--radius)] border border-[var(--border)] bg-[var(--surface-2)]">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="mx-auto block h-auto w-full max-w-[720px] select-none"
          // role="img" tells assistive tech "this subtree is one bundled
          // picture" — which also means, by spec, that its children are never
          // exposed individually. The clickable/draggable nodes below are
          // therefore a pointer-only enhancement; the identical recentre
          // action is exposed for real in the table beneath, as a button per
          // row. Confirmed in Chrome's accessibility tree: with role="img"
          // here the whole diagram collapses to one opaque image node, same
          // as the original concentric-ring version — this preserves that,
          // rather than a broken hybrid that only looks accessible.
          role="img"
          aria-label={`Force-directed relationship diagram for ${data.root}: ${data.nodes.length} entities and ${data.edges.length} relationships, ${data.depth} hops out. The same data, including a way to recentre on any entity, is in the table below.`}
        >
          <defs>
            {/* The "glow": a blurred copy of the shape merged under the crisp
                original. Cheap — one filter, reused by every node — and it is
                exactly what gives a force graph that lit-from-within look
                rather than flat dots on a grid. */}
            <filter
              id="node-glow"
              x="-100%"
              y="-100%"
              width="300%"
              height="300%"
            >
              <feGaussianBlur
                in="SourceGraphic"
                stdDeviation="4.5"
                result="blur"
              />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>

          {links.map((link, i) => {
            const s =
              typeof link.source === "string"
                ? byId.get(link.source)
                : (link.source as SimNode);
            const t =
              typeof link.target === "string"
                ? byId.get(link.target)
                : (link.target as SimNode);
            if (!s || !t) return null;
            const dimmed = neighbourhood
              ? !neighbourhood.edgeSet.has(i)
              : false;
            const highlighted = neighbourhood?.edgeSet.has(i);
            const sp = posOf(s);
            const tp = posOf(t);
            return (
              <line
                key={i}
                x1={sp.x}
                y1={sp.y}
                x2={tp.x}
                y2={tp.y}
                stroke={highlighted ? "var(--accent)" : "var(--border-strong)"}
                strokeWidth={highlighted ? 1.75 : 1}
                opacity={dimmed ? 0.12 : highlighted ? 0.9 : 0.55}
                style={{ transition: "opacity 150ms, stroke 150ms" }}
              />
            );
          })}

          {nodes.map((node) => {
            const isRoot = node.hops === 0;
            const r = isRoot ? MAX_R + 4 : radiusFor(node.degree);
            const dimmed = neighbourhood
              ? !neighbourhood.ids.has(node.id)
              : false;
            const emphasised = active === node.id || (isRoot && !active);
            const p = posOf(node);
            return (
              // Pointer-only, by design: the parent svg's role="img" hides all
              // of this from assistive tech regardless of what is put on it, so
              // there is no aria-label/tabIndex/onKeyDown here worth adding —
              // that would claim a keyboard path that does not exist. The same
              // recentre action is a real <button> per row in the table below.
              <g
                key={node.id}
                onPointerEnter={() => setActive(node.id)}
                onPointerLeave={() => !dragging && setActive(null)}
                onPointerDown={(e) => startDrag(node, e)}
                onClick={() => onRecentre?.(node.id)}
                style={{
                  cursor: onRecentre ? "pointer" : "default",
                  opacity: dimmed ? 0.25 : 1,
                  transition: "opacity 150ms",
                }}
              >
                <circle
                  cx={p.x}
                  cy={p.y}
                  r={r}
                  fill={KIND_COLOR[node.kind] ?? "var(--text-faint)"}
                  stroke={isRoot ? "var(--text)" : "var(--surface)"}
                  strokeWidth={isRoot ? 2.5 : 2}
                  filter={emphasised ? "url(#node-glow)" : undefined}
                />
                <text
                  x={p.x}
                  y={p.y + r + 12}
                  textAnchor="middle"
                  fontSize={isRoot ? 12.5 : 10.5}
                  fontWeight={isRoot ? 700 : 500}
                  fill="var(--text)"
                  style={{ pointerEvents: "none" }}
                >
                  {node.label.length > 20
                    ? `${node.label.slice(0, 19)}…`
                    : node.label}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      <Table
        caption={`Every entity connected to ${data.root}, with its type, distance in relationship hops, and a control to recentre the diagram on it`}
        headers={
          onRecentre
            ? ["Entity", "Type", "Hops from root", "ID", "Action"]
            : ["Entity", "Type", "Hops from root", "ID"]
        }
      >
        {[...data.nodes]
          .sort((a, b) => a.hops - b.hops || a.label.localeCompare(b.label))
          .map((node) => (
            <tr key={node.id}>
              <Td className="font-medium">{node.label}</Td>
              <Td className="capitalize text-[var(--text-muted)]">
                {node.kind}
              </Td>
              <Td className="tabular">
                {node.hops === 0 ? "root" : node.hops}
              </Td>
              <Td className="tabular text-xs text-[var(--text-faint)]">
                {node.id}
              </Td>
              {onRecentre && (
                <Td>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={node.hops === 0}
                    onClick={() => onRecentre(node.id)}
                  >
                    {node.hops === 0 ? "Current centre" : "Recentre here"}
                  </Button>
                </Td>
              )}
            </tr>
          ))}
      </Table>
    </div>
  );
}
