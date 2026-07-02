// POST /api/waitlist — Vires beta waitlist capture (Cloudflare Pages Function).
//
// Mirrors marketing-metron/functions/api/waitlist.ts (the fleet's SOTA waitlist
// pattern). Writes one row per email to the D1 database bound as WAITLIST_DB (see
// wrangler.toml + schema.sql). Idempotent: re-submitting the same address is a no-op
// (INSERT OR IGNORE on the email primary key).
//
// On a NEW signup, sends a "you're on the list" confirmation email via the Resend
// REST API (no-reply@nousergon.ai). Properties:
//   - Best-effort: the D1 row is the source of truth, so a Resend failure NEVER fails
//     the signup (the caller still gets 200). We never block a signup on email.
//   - New-signups-only: INSERT OR IGNORE reports changes=0 for a duplicate address, so
//     a re-submit doesn't re-send (no confirmation spam on repeat submits).
//   - Opt-in by config: the email is sent only when RESEND_API_KEY is bound. Unset →
//     DB-only, exactly as before (no third-party call, privacy posture preserved).
//   - REST API, not the Node SDK — this runs in the Cloudflare Workers runtime.
//
// Types are declared locally (just the D1 surface used) so the file type-checks under
// the Astro frontend tsconfig without pulling Workers-runtime libs into the DOM-typed
// program; wrangler compiles it for the Workers runtime at deploy.

interface D1Result {
  success: boolean;
  // INSERT OR IGNORE → meta.changes is 1 on a new row, 0 when the email already existed.
  meta?: { changes?: number };
}

interface D1PreparedStatement {
  bind(...values: unknown[]): D1PreparedStatement;
  run(): Promise<D1Result>;
}

interface D1Database {
  prepare(query: string): D1PreparedStatement;
}

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

// Sender on the Resend-verified nousergon.ai domain (standing decision, metron-ops#70).
const FROM_ADDRESS = "Vires <no-reply@nousergon.ai>";
const CONFIRMATION_SUBJECT = "You're on the Vires beta waitlist";

function json(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

// Plain-text + minimal HTML confirmation. Claims-disciplined, no dates promised —
// just "you're on the list, we'll email when a spot opens", matching the landing copy.
function confirmationBody(): { text: string; html: string } {
  const text = [
    "You're on the Vires beta waitlist.",
    "",
    "Vires is opening a small private beta. We'll email you when it opens —",
    "nothing else. No newsletter, no drip, no sharing your address.",
    "",
    "— The Vires team",
    "https://vires.nousergon.ai",
  ].join("\n");

  const html = [
    '<div style="font-family:system-ui,-apple-system,sans-serif;font-size:15px;line-height:1.6;color:#1a1a1a">',
    "<p>You're on the <strong>Vires</strong> beta waitlist.</p>",
    "<p>Vires is opening a small private beta. We'll email you when it opens —",
    "nothing else. No newsletter, no drip, no sharing your address.</p>",
    '<p style="color:#666">— The Vires team<br>',
    '<a href="https://vires.nousergon.ai" style="color:#059669">vires.nousergon.ai</a></p>',
    "</div>",
  ].join("\n");

  return { text, html };
}

// Best-effort confirmation send. Returns nothing useful and throws nothing the caller
// must handle — failures are swallowed (logged) so a Resend outage can't break signups.
async function sendConfirmation(apiKey: string, to: string): Promise<void> {
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
    }
  } catch (err) {
    console.error("waitlist confirmation email threw:", err);
  }
}

export async function onRequestPost(context: RequestContext): Promise<Response> {
  const { request, env } = context;

  let payload: { email?: unknown; website?: unknown; source?: unknown };
  try {
    payload = await request.json();
  } catch {
    return json({ error: "Invalid request." }, 400);
  }

  // Honeypot: bots fill the hidden "website" field. Pretend success (200) so we don't
  // teach a scraper what tripped it, but store nothing.
  if (typeof payload.website === "string" && payload.website.trim() !== "") {
    return json({ ok: true }, 200);
  }

  const email = typeof payload.email === "string" ? payload.email.trim().toLowerCase() : "";
  if (!email || email.length > MAX_EMAIL_LEN || !EMAIL_RE.test(email)) {
    return json({ error: "Please enter a valid email address." }, 400);
  }

  const source = typeof payload.source === "string" ? payload.source.slice(0, 64) : "landing";

  let isNewSignup = false;
  try {
    const result = await env.WAITLIST_DB.prepare(
      "INSERT OR IGNORE INTO waitlist (email, source) VALUES (?, ?)",
    )
      .bind(email, source)
      .run();
    // changes === 1 → a row was inserted (new signup); 0 → the email already existed.
    isNewSignup = (result.meta?.changes ?? 0) > 0;
  } catch {
    // Fail loud to the caller (the page surfaces "try again"); never silently drop a signup.
    return json({ error: "Couldn't save your signup — please try again." }, 500);
  }

  // Best-effort confirmation, new signups only. Awaited so the email is sent before the
  // Worker is allowed to terminate, but its failure can't change the 200 we return.
  if (isNewSignup && env.RESEND_API_KEY) {
    await sendConfirmation(env.RESEND_API_KEY, email);
  }

  return json({ ok: true }, 200);
}
