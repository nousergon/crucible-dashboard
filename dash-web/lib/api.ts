import "server-only";

/**
 * Typed client for crucible-dash-api (dash_api/main.py, same box,
 * internal :8506). Response shapes are pinned by tests/test_dash_api.py in
 * the parent repo — a shape change there is a breaking change here.
 *
 * Server-side only: pages are server components; nothing here reaches the
 * browser. `next: { revalidate: 300 }` matches the loaders' S3 TTLs — the
 * page is never fresher than the artifacts anyway.
 */
const API_URL = process.env.DASH_API_URL ?? "http://127.0.0.1:8506";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { next: { revalidate: 300 } });
  if (!res.ok) {
    // Fail loud with the API's own detail — an upstream 503 must surface as
    // an error page, never render as a plausible empty dashboard.
    const detail = await res.text().catch(() => "");
    throw new Error(`dash-api ${path} → ${res.status}: ${detail.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

export interface Stat {
  label: string;
  value: string;
  sub: string;
  help: string;
}

export interface EquityPoint {
  date: string;
  Portfolio: number;
  SPY: number;
}

export interface AlphaPeriod {
  label: string;
  alpha_pct: number;
  n_days: number;
}

export interface SlotBinding {
  slot: string;
  impl: string;
}

export interface Experiment {
  experiment_id: string;
  slots: SlotBinding[];
  report_card_date: string;
  grader_source: string;
  backtest_date: string;
}

export interface IntradayPoint {
  time: string;
  Portfolio: number;
  SPY: number | null;
}

export interface MetricRow {
  metric: string;
  value: string;
  ci: string;
  n: number | string;
  target: number | string;
  red_line: number | string;
  trend: string;
  criticality: string;
  status: string;
  reason: string;
}

export interface Verdict {
  tile: string;
  status: string;
  graded: number;
  total: number;
  reason: string;
}

export interface IntegrityRow {
  check: string;
  status: string;
  detail: string;
  help: string;
}

export interface TrustLeg {
  leg: string;
  repo: string;
  tests: string;
  proves: string;
  caveat: string;
  ci: string;
  verified: string;
  commit: string;
  link: string;
  error: string;
}

export interface Finding {
  date: string;
  found_by: string;
  finding: string;
  fix: string;
}

export const api = {
  experiment: () => get<Experiment>("/api/experiment"),
  intraday: () => get<IntradayPoint[]>("/api/intraday"),
  tile: (key: string) => get<{ tile: string; metrics: MetricRow[] }>(`/api/tiles/${key}`),
  headline: () => get<Stat[]>("/api/headline"),
  equity: () => get<EquityPoint[]>("/api/equity"),
  alphaPeriods: (period: "D" | "W" | "M") =>
    get<AlphaPeriod[]>(`/api/alpha-periods?period=${period}`),
  verdicts: () => get<Verdict[]>("/api/verdicts"),
  integrity: () => get<IntegrityRow[]>("/api/integrity"),
  trust: () => get<{ legs: TrustLeg[]; findings: Finding[] }>("/api/trust"),
};
