import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

const CI_DOT: Record<string, string> = {
  SUCCESS: "bg-positive",
  FAILURE: "bg-negative",
  UNAVAILABLE: "bg-muted",
};

/** Integrity — why these numbers can be trusted (plan §9.3.5): the
 * measurement legs, the validation battery with LIVE CI verdicts, and the
 * findings ledger. Caveats print next to the claims they qualify. */
export default async function IntegrityPage() {
  const [integrity, trust] = await Promise.all([api.integrity(), api.trust()]);

  return (
    <div className="space-y-10">
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-muted">
          Measurement legs — this run
        </h2>
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full text-sm">
            <thead className="bg-surface text-left text-[11px] uppercase tracking-wider text-muted">
              <tr>
                <th className="px-4 py-2.5">Check</th>
                <th className="px-4 py-2.5">Status</th>
                <th className="px-4 py-2.5">Detail</th>
              </tr>
            </thead>
            <tbody>
              {integrity.map((row) => (
                <tr key={row.check} className="border-t border-line" title={row.help}>
                  <td className="px-4 py-2.5">{row.check}</td>
                  <td className="px-4 py-2.5 font-mono text-xs">{row.status}</td>
                  <td className="px-4 py-2.5 font-mono text-xs text-muted">{row.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wider text-muted">
          Validation battery
        </h2>
        <p className="mb-4 max-w-3xl text-sm text-muted">
          Backtests are easy to flatter and graders are easy to game. These named,
          continuously-run checks make that hard here — each vouched for by its repository&apos;s
          live main-branch CI, not by this page&apos;s author.
        </p>
        <div className="space-y-3">
          {trust.legs.map((leg) => (
            <div key={leg.leg} className="rounded-lg border border-line bg-surface px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`h-2 w-2 rounded-full ${CI_DOT[leg.ci] ?? "bg-warn"}`} />
                <span className="text-sm font-semibold">{leg.leg}</span>
                <span className="font-mono text-[11px] text-muted">{leg.repo}</span>
                <span className="ml-auto font-mono text-[11px] text-muted">
                  {leg.ci === "SUCCESS" ? `CI pass · ${leg.commit} · ${leg.verified}` : leg.ci}
                </span>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-muted">{leg.proves}</p>
              {leg.caveat && (
                <p className="mt-1.5 text-xs leading-relaxed text-warn/90">⚠ {leg.caveat}</p>
              )}
            </div>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wider text-muted">
          What the battery has caught
        </h2>
        <p className="mb-3 text-sm text-muted">
          A validation battery that never finds anything is decoration. Each finding links to its
          merged fix.
        </p>
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full text-sm">
            <thead className="bg-surface text-left text-[11px] uppercase tracking-wider text-muted">
              <tr>
                <th className="px-4 py-2.5">Date</th>
                <th className="px-4 py-2.5">Found by</th>
                <th className="px-4 py-2.5">Finding</th>
                <th className="px-4 py-2.5">Fix</th>
              </tr>
            </thead>
            <tbody>
              {trust.findings.map((f) => (
                <tr key={`${f.date}-${f.found_by}`} className="border-t border-line align-top">
                  <td className="whitespace-nowrap px-4 py-2.5 font-mono text-xs">{f.date}</td>
                  <td className="whitespace-nowrap px-4 py-2.5 font-mono text-xs">{f.found_by}</td>
                  <td className="px-4 py-2.5 text-xs leading-relaxed text-muted">{f.finding}</td>
                  <td className="whitespace-nowrap px-4 py-2.5 font-mono text-xs">{f.fix}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="rounded-lg border border-line px-4 py-3 text-xs leading-relaxed text-muted">
        <span className="font-semibold text-ink">What this does not prove:</span> no live-money
        results — everything here is paper-traded and illustrative. Green checks bound the
        engine&apos;s honesty, not the strategy&apos;s edge: a correctly measured strategy can still have
        negative alpha, and this surface shows it when it does.
      </section>
    </div>
  );
}
