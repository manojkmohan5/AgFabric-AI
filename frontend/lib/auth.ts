"use client";

/** Auth state.
 *
 * The token lives in localStorage. That is a deliberate trade-off, not an
 * oversight: the backend authenticates with a Bearer header and runs with
 * `allow_credentials=False`, so an httpOnly cookie is not available
 * cross-origin. localStorage is readable by injected script, so the real defence
 * is that no untrusted HTML is ever rendered — every value below is inserted as
 * text by React, never as markup.
 *
 * ponytail: swap to an httpOnly refresh cookie on a same-origin deployment, which
 * is also when token revocation becomes worth adding.
 */

import { create } from "zustand";

const KEY = "agfabric.session";

export type Role = "ops" | "accountant" | "warehouse" | "exec";

export interface Session {
  token: string;
  email: string;
  name: string;
  role: Role;
}

interface AuthState {
  session: Session | null;
  ready: boolean;
  restore: () => void;
  signIn: (session: Session) => void;
  signOut: () => void;
}

export const useAuth = create<AuthState>((set) => ({
  session: null,
  // False until localStorage has been read, so a guard cannot redirect a
  // logged-in user to /login during the first paint.
  ready: false,
  restore: () => {
    try {
      const raw = localStorage.getItem(KEY);
      set({ session: raw ? (JSON.parse(raw) as Session) : null, ready: true });
    } catch {
      set({ session: null, ready: true });
    }
  },
  signIn: (session) => {
    localStorage.setItem(KEY, JSON.stringify(session));
    set({ session });
  },
  signOut: () => {
    localStorage.removeItem(KEY);
    set({ session: null });
  },
}));

/** Roles allowed to see money. Mirrors FINANCE_ROLES on the backend.
 *
 * This only decides what the UI bothers to render — the backend redacts the
 * values regardless, so this is a courtesy, never the control. */
export const FINANCE_ROLES: Role[] = ["accountant", "exec"];

export function canSeeMoney(role: Role | undefined): boolean {
  return role ? FINANCE_ROLES.includes(role) : false;
}

/** Roles allowed to trigger agents. Mirrors require_role("ops", "exec"). */
export function canRunAgents(role: Role | undefined): boolean {
  return role === "ops" || role === "exec";
}
