import { AlphaBars } from "@/components/alpha-bars";
import { EquityChart } from "@/components/equity-chart";
import { StatCard } from "@/components/stat-card";
import { VerdictChips } from "@/components/verdict-chips";
import { api, type MetricRow } from "@/lib/api";

export const dynamic = "force-dynamic";

/** Performance — the tear sheet (plan §9.3.1). Numbers with provenance;
 * the grader's per-component verdicts follow, reasons included. */
export default async function PerformancePage() {
  const [headline, equity, intraday, daily, weekly, monthly, verdicts] = await Promise.all([
    api.headline(),
    api.equity(),
    api.intraday().catch(() => []), // best-effort artifact: absent ≠ error
    api.alphaPeriods("D"),
    api.alphaPeriods("W"),
    api.alphaPeriods("M"),
    api.verdicts(),
  ]);
  // Tile detail for each verdict, fetched server-side so expansion is instant.
  const detailPairs = await Promise.all(
    verdicts.map(async (v) => [v.tile, (await api.tile(v.tile)).metrics] as [string, MetricRow[]]),
  );
  const details = Object.fromEntries(detailPairs);

  return (
    <div className="space-y-10">
      <section>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {headline.map((stat) => (
            <StatCard key={stat.label} stat={stat} />
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wider text-muted">
          Cumulative return vs SPY
        </h2>
        <EquityChart data={equity} intraday={intraday} />
      </section>

      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wider text-muted">
          Alpha over time
        </h2>
        <AlphaBars byPeriod={{ D: daily, W: weekly, M: monthly }} />
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-muted">
          Grader verdicts — this strategy
        </h2>
        <VerdictChips verdicts={verdicts} details={details} />
      </section>
    </div>
  );
}
