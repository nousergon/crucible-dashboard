import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

afterEach(() => vi.restoreAllMocks());

describe("dash-api client", () => {
  it("returns parsed JSON on 200", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([{ label: "x" }]))));
    const body = await api.headline();
    expect(body).toEqual([{ label: "x" }]);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/headline"),
      expect.anything(),
    );
  });

  it("fails loud on upstream 503 — never a plausible empty dashboard", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("RuntimeError: s3 unreachable", { status: 503 })),
    );
    await expect(api.verdicts()).rejects.toThrow(/503: RuntimeError/);
  });
});
