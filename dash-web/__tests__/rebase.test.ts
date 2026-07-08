import { describe, expect, it } from "vitest";

import { rebaseWindow } from "@/lib/rebase";

const series = Array.from({ length: 30 }, (_, i) => ({
  date: `2026-06-${String(i + 1).padStart(2, "0")}`,
  Portfolio: i * 1.0, // cumulative %
  SPY: i * 0.5,
}));

describe("rebaseWindow", () => {
  it("ALL passes through untouched", () => {
    expect(rebaseWindow(series, "ALL")).toBe(series);
  });

  it("1W keeps the trailing 5 sessions rebased to 0 at the window start", () => {
    const out = rebaseWindow(series, "1W");
    expect(out).toHaveLength(5);
    // base = session 24 (cum 24%); session 25 rebased: (1.25/1.24 - 1)*100
    expect(out[0].Portfolio).toBeCloseTo(((1.25 / 1.24) - 1) * 100, 3);
    expect(out[4].date).toBe(series[29].date);
  });

  it("short history passes through instead of fabricating a window", () => {
    const short = series.slice(0, 3);
    expect(rebaseWindow(short, "1M")).toBe(short);
  });
});
