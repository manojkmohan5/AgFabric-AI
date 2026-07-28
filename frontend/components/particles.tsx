"use client";

/** Drifting particle field for the sign-in backdrop.
 *
 * Adapted from the well-known canvas-particles pattern rather than pulled in as
 * a dependency: the original is written against shadcn (`cn`, `@/lib/utils`) and
 * this project has its own `components/ui.tsx` and token set, so vendoring the
 * ~80 lines that matter avoids adding shadcn, radix-slot, cva and lucide-react
 * for one decorative background.
 *
 * Four deliberate changes from the source, each fixing something that would bite
 * in a real app:
 *
 * 1. **The animation loop is cancelled on unmount.** The original calls
 *    `requestAnimationFrame(animate)` with no `cancelAnimationFrame`, so the loop
 *    keeps running after the component leaves the tree — on this app that means a
 *    canvas still painting every frame behind the dashboard, forever.
 * 2. **The mouse position lives in a ref, not state.** The original stores it in
 *    `useState`, so every mousemove re-renders the component (and on this page,
 *    the whole sign-in form with it). A ref gives the same parallax with no
 *    renders at all.
 * 3. **Recycling a particle no longer mutates the array mid-iteration.** The
 *    original splices inside `forEach`, which skips the next element and slowly
 *    leaks particles; this rebuilds the offending entry in place instead.
 * 4. **`prefers-reduced-motion` is honoured** — the field is painted once and
 *    left still. A perpetually animating background is exactly what that setting
 *    exists to stop, and every other animation in this app already respects it.
 */

import { useEffect, useRef } from "react";

interface Particle {
  x: number;
  y: number;
  translateX: number;
  translateY: number;
  size: number;
  alpha: number;
  targetAlpha: number;
  dx: number;
  dy: number;
  magnetism: number;
}

export function Particles({
  quantity = 120,
  color = "#8aa398",
  staticity = 50,
  ease = 20,
  size = 0.4,
  className = "",
}: {
  quantity?: number;
  color?: string;
  staticity?: number;
  ease?: number;
  size?: number;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const mouse = useRef({ x: 0, y: 0 });

  useEffect(() => {
    const container = containerRef.current;
    const canvas = canvasRef.current;
    if (!container || !canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const rgb = hexToRgb(color);
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let particles: Particle[] = [];
    let frame = 0;
    let w = 0;
    let h = 0;

    const spawn = (): Particle => ({
      x: Math.floor(Math.random() * w),
      y: Math.floor(Math.random() * h),
      translateX: 0,
      translateY: 0,
      size: Math.floor(Math.random() * 2) + size,
      // Reduced motion never runs the fade-in loop, so start visible.
      alpha: still ? Number((Math.random() * 0.5 + 0.1).toFixed(2)) : 0,
      targetAlpha: Number((Math.random() * 0.6 + 0.1).toFixed(2)),
      dx: (Math.random() - 0.5) * 0.1,
      dy: (Math.random() - 0.5) * 0.1,
      magnetism: 0.1 + Math.random() * 4,
    });

    const draw = (p: Particle) => {
      ctx.translate(p.translateX, p.translateY);
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${rgb.join(", ")}, ${p.alpha})`;
      ctx.fill();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const resize = () => {
      w = container.offsetWidth;
      h = container.offsetHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      particles = Array.from({ length: quantity }, spawn);
      ctx.clearRect(0, 0, w, h);
      particles.forEach(draw);
    };

    const tick = () => {
      ctx.clearRect(0, 0, w, h);
      for (let i = 0; i < particles.length; i++) {
        const p = particles[i];
        // Fade in from the edges so particles do not pop into existence.
        const edge = Math.min(
          p.x + p.translateX - p.size,
          w - p.x - p.translateX - p.size,
          p.y + p.translateY - p.size,
          h - p.y - p.translateY - p.size,
        );
        const nearEdge = Math.max(0, Math.min(1, edge / 20));
        p.alpha = nearEdge >= 1 ? Math.min(p.targetAlpha, p.alpha + 0.02) : p.targetAlpha * nearEdge;

        p.x += p.dx;
        p.y += p.dy;
        p.translateX += (mouse.current.x / (staticity / p.magnetism) - p.translateX) / ease;
        p.translateY += (mouse.current.y / (staticity / p.magnetism) - p.translateY) / ease;

        // Replace in place. Splicing here would skip the next particle.
        if (p.x < -p.size || p.x > w + p.size || p.y < -p.size || p.y > h + p.size) {
          particles[i] = spawn();
        }
        draw(particles[i]);
      }
      frame = window.requestAnimationFrame(tick);
    };

    const onMouseMove = (e: MouseEvent) => {
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left - w / 2;
      const y = e.clientY - rect.top - h / 2;
      if (Math.abs(x) < w / 2 && Math.abs(y) < h / 2) {
        mouse.current = { x, y };
      }
    };

    resize();
    window.addEventListener("resize", resize);
    if (!still) {
      window.addEventListener("mousemove", onMouseMove);
      frame = window.requestAnimationFrame(tick);
    }

    return () => {
      // The whole point of the rewrite: stop the loop and drop the listeners.
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      window.removeEventListener("mousemove", onMouseMove);
    };
  }, [color, quantity, staticity, ease, size]);

  return (
    <div
      ref={containerRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    >
      <canvas ref={canvasRef} className="h-full w-full" />
    </div>
  );
}

function hexToRgb(hex: string): number[] {
  let value = hex.replace("#", "");
  if (value.length === 3) {
    value = value
      .split("")
      .map((c) => c + c)
      .join("");
  }
  const int = parseInt(value, 16);
  return [(int >> 16) & 255, (int >> 8) & 255, int & 255];
}
