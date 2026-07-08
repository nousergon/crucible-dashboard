"use client";

import { useState } from "react";
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

import type { EquityPoint, IntradayPoint } from "@/lib/api";
import { rebaseWindow, type Range } from "@/lib/rebase";

const AXIS = { fontSize: 11, fill: "rgb(138 147 158)" };
const RANGES: Range[] = ["1D", "1W", "1M", "3M", "ALL"];

/** Cumulative return vs SPY with trailing-window ranges — one axis,
 * benchmark always co-plotted (GIPS-flavored presentation, plan §8.3).
 * 1D renders the daemon's intraday path; other ranges slice + rebase the
 * daily ledger series (display transform, no new statistics). */
export function EquityChart({
  data,
  intraday,
}: {
  data: EquityPoint[];
  intraday: IntradayPoint[];
}) {
  const [range, setRange] = useState<Range>("ALL");

  if (!data.length) {
    return (
      <p className="text-sm text-muted">
        No EOD history yet — the curve renders once the ledger has rows.
      </p>
    );
  }

  const isIntraday = range === "1D";
  const series = isIntraday ? intraday : rebaseWindow(data, range);
  const xKey = isIntraday ? "time" : "date";

  return (
    <div>
      <div className="mb-3 inline-flex rounded-md border border-line bg-surface p-0.5 text-xs">
        {RANGES.map((r) => (
          <button
            key={r}
            onClick={() => setRange(r)}
            className={`rounded px-3 py-1 transition-colors ${
              range === r ? "bg-accent/20 text-ink" : "text-muted hover:text-ink"
            }`}
          >
            {r}
          </button>
        ))}
      </div>
      {isIntraday && !intraday.length ? (
        <p className="py-10 text-sm text-muted">
          No intraday session data right now — the 1D path publishes during market hours.
        </p>
      ) : (
        <div className="h-80 w-full">
          <ResponsiveContainer>
            <LineChart data={series} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="rgb(44 50 60)" strokeDasharray="0" vertical={false} />
              <XAxis
                dataKey={xKey}
                tick={AXIS}
                tickLine={false}
                axisLine={{ stroke: "rgb(44 50 60)" }}
                minTickGap={48}
                tickFormatter={(v: string) => (isIntraday ? v.slice(11, 16) : v.slice(0, 10))}
              />
              <YAxis tick={AXIS} tickLine={false} axisLine={false} tickFormatter={(v: number) => `${v.toFixed(1)}%`} width={48} />
              <Tooltip
                contentStyle={{
                  background: "rgb(23 26 32)",
                  border: "1px solid rgb(44 50 60)",
                  borderRadius: 6,
                  fontSize: 12,
                }}
                formatter={(value: number, name: string) => [`${value?.toFixed(2)}%`, name]}
                labelFormatter={(label: string) => (isIntraday ? `${label.slice(0, 16)} ET` : label)}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Line type="monotone" dataKey="SPY" stroke="rgb(138 147 158)" strokeWidth={2} dot={false} connectNulls />
              <Line type="monotone" dataKey="Portfolio" stroke="rgb(87 143 227)" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      {!isIntraday && range !== "ALL" && (
        <p className="mt-1 text-xs text-muted">
          Rebased to 0 at the window start — trailing {range} of trading sessions.
        </p>
      )}
    </div>
  );
}
