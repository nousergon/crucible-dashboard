import type { EquityPoint } from "@/lib/api";

export type Range = "1D" | "1W" | "1M" | "3M" | "ALL";

const TRADING_DAYS: Record<Exclude<Range, "1D" | "ALL">, number> = {
  "1W": 5,
  "1M": 21,
  "3M": 63,
};

/**
 * Slice the cumulative-return series to the trailing window and rebase both
 * legs to 0 at the window start: r_i = (1 + c_i) / (1 + c_base) − 1.
 * A pure display transform of recorded values (same doctrine as the
 * view-model's equity_frame) — no new statistics.
 */
export function rebaseWindow(data: EquityPoint[], range: Range): EquityPoint[] {
  if (range === "ALL" || range === "1D" || data.length === 0) return data;
  const n = TRADING_DAYS[range];
  if (data.length <= n) return data;
  const window = data.slice(-(n + 1)); // include the base session
  const base = window[0];
  const rb = (cum: number, baseCum: number) =>
    Number((((1 + cum / 100) / (1 + baseCum / 100) - 1) * 100).toFixed(4));
  return window.slice(1).map((p) => ({
    date: p.date,
    Portfolio: rb(p.Portfolio, base.Portfolio),
    SPY: rb(p.SPY, base.SPY),
  }));
}
