// POST /api/waitlist — Metron beta waitlist capture (Cloudflare Pages Function).
//
// Writes one row per email to the D1 database bound as WAITLIST_DB (see wrangler.toml
// + schema.sql). Idempotent: re-submitting the same address is a no-op (INSERT OR
// IGNORE on the email primary key). No third-party calls, no cookies — the privacy
// posture of the product holds on its marketing surface too.
//
// Types are declared locally (just the D1 surface used) so the file type-checks under
// the Astro frontend tsconfig without pulling Workers-runtime libs into the DOM-typed
// program; wrangler compiles it for the Workers runtime at deploy.

interface D1Result {
  success: boolean;
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
}

interface RequestContext {
  request: Request;
  env: Env;
}

// Conservative email shape check — the real validation is "we successfully email you".
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_EMAIL_LEN = 254; // RFC 5321 local+domain ceiling

function json(body: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
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

  try {
    await env.WAITLIST_DB.prepare("INSERT OR IGNORE INTO waitlist (email, source) VALUES (?, ?)")
      .bind(email, source)
      .run();
  } catch {
    // Fail loud to the caller (the page surfaces "try again"); never silently drop a signup.
    return json({ error: "Couldn't save your signup — please try again." }, 500);
  }

  return json({ ok: true }, 200);
}
