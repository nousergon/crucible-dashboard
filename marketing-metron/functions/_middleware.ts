// functions/_middleware.ts — server-side landing-page visit counter (metron-ops-I305
// deliverable 4). No third-party tracker, no cookies, no IP storage: a single UPSERT
// bump on funnel_daily per GET request to the root landing page ("/"). Pages Functions
// middleware runs ahead of every request the site serves (static assets included), so
// this is scoped tightly to the one path we mean to count — asset, /dash, and /api/*
// requests pass straight through to `next()` uncounted.
import { bumpFunnel, type D1Database } from "./_lib/d1";

interface Env {
  WAITLIST_DB: D1Database;
}

interface MiddlewareContext {
  request: Request;
  env: Env;
  next: () => Promise<Response>;
}

export async function onRequest(context: MiddlewareContext): Promise<Response> {
  const { request, env, next } = context;

  if (request.method === "GET") {
    const url = new URL(request.url);
    if (url.pathname === "/") {
      await bumpFunnel(env.WAITLIST_DB, "visits");
    }
  }

  return next();
}
