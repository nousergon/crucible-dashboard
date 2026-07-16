import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

const ACTION_STYLE: Record<string, string> = {
  ENTER: "text-positive",
  EXIT: "text-negative",
  REDUCE: "text-warn",
};

/** Decisions — the deliverable-2 tail of #1973's Decisions page
 * (config#2404). Brian's 2026-07-14 Option-A ruling on the §9.2 audience
 * split: external prosumer users see realized position CHANGES + thesis,
 * never the internal decision-chain / veto telemetry / position sizes
 * that make `order_book_rationale` an ops-detail artifact. /api/decisions
 * already enforces that split server-side — this page renders only what
 * it returns. */
export default async function DecisionsPage() {
  const decisions = await api.decisions();

  return (
    <div className="space-y-10">
      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wider text-muted">
          Recent book decisions
        </h2>
        <p className="mb-4 max-w-3xl text-sm text-muted">
          Realized ENTER / EXIT / REDUCE decisions with the research thesis behind each one.
          Position sizes and the internal decision chain (risk gates, pricing sources, vetoed or
          blocked candidates) are ops-detail and not shown here.
        </p>
        {decisions.length > 0 ? (
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <thead className="bg-surface text-left text-[11px] uppercase tracking-wider text-muted">
                <tr>
                  <th className="px-4 py-2.5">Date</th>
                  <th className="px-4 py-2.5">Ticker</th>
                  <th className="px-4 py-2.5">Action</th>
                  <th className="px-4 py-2.5">Signal</th>
                  <th className="px-4 py-2.5">Score</th>
                  <th className="px-4 py-2.5">Conviction</th>
                  <th className="px-4 py-2.5">Sector rating</th>
                </tr>
              </thead>
              <tbody>
                {decisions.map((d) => (
                  <tr key={`${d.date}-${d.ticker}`} className="border-t border-line">
                    <td className="whitespace-nowrap px-4 py-2.5 font-mono text-xs text-muted">
                      {d.date}
                    </td>
                    <td className="px-4 py-2.5 font-semibold">{d.ticker}</td>
                    <td className={`px-4 py-2.5 font-mono text-xs ${ACTION_STYLE[d.action] ?? ""}`}>
                      {d.action}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{d.thesis.signal}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">
                      {d.thesis.score ?? "—"}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">
                      {d.thesis.conviction ?? "—"}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">
                      {d.thesis.sector_rating}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="rounded-lg border border-line bg-surface px-4 py-3 text-sm text-muted">
            No book decisions yet — ENTER / EXIT / REDUCE rows appear here once the executor's
            next order-book rationale run produces one.
          </p>
        )}
      </section>

      <section className="rounded-lg border border-line px-4 py-3 text-xs leading-relaxed text-muted">
        <span className="font-semibold text-ink">What this does not show:</span> position sizes,
        risk-gate / predictor-veto detail, and the tickers that were considered but not traded —
        those are internal operating detail, not shown on this surface. Paper-traded and
        illustrative only.
      </section>
    </div>
  );
}
