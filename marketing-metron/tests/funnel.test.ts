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
