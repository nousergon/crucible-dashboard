import { describe, expect, it } from "vitest";
import { onRequest } from "../functions/_middleware";
import { FakeD1 } from "./fake-d1";

const next = () => Promise.resolve(new Response("ok"));

describe("landing-page visit middleware", () => {
  it("bumps the visits counter on GET /", async () => {
    const db = new FakeD1();
    await onRequest({
      request: new Request("https://metron.nousergon.ai/"),
      env: { WAITLIST_DB: db },
      next,
    });
    const today = new Date().toISOString().slice(0, 10);
    expect(db.funnel.get(today)?.visits).toBe(1);
  });

  it("does not count a request to another path", async () => {
    const db = new FakeD1();
    await onRequest({
      request: new Request("https://metron.nousergon.ai/api/waitlist"),
      env: { WAITLIST_DB: db },
      next,
    });
    expect(db.funnel.size).toBe(0);
  });

  it("does not count a non-GET request to /", async () => {
    const db = new FakeD1();
    await onRequest({
      request: new Request("https://metron.nousergon.ai/", { method: "POST" }),
      env: { WAITLIST_DB: db },
      next,
    });
    expect(db.funnel.size).toBe(0);
  });

  it("always calls next() and returns its response", async () => {
    const db = new FakeD1();
    const res = await onRequest({
      request: new Request("https://metron.nousergon.ai/"),
      env: { WAITLIST_DB: db },
      next,
    });
    expect(await res.text()).toBe("ok");
  });
});
