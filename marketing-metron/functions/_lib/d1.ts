// functions/_lib/d1.ts — shared Pages Functions helpers for the Metron waitlist stack:
// the minimal D1 surface used by waitlist.ts, funnel.ts and _middleware.ts, plus a
// JSON-response helper and the funnel_daily counter bump.
//
// Declared once here rather than per-file (unlike the original waitlist.ts, which kept
// its D1 types inline) because this is plain TS with no Workers-runtime types — it
// typechecks fine under the DOM-typed Astro program that functions/ shares with src/,
// same as the file it's extracted from.
//
// Lives under _lib/ (leading underscore), not lib/: Cloudflare Pages Functions turns
// every non-underscore-prefixed file under functions/ into a route matching its path —
// a plain lib/d1.ts would ship as a live (and broken, no onRequest export) route.

export interface D1Result {
  success: boolean;
  // INSERT OR IGNORE / UPSERT -> meta.changes is 1 on a written row, 0 otherwise.
  meta?: { changes?: number };
}

export interface D1QueryResult<T> {
  success: boolean;
  results: T[];
}

export interface D1PreparedStatement {
  bind(...values: unknown[]): D1PreparedStatement;
  run(): Promise<D1Result>;
  all<T = Record<string, unknown>>(): Promise<D1QueryResult<T>>;
}

export interface D1Database {
  prepare(query: string): D1PreparedStatement;
}

export function json(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

// UTC calendar day (YYYY-MM-DD) — the funnel_daily primary key. UTC (not the viewer's
// local day) so counts are stable regardless of where a request originates.
export function utcDay(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

// Funnel counters, one row per UTC day (see ../../migrations/0002_*.sql). A fixed
// allowlist of upsert statements — never interpolate a caller-supplied column name
// into SQL.
export type FunnelMetric = "visits" | "waitlist_new" | "waitlist_dup" | "email_sent" | "email_failed";

const FUNNEL_UPSERT: Record<FunnelMetric, string> = {
  visits:
    "INSERT INTO funnel_daily (day, visits) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET visits = visits + 1",
  waitlist_new:
    "INSERT INTO funnel_daily (day, waitlist_new) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET waitlist_new = waitlist_new + 1",
  waitlist_dup:
    "INSERT INTO funnel_daily (day, waitlist_dup) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET waitlist_dup = waitlist_dup + 1",
  email_sent:
    "INSERT INTO funnel_daily (day, email_sent) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET email_sent = email_sent + 1",
  email_failed:
    "INSERT INTO funnel_daily (day, email_failed) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET email_failed = email_failed + 1",
};

// Best-effort counter bump — funnel counting must never break the request it's counting
// (a visit, a signup, an email send). Failures are swallowed (logged), same posture as
// the existing Resend best-effort send.
export async function bumpFunnel(
  db: D1Database,
  metric: FunnelMetric,
  day: string = utcDay(),
): Promise<void> {
  try {
    await db.prepare(FUNNEL_UPSERT[metric]).bind(day).run();
  } catch (err) {
    console.error(`funnel counter bump failed (${metric}):`, err);
  }
}
