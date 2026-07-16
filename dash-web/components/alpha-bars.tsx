"use client";

import { useState } from "react";
import {
  Bar,
  BarChart,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { AlphaPeriod } from "@/lib/api";

const PERIODS = [
  { key: "D", label: "Daily" },
  { key: "W", label: "Weekly" },
  { key: "M", label: "Monthly" },
] as const;

const AXIS = { fontSize: 11, fill: "rgb(138 147 158)" };

/** Alpha dissection since inception — per-period sums of the recorded daily
 * ledger; green/red by sign; trend adjudication stays the evaluator's job. */
export function AlphaBars({
  byPeriod,
}: {
  byPeriod: Record<"D" | "W" | "M", AlphaPeriod[]>;
}) {
  const [period, setPeriod] = useState<"D" | "W" | "M">("W");
  const data = byPeriod[period] ?? [];
  return (
    <div>
      <div className="mb-3 inline-flex rounded-md border border-line bg-surface p-0.5 text-xs">
        {PERIODS.map((p) => (
          <button
            key={p.key}
            onClick={() => setPeriod(p.key)}
            className={`rounded px-3 py-1 transition-colors ${
              period === p.key ? "bg-accent/20 text-ink" : "text-muted hover:text-ink"
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>
      {data.length === 0 ? (
        <p className="text-sm text-muted">No ledger history for this period yet.</p>
      ) : (
        <div className="h-64 w-full">
          <ResponsiveContainer>
            <BarChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <XAxis
                dataKey="label"
                tick={AXIS}
                tickLine={false}
                axisLine={{ stroke: "rgb(44 50 60)" }}
                minTickGap={40}
                tickFormatter={(v: string) => v.slice(0, 10)}
              />
              <YAxis tick={AXIS} tickLine={false} axisLine={false} tickFormatter={(v: number) => `${v.toFixed(1)}%`} width={44} />
              <ReferenceLine y={0} stroke="rgb(138 147 158)" strokeDasharray="3 3" />
              <Tooltip
                contentStyle={{
                  background: "rgb(23 26 32)",
                  border: "1px solid rgb(44 50 60)",
                  borderRadius: 6,
                  fontSize: 12,
                }}
                formatter={(value) => {
                  const num = typeof value === "number" ? value : Number(value);
                  return [`${num >= 0 ? "+" : ""}${num.toFixed(2)}%`, "alpha"];
                }}
                labelFormatter={(label, payload) =>
                  `${String(label).slice(0, 10)} · ${payload?.[0]?.payload?.n_days ?? "?"} session(s)`
                }
              />
              <Bar dataKey="alpha_pct" radius={[2, 2, 0, 0]}>
                {data.map((row, i) => (
                  <Cell
                    key={i}
                    fill={row.alpha_pct >= 0 ? "rgb(47 191 111)" : "rgb(230 103 103)"}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
      <p className="mt-2 text-xs text-muted">
        Per-period sums of the recorded daily alpha ledger. Descriptive — statistical trend
        verdicts belong to the evaluator, not this chart.
      </p>
    </div>
  );
}
