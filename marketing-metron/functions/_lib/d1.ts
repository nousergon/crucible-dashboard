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

// Funnel counters, one row per UTC day (see ../../migrations/0002_*.sql,
// ../../migrations/0003_delivery_events.sql). A fixed allowlist of upsert statements —
// never interpolate a caller-supplied column name into SQL.
//
// email_sent/email_failed (waitlist.ts) record what Resend's REST API answered on send
// (send-accepted). email_delivered/email_bounced (resend-webhook.ts, metron-ops-I332)
// record what Resend's delivery webhook later reported — a different fact: a message
// can be send-accepted and never delivered, or delivered well after the send count was
// bumped.
export type FunnelMetric =
  | "visits"
  | "waitlist_new"
  | "waitlist_dup"
  | "email_sent"
  | "email_failed"
  | "email_delivered"
  | "email_bounced";

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
  email_delivered:
    "INSERT INTO funnel_daily (day, email_delivered) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET email_delivered = email_delivered + 1",
  email_bounced:
    "INSERT INTO funnel_daily (day, email_bounced) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET email_bounced = email_bounced + 1",
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

// Records the Resend message id returned by a confirmation send, so a later delivery
// webhook (keyed on that id, never on our email address) can find the row it belongs
// to (metron-ops-I332 deliverable 5). Best-effort, same posture as bumpFunnel — losing
// this write only degrades attribution, it never changes whether the signup succeeded.
export async function recordMessageId(db: D1Database, email: string, messageId: string): Promise<void> {
  try {
    await db.prepare("UPDATE waitlist SET resend_message_id = ? WHERE email = ?").bind(messageId, email).run();
  } catch (err) {
    console.error("recording resend_message_id failed:", err);
  }
}

export type DeliveryStatus = "delivered" | "bounced" | "complained" | "delivery_delayed";

// Applies a verified delivery-webhook event to the waitlist row it names (by
// resend_message_id), NOT best-effort: a delivery event that fails to persist must
// raise, not disappear — the row this call could not find or could not write is
// returned to the caller, which decides how to surface it (never a silent swallow on
// a PRODUCER of the delivery record itself).
export async function recordDeliveryEvent(
  db: D1Database,
  messageId: string,
  status: DeliveryStatus,
  eventId: string,
  recordedAt: number,
): Promise<{ matched: boolean }> {
  const result = await db
    .prepare(
      "UPDATE waitlist SET delivery_status = ?, delivery_event_id = ?, delivered_at = ? WHERE resend_message_id = ?",
    )
    .bind(status, eventId, recordedAt, messageId)
    .run();
  return { matched: (result.meta?.changes ?? 0) > 0 };
}

// Idempotency guard: Resend (via Svix) may redeliver the same webhook event. Returns
// true the first time an event_id is seen (caller should apply it), false on a
// redelivery (caller returns 200 without reapplying — never a double funnel bump).
export async function markWebhookEventProcessed(db: D1Database, eventId: string, eventType: string): Promise<boolean> {
  const result = await db
    .prepare("INSERT OR IGNORE INTO processed_webhook_events (event_id, event_type) VALUES (?, ?)")
    .bind(eventId, eventType)
    .run();
  return (result.meta?.changes ?? 0) > 0;
}
