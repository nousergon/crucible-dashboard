"use client";

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { EquityPoint } from "@/lib/api";

const AXIS = { fontSize: 11, fill: "rgb(138 147 158)" };

/** Cumulative return vs SPY — one axis, benchmark always co-plotted
 * (GIPS-flavored presentation, plan §8.3). */
export function EquityChart({ data }: { data: EquityPoint[] }) {
  if (!data.length) {
    return (
      <p className="text-sm text-muted">
        No EOD history yet — the curve renders once the ledger has rows.
      </p>
    );
  }
  return (
    <div className="h-80 w-full">
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
          <CartesianGrid stroke="rgb(44 50 60)" strokeDasharray="0" vertical={false} />
          <XAxis dataKey="date" tick={AXIS} tickLine={false} axisLine={{ stroke: "rgb(44 50 60)" }} minTickGap={48} />
          <YAxis tick={AXIS} tickLine={false} axisLine={false} tickFormatter={(v: number) => `${v.toFixed(0)}%`} width={44} />
          <Tooltip
            contentStyle={{
              background: "rgb(23 26 32)",
              border: "1px solid rgb(44 50 60)",
              borderRadius: 6,
              fontSize: 12,
            }}
            formatter={(value: number, name: string) => [`${value.toFixed(2)}%`, name]}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Line type="monotone" dataKey="SPY" stroke="rgb(138 147 158)" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="Portfolio" stroke="rgb(87 143 227)" strokeWidth={2} dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
