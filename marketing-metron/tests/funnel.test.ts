import { describe, expect, it } from "vitest";
import { onRequestGet } from "../functions/api/funnel";
import { FakeD1 } from "./fake-d1";

function req(headers: Record<string, string> = {}, qs = ""): Request {
  return new Request(`https://metron.nousergon.ai/api/funnel${qs}`, { headers });
}

describe("GET /api/funnel", () => {
  it("404s when FUNNEL_READ_TOKEN is unset", async () => {
    const db = new FakeD1();
    const res = await onRequestGet({ request: req(), env: { WAITLIST_DB: db } });
    expect(res.status).toBe(404);
  });

  it("404s on a missing or wrong bearer token", async () => {
    const db = new FakeD1();
    const env = { WAITLIST_DB: db, FUNNEL_READ_TOKEN: "secret" };
    const noAuth = await onRequestGet({ request: req(), env });
    expect(noAuth.status).toBe(404);
    const wrongAuth = await onRequestGet({
      request: req({ authorization: "Bearer wrong" }),
      env,
    });
    expect(wrongAuth.status).toBe(404);
  });

  it("returns daily counts with the correct bearer token", async () => {
    const db = new FakeD1();
    db.funnel.set("2026-09-14", {
      day: "2026-09-14",
      visits: 10,
      waitlist_new: 2,
      waitlist_dup: 1,
      email_sent: 2,
      email_failed: 0,
    });
    const env = { WAITLIST_DB: db, FUNNEL_READ_TOKEN: "secret" };
    const res = await onRequestGet({ request: req({ authorization: "Bearer secret" }), env });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; days: Array<{ day: string; visits: number }> };
    expect(body.ok).toBe(true);
    expect(body.days).toEqual([
      { day: "2026-09-14", visits: 10, waitlist_new: 2, waitlist_dup: 1, email_sent: 2, email_failed: 0 },
    ]);
  });

  // metron-ops-I305 deliverable 4: absence must render as NOT-MEASURED, never as zero.
  it("reports a never-recorded metric as not-measured, not as zero", async () => {
    const db = new FakeD1();
    db.funnel.set("2026-09-14", {
      day: "2026-09-14",
      visits: 10,
      waitlist_new: 0,
      waitlist_dup: 0,
      email_sent: 0,
      email_failed: 0,
    });
    const env = { WAITLIST_DB: db, FUNNEL_READ_TOKEN: "secret" };
    const res = await onRequestGet({ request: req({ authorization: "Bearer secret" }), env });
    const body = (await res.json()) as {
      metrics: Record<string, { measured: boolean; window_total: number | null; reason?: string }>;
    };

    expect(body.metrics.visits).toMatchObject({ measured: true, window_total: 10, lifetime_total: 10 });
    // Never recorded -> null + a reason, so nobody quotes "0 waitlist submits" from a
    // counter that may simply never have been wired up.
    expect(body.metrics.waitlist_new.measured).toBe(false);
    expect(body.metrics.waitlist_new.window_total).toBeNull();
    expect(body.metrics.waitlist_new.reason).toMatch(/has ever been recorded/);
    expect(body.metrics.email_sent.measured).toBe(false);
  });

  it("reports a real zero inside the window once the metric has any lifetime record", async () => {
    const db = new FakeD1();
    db.funnel.set("2026-01-02", {
      day: "2026-01-02",
      visits: 3,
      waitlist_new: 1,
      waitlist_dup: 0,
      email_sent: 1,
      email_failed: 0,
    });
    db.funnel.set("2026-09-14", {
      day: "2026-09-14",
      visits: 5,
      waitlist_new: 0,
      waitlist_dup: 0,
      email_sent: 0,
      email_failed: 0,
    });
    const env = { WAITLIST_DB: db, FUNNEL_READ_TOKEN: "secret" };
    const res = await onRequestGet({
      request: req({ authorization: "Bearer secret" }, "?days=1"),
      env,
    });
    const body = (await res.json()) as {
      window_days: number;
      metrics: Record<string, { measured: boolean; window_total: number | null; lifetime_total: number | null; first_recorded: string | null }>;
    };

    expect(body.window_days).toBe(1);
    expect(body.metrics.waitlist_new).toEqual({
      measured: true,
      window_total: 0, // a genuine zero: the counter has recorded before
      lifetime_total: 1,
      first_recorded: "2026-01-02",
    });
  });

  it("answers 503 not-measured when the read fails — never a body that reads as zero", async () => {
    const db = new FakeD1();
    db.failNext = true;
    const env = { WAITLIST_DB: db, FUNNEL_READ_TOKEN: "secret" };
    const res = await onRequestGet({ request: req({ authorization: "Bearer secret" }), env });
    expect(res.status).toBe(503);
    const body = (await res.json()) as { ok: boolean; measured: boolean; status: string };
    expect(body).toMatchObject({ ok: false, measured: false, status: "not-measured" });
  });

  it("caps the days parameter at MAX_DAYS(90) without erroring", async () => {
    const db = new FakeD1();
    const env = { WAITLIST_DB: db, FUNNEL_READ_TOKEN: "secret" };
    const res = await onRequestGet({
      request: req({ authorization: "Bearer secret" }, "?days=99999"),
      env,
    });
    expect(res.status).toBe(200);
  });
});
