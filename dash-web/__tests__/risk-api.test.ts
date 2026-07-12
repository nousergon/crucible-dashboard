import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";

afterEach(() => vi.restoreAllMocks());

describe("risk & attribution api", () => {
  it("parses the execution envelope (headline + triggers + exit rules + shadow)", async () => {
    const payload = {
      headline: [{ label: "Win rate", value: "58.0%", sub: "roundtrips > 0", help: "…" }],
      triggers: [
        {
          trigger: "vwap_reclaim",
          n_trades: 12,
          slippage_vs_signal: "+0.04%",
          slippage_vs_open: "-0.02%",
          win_rate_vs_spy: "55.0%",
        },
      ],
      exit_rules: [
        {
          exit_type: "trailing_stop",
          n: 9,
          avg_mfe: "+3.10%",
          avg_mae: "-1.20%",
          avg_realized: "+1.80%",
          avg_capture: "0.58",
        },
      ],
      shadow_classification: [{ measure: "Precision (traded → beat SPY)", value: "60.0%" }],
    };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(payload))));
    const body = await api.execution();
    expect(body).toEqual(payload);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/execution"),
      expect.anything(),
    );
  });

  it("parses attribution rows", async () => {
    const rows = [
      { sub_score: "quant", target: "beat_spy_21d", correlation: 0.12, fdr_significant: true },
    ];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(rows))));
    const body = await api.attribution();
    expect(body).toEqual(rows);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/attribution"),
      expect.anything(),
    );
  });

  it("fails loud on upstream 503 — never a plausible empty risk page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("RuntimeError: s3 unreachable", { status: 503 })),
    );
    await expect(api.execution()).rejects.toThrow(/503: RuntimeError/);
  });
});
