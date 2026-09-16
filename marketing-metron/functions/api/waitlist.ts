// POST /api/waitlist — Metron beta waitlist capture (Cloudflare Pages Function).
//
// Writes one row per email to the D1 database bound as WAITLIST_DB (see wrangler.toml
// + migrations/). Idempotent: re-submitting the same address is a no-op (INSERT OR
// IGNORE on the email primary key).
//
// On a NEW signup, sends a "you're on the list" confirmation email via the Resend
// REST API (no-reply@nousergon.ai) — making good on the landing copy's "we email you"
// promise (metron-ops#72, flavor (a): the immediate confirmation). Properties:
//   - Best-effort: the D1 row is the source of truth, so a Resend failure NEVER fails
//     the signup (the caller still gets 200). We never block a signup on email.
//   - New-signups-only: INSERT OR IGNORE reports changes=0 for a duplicate address, so
//     a re-submit doesn't re-send (no confirmation spam on repeat submits).
//   - Opt-in by config: the email is sent only when RESEND_API_KEY is bound. Unset →
//     DB-only, exactly as before (no third-party call, privacy posture preserved).
//   - REST API, not the Node SDK — this runs in the Cloudflare Workers runtime.
//
// Two OPTIONAL fields (metron-ops-I305 deliverable 3, segment options per Brian ruling
// 2026-09-15): `segment` ("Which describes you?", a fixed allowlist — an unknown value
// is a 400, a missing one stores NULL) and `currentTool` ("What do you use today to
// check your portfolio?", free text <=500 chars, stored as `current_tool`). Every
// signup and email send also bumps a same-day funnel counter (functions/_lib/d1.ts) —
// new vs duplicate submit, and email sent vs failed — so Stage A exit can quote the
// funnel from one GET /api/funnel call.

import { bumpFunnel, json, type D1Database } from "../_lib/d1";

interface Env {
  WAITLIST_DB: D1Database;
  // Optional: when bound, a confirmation email is sent on new signups. Set it as a
  // Pages secret: `npx wrangler pages secret put RESEND_API_KEY`. Unset → DB-only.
  RESEND_API_KEY?: string;
}

interface RequestContext {
  request: Request;
  env: Env;
}

// Conservative email shape check — the real validation is "we successfully email you".
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_EMAIL_LEN = 254; // RFC 5321 local+domain ceiling
const MAX_CURRENT_TOOL_LEN = 500;

// "Which describes you?" — fixed allowlist (Brian ruling 2026-09-15, metron-ops-I305).
// Stable slugs stored in D1; the landing page <select> values must match these exactly.
const SEGMENTS = ["fire", "active_investor", "index_investor", "day_trader", "other"] as const;
type Segment = (typeof SEGMENTS)[number];

function isSegment(value: unknown): value is Segment {
  return typeof value === "string" && (SEGMENTS as readonly string[]).includes(value);
}

// Sender on the Resend-verified nousergon.ai domain (standing decision, metron-ops#70).
const FROM_ADDRESS = "Metron <no-reply@nousergon.ai>";
const CONFIRMATION_SUBJECT = "You're on the Metron beta waitlist";

// Plain-text + minimal HTML confirmation. Claims-disciplined, no dates promised —
// just "you're on the list, we'll email when a spot opens", matching the landing copy.
function confirmationBody(): { text: string; html: string } {
  const text = [
    "You're on the Metron beta waitlist.",
    "",
    "Metron is opening a small private beta. We'll email you when it opens —",
    "nothing else. No newsletter, no drip, no sharing your address.",
    "",
    "— The Metron team",
    "https://metron.nousergon.ai",
  ].join("\n");

  const html = [
    '<div style="font-family:system-ui,-apple-system,sans-serif;font-size:15px;line-height:1.6;color:#1a1a1a">',
    "<p>You're on the <strong>Metron</strong> beta waitlist.</p>",
    "<p>Metron is opening a small private beta. We'll email you when it opens —",
    "nothing else. No newsletter, no drip, no sharing your address.</p>",
    '<p style="color:#666">— The Metron team<br>',
    '<a href="https://metron.nousergon.ai" style="color:#2563eb">metron.nousergon.ai</a></p>',
    "</div>",
  ].join("\n");

  return { text, html };
}

// Best-effort confirmation send. Returns whether Resend accepted it (2xx) so the caller
// can bump the right funnel counter; throws nothing the caller must handle — failures
// are swallowed (logged) so a Resend outage can't break signups.
async function sendConfirmation(apiKey: string, to: string): Promise<boolean> {
  const { text, html } = confirmationBody();
  try {
    const resp = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        from: FROM_ADDRESS,
        to: [to],
        subject: CONFIRMATION_SUBJECT,
        text,
        html,
      }),
    });
    if (!resp.ok) {
      // Surface the reason in logs (wrangler tail) without leaking it to the caller.
      const detail = await resp.text().catch(() => "");
      console.error(`waitlist confirmation email failed: ${resp.status} ${detail}`);
      return false;
    }
    return true;
  } catch (err) {
    console.error("waitlist confirmation email threw:", err);
    return false;
  }
}

export async function onRequestPost(context: RequestContext): Promise<Response> {
  const { request, env } = context;

  let payload: {
    email?: unknown;
    website?: unknown;
    source?: unknown;
    segment?: unknown;
    currentTool?: unknown;
  };
  try {
    payload = await request.json();
  } catch {
    return json({ error: "Invalid request." }, 400);
  }

  // Honeypot: bots fill the hidden "website" field. Pretend success (200) so we don't
  // teach a scraper what tripped it, but store nothing (and don't count it in the
  // funnel — it isn't a real submit attempt).
  if (typeof payload.website === "string" && payload.website.trim() !== "") {
    return json({ ok: true }, 200);
  }

  const email = typeof payload.email === "string" ? payload.email.trim().toLowerCase() : "";
  if (!email || email.length > MAX_EMAIL_LEN || !EMAIL_RE.test(email)) {
    return json({ error: "Please enter a valid email address." }, 400);
  }

  const source = typeof payload.source === "string" ? payload.source.slice(0, 64) : "landing";

  // segment: optional. Present-but-unknown is a 400 (never silently coerced to
  // "other" or dropped); absent stores NULL.
  let segment: Segment | null = null;
  if (payload.segment !== undefined && payload.segment !== null && payload.segment !== "") {
    if (!isSegment(payload.segment)) {
      return json({ error: "Unrecognized selection for 'Which describes you?'." }, 400);
    }
    segment = payload.segment;
  }

  // currentTool: optional free text, capped at MAX_CURRENT_TOOL_LEN.
  let currentTool: string | null = null;
  if (typeof payload.currentTool === "string") {
    const trimmed = payload.currentTool.trim();
    if (trimmed.length > MAX_CURRENT_TOOL_LEN) {
      return json({ error: "That answer is too long (500 characters max)." }, 400);
    }
    currentTool = trimmed === "" ? null : trimmed;
  }

  let isNewSignup = false;
  try {
    const result = await env.WAITLIST_DB.prepare(
      "INSERT OR IGNORE INTO waitlist (email, source, segment, current_tool) VALUES (?, ?, ?, ?)",
    )
      .bind(email, source, segment, currentTool)
      .run();
    // changes === 1 → a row was inserted (new signup); 0 → the email already existed.
    isNewSignup = (result.meta?.changes ?? 0) > 0;
  } catch {
    // Fail loud to the caller (the page surfaces "try again"); never silently drop a signup.
    return json({ error: "Couldn't save your signup — please try again." }, 500);
  }

  await bumpFunnel(env.WAITLIST_DB, isNewSignup ? "waitlist_new" : "waitlist_dup");

  // Best-effort confirmation, new signups only. Awaited so the email is sent before the
  // Worker is allowed to terminate, but its failure can't change the 200 we return.
  if (isNewSignup && env.RESEND_API_KEY) {
    const sent = await sendConfirmation(env.RESEND_API_KEY, email);
    await bumpFunnel(env.WAITLIST_DB, sent ? "email_sent" : "email_failed");
  }

  return json({ ok: true }, 200);
}
