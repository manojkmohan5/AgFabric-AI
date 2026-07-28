"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Particles } from "@/components/particles";
import { Button, ErrorNote, Field, inputClass } from "@/components/ui";
import { type Health, get, login } from "@/lib/api";
import { type Role, useAuth } from "@/lib/auth";

const DEMO_USERS = [
  { email: "exec@agfabric.test", role: "Executive — sees financials" },
  { email: "accounting@agfabric.test", role: "Accountant — sees financials" },
  { email: "ops@agfabric.test", role: "Operations — can run agents" },
  { email: "warehouse@agfabric.test", role: "Warehouse — no financials" },
];

export default function LoginPage() {
  const router = useRouter();
  const { session, ready, restore, signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Whether this instance is running the demo dataset. A production instance has
  // no seeded accounts, so listing them would send people to a dead end.
  const [demoMode, setDemoMode] = useState<boolean | null>(null);

  useEffect(() => {
    if (!ready) restore();
  }, [ready, restore]);

  useEffect(() => {
    // /health is unauthenticated, which is why the flag lives there.
    get<Health & { demo_mode?: boolean }>("/health", null)
      .then((h) => {
        const demo = h.demo_mode ?? false;
        setDemoMode(demo);
        // Prefill only where that account actually exists.
        if (demo) setEmail((current) => current || "exec@agfabric.test");
      })
      .catch(() => setDemoMode(false));
  }, []);

  // api.ts redirects here with ?expired=1 after clearing a token the API
  // rejected, so the user is told why they are back rather than guessing.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).has("expired")) {
      setError("Your session ended. Please sign in again.");
    }
  }, []);

  useEffect(() => {
    if (session) router.replace("/dashboard");
  }, [session, router]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = await login(email, password);
      signIn({
        token: result.access_token,
        email: result.user.email,
        name: result.user.name,
        role: result.user.role as Role,
      });
      router.replace("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative min-h-dvh w-full overflow-hidden">
      {/* Drifting particle field. Decorative, so aria-hidden, and it holds still
          under prefers-reduced-motion. */}
      <Particles
        className="absolute inset-0"
        quantity={110}
        ease={20}
        color="#8aa398"
      />

      {/* Three long, soft radial sweeps at -45deg. This is what gives the
          template its depth — light raking across the page rather than a flat
          fill. contain-strict keeps them from affecting layout or scroll. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 isolate overflow-hidden"
        style={{ contain: "strict" }}
      >
        <div className="glow-sweep absolute left-0 top-0 h-[80rem] w-[35rem] -translate-y-[22rem] -rotate-45 rounded-full opacity-70" />
        <div className="glow-sweep-thin absolute left-0 top-0 h-[80rem] w-[15rem] -rotate-45 rounded-full [translate:5%_-50%]" />
        <div className="glow-sweep-thin absolute left-0 top-0 h-[80rem] w-[15rem] -translate-y-[22rem] -rotate-45 rounded-full" />
      </div>

      <div className="relative mx-auto flex min-h-dvh max-w-6xl flex-col justify-center px-4 py-10">
        <div className="mx-auto w-full max-w-sm">
          <div className="mb-6 flex items-center gap-2.5">
            <span
              aria-hidden="true"
              className="grid h-9 w-9 place-items-center rounded-[10px] text-base font-bold"
              style={{ background: "var(--accent)", color: "var(--bg)" }}
            >
              A
            </span>
            <p className="text-lg font-semibold tracking-tight">AgFabric AI</p>
          </div>

          <div className="mb-5 flex flex-col space-y-1">
            <h1 className="text-2xl font-bold tracking-tight">Sign in</h1>
            <p className="text-[var(--text-muted)]">
              Enterprise agricultural intelligence.
            </p>
          </div>

          {/* Translucent + blurred so the particle field shows through without
              ever competing with the inputs for legibility. */}
          <form
            onSubmit={submit}
            className="space-y-4 rounded-2xl border border-[var(--border)] bg-[var(--surface)]/80 p-5 backdrop-blur-xl"
          >
            <Field label="Email" id="email">
              {(props) => (
                <input
                  {...props}
                  type="email"
                  name="email"
                  autoComplete="username"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className={inputClass}
                />
              )}
            </Field>

            <Field label="Password" id="password">
              {(props) => (
                <input
                  {...props}
                  type="password"
                  name="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className={inputClass}
                />
              )}
            </Field>

            {error && <ErrorNote message={error} />}

            <Button type="submit" disabled={busy} className="w-full">
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>

          {demoMode && (
            <div className="mt-4 rounded-2xl border border-[var(--border)] bg-[var(--surface-2)]/70 p-4 backdrop-blur-xl">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-muted)]">
                Demo accounts
              </h2>
              <ul className="mt-2.5 space-y-1.5">
                {DEMO_USERS.map((user) => (
                  <li key={user.email}>
                    <button
                      type="button"
                      onClick={() => setEmail(user.email)}
                      className="w-full rounded-md px-1.5 py-1 text-left text-xs hover:bg-[var(--surface)]"
                    >
                      <span className="font-medium">{user.email}</span>
                      <span className="block text-[var(--text-faint)]">
                        {user.role}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
              <p className="mt-3 text-xs text-[var(--text-faint)]">
                Password is whatever <code>SEED_PASSWORD</code> is set to on the
                API.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
