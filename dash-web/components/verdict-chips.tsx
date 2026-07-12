"use client";

import { useState } from "react";

import type { MetricRow, Verdict } from "@/lib/api";

const DOT: Record<string, string> = {
  GREEN: "bg-positive",
  WATCH: "bg-warn",
  RED: "bg-negative",
};

const STATUS_TEXT: Record<string, string> = {
  GREEN: "text-positive",
  WATCH: "text-warn",
  RED: "text-negative",
};

/** Grader verdicts on the experiment's components — each tile expands to its
 * full MetricRecord table (value · CI · N · target · red-line · trend ·
 * reason). Honest negatives stay visible WITH their reason; performance
 * itself is never a chip (§9.2). */
export function VerdictChips({
  verdicts,
  details,
}: {
  verdicts: Verdict[];
  details: Record<string, MetricRow[]>;
}) {
  const [open, setOpen] = useState<string | null>(null);

  if (!verdicts.length) {
    return <p className="text-sm text-muted">No graded card published yet.</p>;
  }

  const expanded = open ? details[open] ?? [] : [];

  return (
    <div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {verdicts.map((v) => (
          <button
            key={v.tile}
            onClick={() => setOpen(open === v.tile ? null : v.tile)}
            className={`rounded-lg border px-4 py-3 text-left transition-colors ${
              open === v.tile ? "border-accent bg-surface" : "border-line bg-surface hover:border-muted"
            }`}
          >
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${DOT[v.status] ?? "bg-muted"}`} />
              <span className="text-sm font-semibold capitalize">{v.tile.replace("_", " ")}</span>
              <span className="ml-auto font-mono text-[11px] text-muted">
                {v.graded}/{v.total}
              </span>
              <span className="text-[10px] text-muted">{open === v.tile ? "▲" : "▼"}</span>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-muted">{v.reason}</p>
          </button>
        ))}
      </div>

      {open && (
        <div className="mt-3 overflow-x-auto rounded-lg border border-line">
          <table className="w-full text-xs">
            <thead className="bg-surface text-left text-[10px] uppercase tracking-wider text-muted">
              <tr>
                <th className="px-3 py-2">Metric</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Value</th>
                <th className="px-3 py-2">95% CI</th>
                <th className="px-3 py-2">N</th>
                <th className="px-3 py-2">Target</th>
                <th className="px-3 py-2">Red line</th>
                <th className="px-3 py-2">Trend</th>
                <th className="px-3 py-2">Why</th>
              </tr>
            </thead>
            <tbody>
              {expanded.map((m) => (
                <tr key={m.metric} className="border-t border-line align-top">
                  <td className="whitespace-nowrap px-3 py-2 font-mono">{m.metric}</td>
                  <td className={`whitespace-nowrap px-3 py-2 font-mono ${STATUS_TEXT[m.status] ?? "text-muted"}`}>
                    {m.status}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono">{m.value}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-muted">{m.ci}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-muted">{m.n}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-muted">{m.target}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-muted">{m.red_line}</td>
                  <td className="whitespace-nowrap px-3 py-2">{m.trend}</td>
                  <td className="min-w-64 px-3 py-2 leading-relaxed text-muted">{m.reason}</td>
                </tr>
              ))}
              {expanded.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-3 py-3 text-muted">
                    No component detail available for this tile.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
