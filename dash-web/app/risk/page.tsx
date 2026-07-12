import { StatCard } from "@/components/stat-card";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

/** Risk & Attribution — the "deliverable 2 tail" (config#1973 §9.3): the
 * execution-sim risk evidence (trigger timing, exit quality, and the risk
 * guard's shadow-book counterfactual) alongside the sub-score → outcome
 * attribution with its BH-FDR verdict. Both surfaces read from endpoints
 * already vetted through the API audience split (/api/execution, /api/
 * attribution) — this page renders them, it computes nothing. Absent
 * artifacts render as honest empty states, never a fabricated blank. */
export default async function RiskPage() {
  const [execution, attribution] = await Promise.all([
    api.execution(),
    api.attribution(),
  ]);

  const hasExecutionDetail =
    execution.triggers.length > 0 ||
    execution.exit_rules.length > 0 ||
    execution.shadow_classification.length > 0;

  return (
    <div className="space-y-10">
      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wider text-muted">
          Execution & risk-guard — this run
        </h2>
        <p className="mb-4 max-w-3xl text-sm text-muted">
          How well the intraday daemon timed entries and exits, and whether the risk guard
          added or destroyed value in the shadow-book counterfactual. Recorded values only —
          exit-timing quality and guard lift, never a grade.
        </p>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {execution.headline.map((stat) => (
            <StatCard key={stat.label} stat={stat} />
          ))}
        </div>
      </section>

      {execution.triggers.length > 0 && (
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-muted">
            Trigger timing — slippage vs signal &amp; open
          </h2>
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <thead className="bg-surface text-left text-[11px] uppercase tracking-wider text-muted">
                <tr>
                  <th className="px-4 py-2.5">Trigger</th>
                  <th className="px-4 py-2.5">Trades</th>
                  <th className="px-4 py-2.5">Slippage vs signal</th>
                  <th className="px-4 py-2.5">Slippage vs open</th>
                  <th className="px-4 py-2.5">Win rate vs SPY</th>
                </tr>
              </thead>
              <tbody>
                {execution.triggers.map((t) => (
                  <tr key={t.trigger} className="border-t border-line">
                    <td className="px-4 py-2.5">{t.trigger}</td>
                    <td className="px-4 py-2.5 font-mono text-xs">{t.n_trades}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{t.slippage_vs_signal}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{t.slippage_vs_open}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{t.win_rate_vs_spy}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {execution.exit_rules.length > 0 && (
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-muted">
            Exit-rule quality — MFE / MAE / capture
          </h2>
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <thead className="bg-surface text-left text-[11px] uppercase tracking-wider text-muted">
                <tr>
                  <th className="px-4 py-2.5">Exit rule</th>
                  <th className="px-4 py-2.5">N</th>
                  <th className="px-4 py-2.5">Avg MFE</th>
                  <th className="px-4 py-2.5">Avg MAE</th>
                  <th className="px-4 py-2.5">Avg realized</th>
                  <th className="px-4 py-2.5">Avg capture</th>
                </tr>
              </thead>
              <tbody>
                {execution.exit_rules.map((e) => (
                  <tr key={e.exit_type} className="border-t border-line">
                    <td className="px-4 py-2.5">{e.exit_type}</td>
                    <td className="px-4 py-2.5 font-mono text-xs">{e.n}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{e.avg_mfe}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{e.avg_mae}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{e.avg_realized}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{e.avg_capture}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {execution.shadow_classification.length > 0 && (
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-muted">
            Risk-guard shadow book — did blocking help?
          </h2>
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full max-w-xl text-sm">
              <tbody>
                {execution.shadow_classification.map((row) => (
                  <tr key={row.measure} className="border-t border-line first:border-t-0">
                    <td className="px-4 py-2.5 text-muted">{row.measure}</td>
                    <td className="px-4 py-2.5 text-right font-mono text-xs">{row.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wider text-muted">
          Attribution — which sub-scores tracked the outcome
        </h2>
        <p className="mb-4 max-w-3xl text-sm text-muted">
          Univariate correlation of each research sub-score with the primary-horizon beat-SPY
          outcome, with a Benjamini–Hochberg FDR verdict so multiple-comparison luck is not
          mistaken for signal. Ranked by absolute correlation.
        </p>
        {attribution.length > 0 ? (
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <thead className="bg-surface text-left text-[11px] uppercase tracking-wider text-muted">
                <tr>
                  <th className="px-4 py-2.5">Sub-score</th>
                  <th className="px-4 py-2.5">Target</th>
                  <th className="px-4 py-2.5">Correlation</th>
                  <th className="px-4 py-2.5">FDR-significant</th>
                </tr>
              </thead>
              <tbody>
                {attribution.map((row) => (
                  <tr key={row.sub_score} className="border-t border-line">
                    <td className="px-4 py-2.5">{row.sub_score}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{row.target}</td>
                    <td
                      className={`px-4 py-2.5 font-mono text-xs ${
                        row.correlation < 0 ? "text-negative" : "text-ink"
                      }`}
                    >
                      {row.correlation >= 0 ? "+" : ""}
                      {row.correlation.toFixed(3)}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs">
                      {row.fdr_significant ? (
                        <span className="text-positive">yes</span>
                      ) : (
                        <span className="text-muted">no</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="rounded-lg border border-line bg-surface px-4 py-3 text-sm text-muted">
            No attribution artifact for this run yet — sub-score → outcome correlations appear
            once the backtester emits <span className="font-mono text-xs">attribution.json</span>.
          </p>
        )}
      </section>

      {!hasExecutionDetail && (
        <section className="rounded-lg border border-line bg-surface px-4 py-3 text-sm text-muted">
          No execution-sim detail (triggers, exit rules, shadow book) for this run yet — the
          headline strip above shows the honest ABSENT state until the intraday daemon and
          exit-timing artifacts are emitted.
        </section>
      )}

      <section className="rounded-lg border border-line px-4 py-3 text-xs leading-relaxed text-muted">
        <span className="font-semibold text-ink">What this does not prove:</span> execution and
        attribution are measured on paper-traded, illustrative results only. A sub-score that
        correlated with past outcomes need not predict future ones, and a risk guard that helped
        in the shadow book need not help live — these are diagnostics of the engine&apos;s
        behavior, not a claim of edge.
      </section>
    </div>
  );
}
