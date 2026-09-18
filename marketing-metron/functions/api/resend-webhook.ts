// POST /api/resend-webhook — Resend delivery-event webhook (Cloudflare Pages Function).
//
// waitlist.ts records "email_sent" on the Resend REST API answering 2xx to our send
// call — send-accepted, not delivered. metron-ops-I305 deliverable 4 quotes "the
// confirmation email (delivery event id quoted)" as a Stage A exit criterion, which
// send-accepted cannot satisfy: a message Resend accepted and then bounced, or that a
// recipient's provider dropped, reads identically to a delivered one. This endpoint
// records what Resend's webhook (Svix-delivered) actually reports happened to the
// message, so a delivery event id is recorded and quotable, and a bounce is visible as
// `email_bounced` rather than silently counted as sent (metron-ops-I332).
//
// Posture, same shape as funnel.ts's FUNNEL_READ_TOKEN gate:
//   - RESEND_WEBHOOK_SECRET unbound -> 404 (nothing about this route is confirmed to
//     exist to a prober).
//   - Signature missing/invalid -> 401, and NOTHING is recorded — an unverified POST is
//     never trusted, no matter how well-formed its body looks.
//   - A well-formed, verified event of a TYPE we don't track a counter for (e.g.
//     email.complained, email.delivery_delayed) is accepted (200) and logged to
//     processed_webhook_events for the idempotency ledger and audit trail, but bumps no
//     funnel counter — "ignored", never an error.
//   - A redelivered event (same svix-id) is a no-op the second time — INSERT OR IGNORE
//     into processed_webhook_events makes this idempotent without a double funnel bump.
//   - Absence must stay visible: a signup whose delivery event never arrives keeps
//     waitlist.delivery_status NULL forever, distinguishable from 'delivered'/'bounced'
//     — never inferred, never silently counted as delivered.
//
// Verification: Resend signs webhooks the Svix way (svix-id / svix-timestamp /
// svix-signature headers, HMAC-SHA256 over `${id}.${timestamp}.${rawBody}` with the
// base64 payload of a `whsec_...`-prefixed secret). Implemented directly against
// WebCrypto (no svix SDK dependency) since this runs in the Cloudflare Workers runtime.

import {
  bumpFunnel,
  json,
  markWebhookEventProcessed,
  recordDeliveryEvent,
  type D1Database,
  type DeliveryStatus,
} from "../_lib/d1";

interface Env {
  WAITLIST_DB: D1Database;
  RESEND_WEBHOOK_SECRET?: string;
}

interface RequestContext {
  request: Request;
  env: Env;
}

// Webhook event types this endpoint accepts (deliverable 1). Only "delivered" and
// "bounced" map to a funnel_daily counter today (deliverable 3); "complained" and
// "delivery_delayed" are recorded in the idempotency ledger for audit but bump nothing.
const DELIVERY_STATUS_BY_TYPE: Record<string, DeliveryStatus> = {
  "email.delivered": "delivered",
  "email.bounced": "bounced",
  "email.complained": "complained",
  "email.delivery_delayed": "delivery_delayed",
};

// Which delivery statuses have a funnel_daily counter. Deliberately a subset of
// DELIVERY_STATUS_BY_TYPE — adding a new Resend event type here requires an explicit
// migration + column, never an implicit fall-through.
const FUNNEL_METRIC_BY_STATUS: Partial<Record<DeliveryStatus, "email_delivered" | "email_bounced">> = {
  delivered: "email_delivered",
  bounced: "email_bounced",
};

// Svix signature tolerance — reject a timestamp further than this from now, in either
// direction, even with a valid signature (replay protection, per Svix's own
// verification guidance). Generous enough to absorb webhook-delivery retry backoff.
const TIMESTAMP_TOLERANCE_SECONDS = 5 * 60;

function notFound(): Response {
  return new Response("Not found.", { status: 404 });
}

function unauthorized(): Response {
  return new Response("Unauthorized.", { status: 401 });
}

function base64Decode(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function base64Encode(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

// Constant-time string comparison — a timing side-channel on signature comparison lets
// an attacker recover a valid signature byte-by-byte.
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

async function computeSvixSignature(secret: string, id: string, timestamp: string, rawBody: string): Promise<string> {
  const secretB64 = secret.startsWith("whsec_") ? secret.slice("whsec_".length) : secret;
  const keyBytes = base64Decode(secretB64);
  const key = await crypto.subtle.importKey(
    "raw",
    keyBytes.buffer.slice(keyBytes.byteOffset, keyBytes.byteOffset + keyBytes.byteLength) as ArrayBuffer,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signedContent = `${id}.${timestamp}.${rawBody}`;
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(signedContent).buffer as ArrayBuffer);
  return base64Encode(new Uint8Array(mac));
}

// Verifies a Svix-style webhook signature. `svix-signature` is a space-separated list
// of `v1,<base64>` candidates (Svix rotates signing keys, so more than one may be
// present) — a match against ANY candidate is a pass.
async function verifySignature(
  secret: string,
  id: string,
  timestamp: string,
  rawBody: string,
  signatureHeader: string,
): Promise<boolean> {
  const tsSeconds = Number.parseInt(timestamp, 10);
  if (!Number.isFinite(tsSeconds)) return false;
  const nowSeconds = Math.floor(Date.now() / 1000);
  if (Math.abs(nowSeconds - tsSeconds) > TIMESTAMP_TOLERANCE_SECONDS) return false;

  const expected = await computeSvixSignature(secret, id, timestamp, rawBody);
  const candidates = signatureHeader
    .split(" ")
    .map((entry) => entry.split(",")[1])
    .filter((v): v is string => Boolean(v));
  if (candidates.length === 0) return false;
  return candidates.some((candidate) => timingSafeEqual(candidate, expected));
}

interface ResendWebhookPayload {
  type?: unknown;
  data?: { email_id?: unknown };
}

export async function onRequestPost(context: RequestContext): Promise<Response> {
  const { request, env } = context;

  if (!env.RESEND_WEBHOOK_SECRET) {
    return notFound();
  }

  const svixId = request.headers.get("svix-id");
  const svixTimestamp = request.headers.get("svix-timestamp");
  const svixSignature = request.headers.get("svix-signature");
  if (!svixId || !svixTimestamp || !svixSignature) {
    return unauthorized();
  }

  // Read the raw body ONCE, as text — signature verification is over the exact bytes
  // Resend sent; re-serializing a parsed JSON object would not reproduce them.
  const rawBody = await request.text();

  const verified = await verifySignature(env.RESEND_WEBHOOK_SECRET, svixId, svixTimestamp, rawBody, svixSignature);
  if (!verified) {
    return unauthorized();
  }

  let payload: ResendWebhookPayload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    // Verified signature but unparseable body: fail loud rather than silently 200 —
    // this is Resend sending us something we don't understand despite a valid secret.
    return json({ error: "Verified signature, but the body is not valid JSON." }, 400);
  }

  const eventType = typeof payload.type === "string" ? payload.type : "unknown";

  // Idempotency: a redelivered event (same svix-id) is a no-op the second time.
  const isNewEvent = await markWebhookEventProcessed(env.WAITLIST_DB, svixId, eventType);
  if (!isNewEvent) {
    return json({ ok: true, deduped: true }, 200);
  }

  const status = DELIVERY_STATUS_BY_TYPE[eventType];
  if (!status) {
    // An event type outside our accepted set. Recorded above for audit; ignored here
    // without error — this is not a defect, Resend's webhook fires event types beyond
    // the four this endpoint tracks.
    return json({ ok: true, ignored: true, type: eventType }, 200);
  }

  const messageId = typeof payload.data?.email_id === "string" ? payload.data.email_id : null;
  if (messageId) {
    const recordedAt = Math.floor(Date.now() / 1000);
    const { matched } = await recordDeliveryEvent(env.WAITLIST_DB, messageId, status, svixId, recordedAt);
    if (!matched) {
      // No waitlist row carries this resend_message_id — e.g. a confirmation sent
      // before this migration shipped. Not a swallow: logged explicitly, and the
      // funnel counter below still records the fact at the aggregate level even
      // without per-row attribution.
      console.error(`resend-webhook: no waitlist row for resend_message_id=${messageId} (event ${svixId})`);
    }
  } else {
    console.error(`resend-webhook: event ${svixId} (${eventType}) carried no data.email_id`);
  }

  const metric = FUNNEL_METRIC_BY_STATUS[status];
  if (metric) {
    await bumpFunnel(env.WAITLIST_DB, metric);
  }

  return json({ ok: true }, 200);
}
