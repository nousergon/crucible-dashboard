import { beforeEach, describe, expect, it } from "vitest";
import { onRequestPost } from "../functions/api/resend-webhook";
import { FakeD1 } from "./fake-d1";

// Not a real credential — a fixture secret built at test time so it can never be
// mistaken for (or scanned as) a live Resend webhook signing key.
const SECRET = `whsec_${btoa("fixture-only-not-a-real-secret")}`;

async function sign(secret: string, id: string, timestamp: string, body: string): Promise<string> {
  const secretB64 = secret.startsWith("whsec_") ? secret.slice("whsec_".length) : secret;
  const binary = atob(secretB64);
  const keyBytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) keyBytes[i] = binary.charCodeAt(i);
  const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${id}.${timestamp}.${body}`));
  const macBytes = new Uint8Array(mac);
  let macBinary = "";
  for (const b of macBytes) macBinary += String.fromCharCode(b);
  return btoa(macBinary);
}

async function req(
  bodyObj: Record<string, unknown>,
  opts: { id?: string; timestamp?: string; signature?: string; secret?: string; skipHeaders?: boolean } = {},
): Promise<Request> {
  const body = JSON.stringify(bodyObj);
  const id = opts.id ?? "msg_test_1";
  const timestamp = opts.timestamp ?? String(Math.floor(Date.now() / 1000));
  const signature = opts.signature ?? `v1,${await sign(opts.secret ?? SECRET, id, timestamp, body)}`;

  const headers: Record<string, string> = { "content-type": "application/json" };
  if (!opts.skipHeaders) {
    headers["svix-id"] = id;
    headers["svix-timestamp"] = timestamp;
    headers["svix-signature"] = signature;
  }
  return new Request("https://metron.nousergon.ai/api/resend-webhook", {
    method: "POST",
    headers,
    body,
  });
}

function delivered(messageId: string) {
  return { type: "email.delivered", created_at: "2026-09-18T00:00:00.000Z", data: { email_id: messageId } };
}

function bounced(messageId: string) {
  return { type: "email.bounced", created_at: "2026-09-18T00:00:00.000Z", data: { email_id: messageId } };
}

describe("POST /api/resend-webhook", () => {
  let db: FakeD1;

  beforeEach(() => {
    db = new FakeD1();
  });

  it("404s when RESEND_WEBHOOK_SECRET is unset", async () => {
    const res = await onRequestPost({ request: await req(delivered("msg_1")), env: { WAITLIST_DB: db } });
    expect(res.status).toBe(404);
  });

  it("401s and records nothing when the svix headers are missing", async () => {
    const res = await onRequestPost({
      request: await req(delivered("msg_1"), { skipHeaders: true }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(res.status).toBe(401);
    expect(db.processedWebhookEvents.size).toBe(0);
  });

  it("401s and records nothing on a bad signature", async () => {
    const res = await onRequestPost({
      request: await req(delivered("msg_1"), { signature: "v1,not-a-real-signature" }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(res.status).toBe(401);
    expect(db.processedWebhookEvents.size).toBe(0);
  });

  it("401s on a signature computed with the wrong secret", async () => {
    const res = await onRequestPost({
      request: await req(delivered("msg_1"), { secret: `whsec_${btoa("a-different-fixture-secret")}` }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(res.status).toBe(401);
  });

  it("401s on a stale timestamp even with a valid signature over it", async () => {
    const staleTimestamp = String(Math.floor(Date.now() / 1000) - 3600); // 1 hour old
    const res = await onRequestPost({
      request: await req(delivered("msg_1"), { timestamp: staleTimestamp }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(res.status).toBe(401);
  });

  it("records a delivered event, quotable by its event id, and bumps email_delivered", async () => {
    db.waitlist.set("a@example.com", {
      email: "a@example.com",
      source: "landing",
      segment: null,
      current_tool: null,
      created_at: 0,
      resend_message_id: "msg_abc",
      delivery_status: null,
      delivery_event_id: null,
      delivered_at: null,
    });
    const res = await onRequestPost({
      request: await req(delivered("msg_abc"), { id: "evt_1" }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(res.status).toBe(200);

    const row = db.waitlist.get("a@example.com");
    expect(row?.delivery_status).toBe("delivered");
    expect(row?.delivery_event_id).toBe("evt_1"); // the quotable delivery event id
    expect(row?.delivered_at).not.toBeNull();

    const today = new Date().toISOString().slice(0, 10);
    expect(db.funnel.get(today)?.email_delivered).toBe(1);
  });

  it("records a bounce distinctly from delivered, never counted as sent", async () => {
    db.waitlist.set("b@example.com", {
      email: "b@example.com",
      source: "landing",
      segment: null,
      current_tool: null,
      created_at: 0,
      resend_message_id: "msg_def",
      delivery_status: null,
      delivery_event_id: null,
      delivered_at: null,
    });
    const res = await onRequestPost({
      request: await req(bounced("msg_def"), { id: "evt_2" }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(res.status).toBe(200);

    const row = db.waitlist.get("b@example.com");
    expect(row?.delivery_status).toBe("bounced");

    const today = new Date().toISOString().slice(0, 10);
    expect(db.funnel.get(today)?.email_bounced).toBe(1);
    expect(db.funnel.get(today)?.email_delivered ?? 0).toBe(0);
  });

  it("ignores an unrecognized event type without error and without a funnel bump", async () => {
    const res = await onRequestPost({
      request: await req({ type: "email.clicked", data: { email_id: "msg_xyz" } }, { id: "evt_3" }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; ignored?: boolean };
    expect(body.ok).toBe(true);
    expect(body.ignored).toBe(true);
    const today = new Date().toISOString().slice(0, 10);
    expect(db.funnel.get(today)?.email_delivered ?? 0).toBe(0);
    expect(db.funnel.get(today)?.email_bounced ?? 0).toBe(0);
  });

  it("is idempotent on a redelivered event id — no double funnel bump", async () => {
    db.waitlist.set("c@example.com", {
      email: "c@example.com",
      source: "landing",
      segment: null,
      current_tool: null,
      created_at: 0,
      resend_message_id: "msg_ghi",
      delivery_status: null,
      delivery_event_id: null,
      delivered_at: null,
    });
    const first = await onRequestPost({
      request: await req(delivered("msg_ghi"), { id: "evt_dup" }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(first.status).toBe(200);

    const second = await onRequestPost({
      request: await req(delivered("msg_ghi"), { id: "evt_dup" }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(second.status).toBe(200);
    const secondBody = (await second.json()) as { ok: boolean; deduped?: boolean };
    expect(secondBody.deduped).toBe(true);

    const today = new Date().toISOString().slice(0, 10);
    expect(db.funnel.get(today)?.email_delivered).toBe(1); // not 2
  });

  it("accepts a delivery event with no matching waitlist row (logs, does not throw)", async () => {
    const res = await onRequestPost({
      request: await req(delivered("msg_unknown"), { id: "evt_4" }),
      env: { WAITLIST_DB: db, RESEND_WEBHOOK_SECRET: SECRET },
    });
    expect(res.status).toBe(200);
    const today = new Date().toISOString().slice(0, 10);
    // Aggregate funnel counter still records the fact even without per-row attribution.
    expect(db.funnel.get(today)?.email_delivered).toBe(1);
  });
});
