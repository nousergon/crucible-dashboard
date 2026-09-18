import { beforeEach, describe, expect, it, vi } from "vitest";
import { onRequestPost } from "../functions/api/waitlist";
import { FakeD1 } from "./fake-d1";

function req(body: Record<string, unknown>): Request {
  return new Request("https://metron.nousergon.ai/api/waitlist", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("POST /api/waitlist", () => {
  let db: FakeD1;

  beforeEach(() => {
    db = new FakeD1();
    vi.stubGlobal("fetch", vi.fn());
  });

  it("rejects an invalid email", async () => {
    const res = await onRequestPost({ request: req({ email: "not-an-email" }), env: { WAITLIST_DB: db } });
    expect(res.status).toBe(400);
    expect(db.waitlist.size).toBe(0);
  });

  it("stores a new signup with no segment/currentTool (both null)", async () => {
    const res = await onRequestPost({ request: req({ email: "a@example.com" }), env: { WAITLIST_DB: db } });
    expect(res.status).toBe(200);
    const row = db.waitlist.get("a@example.com");
    expect(row?.segment).toBeNull();
    expect(row?.current_tool).toBeNull();
  });

  it("stores an allowed segment and free-text current tool", async () => {
    const res = await onRequestPost({
      request: req({ email: "b@example.com", segment: "fire", currentTool: "a spreadsheet" }),
      env: { WAITLIST_DB: db },
    });
    expect(res.status).toBe(200);
    const row = db.waitlist.get("b@example.com");
    expect(row?.segment).toBe("fire");
    expect(row?.current_tool).toBe("a spreadsheet");
  });

  it("rejects an unrecognized segment value (400, nothing stored)", async () => {
    const res = await onRequestPost({
      request: req({ email: "c@example.com", segment: "day_trading_bot" }),
      env: { WAITLIST_DB: db },
    });
    expect(res.status).toBe(400);
    expect(db.waitlist.has("c@example.com")).toBe(false);
  });

  it("rejects a currentTool longer than 500 characters", async () => {
    const res = await onRequestPost({
      request: req({ email: "d@example.com", currentTool: "x".repeat(501) }),
      env: { WAITLIST_DB: db },
    });
    expect(res.status).toBe(400);
    expect(db.waitlist.has("d@example.com")).toBe(false);
  });

  it("is idempotent on repeat submits (INSERT OR IGNORE) and counts a duplicate in the funnel", async () => {
    const email = "e@example.com";
    await onRequestPost({ request: req({ email }), env: { WAITLIST_DB: db } });
    const res2 = await onRequestPost({ request: req({ email }), env: { WAITLIST_DB: db } });
    expect(res2.status).toBe(200);
    expect(db.waitlist.size).toBe(1);

    const today = new Date().toISOString().slice(0, 10);
    const funnel = db.funnel.get(today);
    expect(funnel?.waitlist_new).toBe(1);
    expect(funnel?.waitlist_dup).toBe(1);
  });

  it("drops honeypot submissions silently, without a funnel count", async () => {
    const res = await onRequestPost({
      request: req({ email: "bot@example.com", website: "http://spam.example" }),
      env: { WAITLIST_DB: db },
    });
    expect(res.status).toBe(200);
    expect(db.waitlist.has("bot@example.com")).toBe(false);
    const today = new Date().toISOString().slice(0, 10);
    expect(db.funnel.get(today)).toBeUndefined();
  });

  it("counts email_sent on a 2xx Resend response, only for new signups", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(new Response("{}", { status: 200 }));
    await onRequestPost({
      request: req({ email: "f@example.com" }),
      env: { WAITLIST_DB: db, RESEND_API_KEY: "test-key" },
    });
    const today = new Date().toISOString().slice(0, 10);
    expect(db.funnel.get(today)?.email_sent).toBe(1);
    expect(db.funnel.get(today)?.email_failed).toBe(0);
  });

  it("counts email_failed on a non-2xx Resend response, and still returns 200", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(new Response("boom", { status: 500 }));
    const res = await onRequestPost({
      request: req({ email: "g@example.com" }),
      env: { WAITLIST_DB: db, RESEND_API_KEY: "test-key" },
    });
    expect(res.status).toBe(200);
    const today = new Date().toISOString().slice(0, 10);
    expect(db.funnel.get(today)?.email_failed).toBe(1);
  });

  it("stores the Resend message id on the waitlist row (metron-ops-I332 attribution)", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ id: "msg_returned_by_resend" }), { status: 200 }),
    );
    await onRequestPost({
      request: req({ email: "i@example.com" }),
      env: { WAITLIST_DB: db, RESEND_API_KEY: "test-key" },
    });
    expect(db.waitlist.get("i@example.com")?.resend_message_id).toBe("msg_returned_by_resend");
  });

  it("does not send or count email on a duplicate submit", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const email = "h@example.com";
    await onRequestPost({ request: req({ email }), env: { WAITLIST_DB: db, RESEND_API_KEY: "test-key" } });
    fetchMock.mockClear();
    await onRequestPost({ request: req({ email }), env: { WAITLIST_DB: db, RESEND_API_KEY: "test-key" } });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
