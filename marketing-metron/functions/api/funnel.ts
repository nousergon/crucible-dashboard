// GET /api/funnel — daily funnel counts, so Stage A exit (metron-ops-I305 deliverable 4)
// can quote landing visits, waitlist submits (new vs duplicate), and confirmation
// emails (sent vs failed) from one request.
//
// Bearer-token protected: FUNNEL_READ_TOKEN must be bound as a Pages secret
// (`npx wrangler pages secret put FUNNEL_READ_TOKEN`) and the caller sends
// `Authorization: Bearer <token>`. Unset env var OR a wrong/missing token both answer
// 404 — the same response either way, so the route never confirms to a prober that a
// token exists to guess against.
//
// NOT-MEASURED, NEVER ZERO (metron-ops-I305 deliverable 4, and principles.md §2.7: a
// component emitting nothing is unobserved, not healthy). A counter that has never
// recorded a single event is indistinguishable from a counter that was never wired up,
// so it reports `measured: false` with a null total and a reason — it does NOT report
// 0. Once a metric has any lifetime record, a zero inside the requested window is a
// real zero and is reported as one. A failed read answers 503 with the same
// not-measured shape rather than a body a reader could quote as zero.

import { type D1Database, json } from "../_lib/d1";

interface Env {
  WAITLIST_DB: D1Database;
  FUNNEL_READ_TOKEN?: string;
}

interface RequestContext {
  request: Request;
  env: Env;
}

interface FunnelRow {
  day: string;
  visits: number;
  waitlist_new: number;
  waitlist_dup: number;
  email_sent: number;
  email_failed: number;
}

type Metric = Exclude<keyof FunnelRow, "day">;

const METRICS: Metric[] = ["visits", "waitlist_new", "waitlist_dup", "email_sent", "email_failed"];

const DEFAULT_DAYS = 30;
const MAX_DAYS = 90;
// Upper bound on rows read in the single query below: one row per UTC day, so ten years
// of history. Bounded so a runaway table can never make this endpoint the slow path.
const MAX_ROWS = 3650;

interface MetricReport {
  measured: boolean;
  window_total: number | null;
  lifetime_total: number | null;
  first_recorded: string | null;
  reason?: string;
}

// Built per request: the Workers runtime rejects constructing a Response during
// global-scope evaluation ("Disallowed operation called within global scope"), and a
// shared Response object would also hand out an already-consumed body.
function notFound(): Response {
  return new Response("Not found.", { status: 404 });
}

function reportMetric(metric: Metric, rows: FunnelRow[], windowDays: string[]): MetricReport {
  const lifetime = rows.reduce((sum, r) => sum + (r[metric] ?? 0), 0);
  const firstRecorded = [...rows].reverse().find((r) => (r[metric] ?? 0) > 0)?.day ?? null;

  if (lifetime === 0) {
    return {
      measured: false,
      window_total: null,
      lifetime_total: null,
      first_recorded: null,
      reason: `no ${metric} has ever been recorded — the counter is unverified, not zero`,
    };
  }

  const inWindow = new Set(windowDays);
  const windowTotal = rows
    .filter((r) => inWindow.has(r.day))
    .reduce((sum, r) => sum + (r[metric] ?? 0), 0);

  return {
    measured: true,
    window_total: windowTotal,
    lifetime_total: lifetime,
    first_recorded: firstRecorded,
  };
}

export async function onRequestGet(context: RequestContext): Promise<Response> {
  const { request, env } = context;

  if (!env.FUNNEL_READ_TOKEN) {
    return notFound();
  }

  const auth = request.headers.get("authorization") ?? "";
  if (auth !== `Bearer ${env.FUNNEL_READ_TOKEN}`) {
    return notFound();
  }

  const url = new URL(request.url);
  const requested = Number.parseInt(url.searchParams.get("days") ?? "", 10);
  const days =
    Number.isFinite(requested) && requested > 0 ? Math.min(requested, MAX_DAYS) : DEFAULT_DAYS;

  try {
    // ONE query serves both the window and the lifetime view: the table holds one row
    // per UTC day, so reading it whole is cheap and lets "never recorded" be told apart
    // from "zero in this window" without a second round trip.
    const result = await env.WAITLIST_DB.prepare(
      `SELECT day, visits, waitlist_new, waitlist_dup, email_sent, email_failed
       FROM funnel_daily
       ORDER BY day DESC
       LIMIT ?`
    )
      .bind(MAX_ROWS)
      .all<FunnelRow>();

    const rows = result.results ?? [];
    const windowRows = rows.slice(0, days);
    const windowDays = windowRows.map((r) => r.day);

    const metrics: Record<string, MetricReport> = {};
    for (const metric of METRICS) {
      metrics[metric] = reportMetric(metric, rows, windowDays);
    }

    return json(
      {
        ok: true,
        window_days: days,
        // Every metric reports its own measured/not-measured state; a reader quoting
        // these numbers can tell an unwired counter from a genuine zero.
        metrics,
        // Per-day detail for the requested window, newest first (unchanged shape).
        days: windowRows,
      },
      200
    );
  } catch (err) {
    console.error("GET /api/funnel query failed:", err);
    // 503 + the not-measured shape: a read that failed must never be rendered as zero
    // or as green by whatever is quoting it.
    return json(
      {
        ok: false,
        measured: false,
        status: "not-measured",
        reason: "could not read funnel counts from D1",
      },
      503
    );
  }
}
