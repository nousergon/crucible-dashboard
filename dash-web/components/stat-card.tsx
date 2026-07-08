import type { Stat } from "@/lib/api";

/** Headline stat: value with provenance sub-line and a hover definition —
 * numbers, never grades (plan §9.2). */
export function StatCard({ stat }: { stat: Stat }) {
  const negative = stat.value.startsWith("-") || stat.value.startsWith("−");
  const isAbsent = stat.value === "—";
  return (
    <div
      className="rounded-lg border border-line bg-surface px-4 py-3"
      title={stat.help}
    >
      <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">
        {stat.label}
      </div>
      <div
        className={`mt-1 font-mono text-2xl font-semibold ${
          isAbsent ? "text-muted" : negative ? "text-negative" : "text-ink"
        }`}
      >
        {stat.value}
      </div>
      <div className="mt-0.5 font-mono text-[11px] text-muted">{stat.sub}</div>
    </div>
  );
}
