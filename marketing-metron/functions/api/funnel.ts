// GET /api/funnel — daily funnel counts, so Stage A exit (metron-ops-I305 deliverable 4)
// can quote landing visits, waitlist submits (new vs duplicate), and confirmation
// emails (sent vs failed) from one request.
//
// Bearer-token protected: FUNNEL_READ_TOKEN must be bound as a Pages secret
// (`npx wrangler pages secret put FUNNEL_READ_TOKEN`) and the caller sends
// `Authorization: Bearer <token>`. Unset env var OR a wrong/missing token both answer
// 404 — the same response either way, so the route never confirms to a prober that a
// token exists to guess against.

import { json, type D1Database } from "../_lib/d1";

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

const DEFAULT_DAYS = 30;
const MAX_DAYS = 90;
// Built per request: the Workers runtime rejects constructing a Response during
// global-scope evaluation ("Disallowed operation called within global scope"), and a
// shared Response object would also hand out an already-consumed body.
function notFound(): Response {
  return new Response("Not found.", { status: 404 });
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
  const days = Number.isFinite(requested) && requested > 0 ? Math.min(requested, MAX_DAYS) : DEFAULT_DAYS;

  try {
    const result = await env.WAITLIST_DB.prepare(
      `SELECT day, visits, waitlist_new, waitlist_dup, email_sent, email_failed
       FROM funnel_daily
       ORDER BY day DESC
       LIMIT ?`,
    )
      .bind(days)
      .all<FunnelRow>();

    return json({ ok: true, days: result.results }, 200);
  } catch (err) {
    console.error("GET /api/funnel query failed:", err);
    return json({ error: "Couldn't read funnel counts." }, 500);
  }
}
