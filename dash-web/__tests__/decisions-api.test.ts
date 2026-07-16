import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

afterEach(() => vi.restoreAllMocks());

describe("decisions api (config#2404)", () => {
  it("parses curated decision rows", async () => {
    const rows = [
      {
        date: "2026-07-14",
        ticker: "AAPL",
        action: "ENTER",
        thesis: { signal: "ENTER", score: 0.82, conviction: 0.7, sector_rating: "A" },
      },
    ];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(rows))));
    const body = await api.decisions();
    expect(body).toEqual(rows);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/decisions"),
      expect.anything(),
    );
  });

  it("fails loud on upstream 503 — never a plausible empty decisions page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("RuntimeError: s3 unreachable", { status: 503 })),
    );
    await expect(api.decisions()).rejects.toThrow(/503: RuntimeError/);
  });
});
