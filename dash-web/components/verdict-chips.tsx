import type { Verdict } from "@/lib/api";

const DOT: Record<string, string> = {
  GREEN: "bg-positive",
  WATCH: "bg-warn",
  RED: "bg-negative",
};

/** Grader verdicts on the experiment's components — honest negatives stay
 * visible WITH their reason; performance itself is never a chip (§9.2). */
export function VerdictChips({ verdicts }: { verdicts: Verdict[] }) {
  if (!verdicts.length) {
    return <p className="text-sm text-muted">No graded card published yet.</p>;
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {verdicts.map((v) => (
        <div key={v.tile} className="rounded-lg border border-line bg-surface px-4 py-3">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${DOT[v.status] ?? "bg-muted"}`} />
            <span className="text-sm font-semibold capitalize">{v.tile.replace("_", " ")}</span>
            <span className="ml-auto font-mono text-[11px] text-muted">
              {v.graded}/{v.total}
            </span>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-muted">{v.reason}</p>
        </div>
      ))}
    </div>
  );
}
