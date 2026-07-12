import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

/** About — what ran, exactly (plan §9.3.6): reproducibility before
 * performance. */
export default async function AboutPage() {
  const experiment = await api.experiment();

  return (
    <div className="max-w-3xl space-y-8">
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-muted">
          This experiment
        </h2>
        <div className="rounded-lg border border-line bg-surface px-5 py-4">
          <div className="font-mono text-lg font-semibold">{experiment.experiment_id}</div>
          <div className="mt-1 font-mono text-xs text-muted">
            report card {experiment.report_card_date} · backtest {experiment.backtest_date}
          </div>
          <div className="mt-4 space-y-2">
            {experiment.slots.map((s) => (
              <div key={s.slot} className="flex flex-wrap gap-2 text-sm">
                <span className="font-mono text-xs text-accent">{s.slot}</span>
                <span className="text-muted">{s.impl}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="space-y-3 text-sm leading-relaxed text-muted">
        <p>
          <span className="font-semibold text-ink">Crucible</span> is an experiment harness for
          AI-driven investment research: bring a research orchestration, a prediction model, or an
          execution strategy, and the platform runs it against a leak-free data spine, grades every
          decision, and reports the result with institutional statistics — confidence intervals,
          deflated Sharpe, false-discovery control.
        </p>
        <p>
          The <span className="font-mono text-ink">reference-rate</span> experiment shown here is
          the stock implementation of all three slots, run live since March 2026 — the harness
          grading its own reference strategy, wins and losses alike. Nothing on this surface is
          hand-curated: every figure derives from versioned artifacts, and the grader itself is
          validated (see Integrity).
        </p>
        <p>
          Paper-traded and illustrative only. Not investment advice, and not an offer of any
          security or advisory service.
        </p>
      </section>
    </div>
  );
}
