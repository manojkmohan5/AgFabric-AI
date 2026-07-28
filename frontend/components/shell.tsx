"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useEffect } from "react";

import { useAuth } from "@/lib/auth";
import { Loading } from "./ui";

/** Nav items with an app-icon tile each, as in the macOS widget gallery.
 *
 * The tile colours are decorative only — every item is labelled in text beside
 * its tile, so nothing here depends on telling the colours apart. That is why
 * these are plain hexes rather than palette slots: they encode no data and are
 * not subject to the categorical-palette rules. The glyph is white on a
 * saturated fill, which clears contrast on all seven.
 */
const NAV = [
  { href: "/dashboard", label: "Dashboard", glyph: "▤", tile: "#2f7fd0" },
  { href: "/search", label: "AI Search", glyph: "◈", tile: "#7b52c9" },
  { href: "/documents", label: "Documents", glyph: "▣", tile: "#c07a1e" },
  { href: "/graph", label: "Knowledge Graph", glyph: "◍", tile: "#1a8f7a" },
  { href: "/risk", label: "Risk Center", glyph: "▲", tile: "#c0393f" },
  { href: "/agents", label: "Agents", glyph: "◐", tile: "#4a5bc4" },
  { href: "/audit", label: "Audit", glyph: "☰", tile: "#5f7061" },
];

export function Shell({ children }: { children: ReactNode }) {
  const { session, ready, restore, signOut } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!ready) restore();
  }, [ready, restore]);

  useEffect(() => {
    // Only redirect once the stored session has actually been read, otherwise a
    // logged-in user is bounced to /login on first paint.
    if (ready && !session) router.replace("/login");
  }, [ready, session, router]);

  if (!ready || !session) {
    return (
      <div className="grid min-h-dvh place-items-center">
        <Loading label="Checking your session" />
      </div>
    );
  }

  return (
    <div className="flex min-h-dvh flex-col lg:flex-row">
      <header className="sidebar-glass lg:relative border-b border-[var(--border)] lg:sticky lg:top-0 lg:h-dvh lg:w-60 lg:shrink-0 lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between gap-3 px-4 py-3.5 lg:block">
          <Link
            href="/dashboard"
            className="flex items-center gap-2.5 rounded-md font-semibold tracking-tight"
          >
            <span
              aria-hidden="true"
              className="grid h-7 w-7 place-items-center rounded-md text-sm font-bold"
              style={{ background: "var(--accent)", color: "var(--bg)" }}
            >
              A
            </span>
            AgFabric&nbsp;AI
          </Link>

          <div className="lg:mt-5">
            <p className="hidden text-xs text-[var(--text-muted)] lg:block">
              {session.name}
            </p>
            <p className="hidden text-xs capitalize text-[var(--text-faint)] lg:block">
              {session.role}
            </p>
          </div>
        </div>

        {/* Labelled so a screen reader can distinguish this from other nav. */}
        <nav aria-label="Main" className="px-2 pb-3 lg:px-2">
          <ul className="flex gap-1 overflow-x-auto lg:flex-col lg:overflow-visible">
            {NAV.map((item) => {
              const active = pathname === item.href;
              return (
                <li key={item.href} className="shrink-0 lg:shrink">
                  <Link
                    href={item.href}
                    // aria-current is how "you are here" is exposed; the colour
                    // change alone would not be perceivable to everyone.
                    aria-current={active ? "page" : undefined}
                    className="flex items-center gap-2.5 whitespace-nowrap rounded-[10px] px-2 py-1.5 text-sm transition-colors hover:bg-[color-mix(in_oklab,var(--surface-2)_70%,transparent)]"
                    style={
                      active
                        ? {
                            background: "var(--accent-soft)",
                            color: "var(--accent-text)",
                            fontWeight: 600,
                          }
                        : { color: "var(--text-muted)" }
                    }
                  >
                    <span
                      aria-hidden="true"
                      className="nav-tile"
                      style={{ background: item.tile }}
                    >
                      {item.glyph}
                    </span>
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="hidden border-t border-[var(--border)] p-2 lg:absolute lg:bottom-0 lg:block lg:w-60">
          <button
            type="button"
            onClick={() => {
              signOut();
              router.replace("/login");
            }}
            className="w-full rounded-lg px-2.5 py-2 text-left text-sm text-[var(--text-muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
          >
            Sign out
          </button>
        </div>
      </header>

      <main id="main" className="min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-8">
        {children}
      </main>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {/* The description sits on the mesh with no card behind it, so it uses
            the on-canvas token; --text-muted measures 3.71:1 there, under AA. */}
        {description && (
          <p className="text-on-canvas mt-1 max-w-2xl text-sm">{description}</p>
        )}
      </div>
      {action}
    </div>
  );
}
