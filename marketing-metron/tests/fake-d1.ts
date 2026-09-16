// functions/__tests__/fake-d1.ts — an in-memory stand-in for the D1 surface declared in
// functions/lib/d1.ts, covering exactly the statements waitlist.ts / funnel.ts /
// _middleware.ts issue (this is not a SQL engine — it pattern-matches the small fixed
// set of queries the app actually sends).

import type { D1Database, D1PreparedStatement, D1QueryResult, D1Result } from "../functions/_lib/d1";

interface WaitlistRow {
  email: string;
  source: string | null;
  segment: string | null;
  current_tool: string | null;
  created_at: number;
}

interface FunnelRow {
  day: string;
  visits: number;
  waitlist_new: number;
  waitlist_dup: number;
  email_sent: number;
  email_failed: number;
}

const FUNNEL_METRIC_COLUMNS = ["waitlist_new", "waitlist_dup", "email_sent", "email_failed", "visits"] as const;

function emptyFunnelRow(day: string): FunnelRow {
  return { day, visits: 0, waitlist_new: 0, waitlist_dup: 0, email_sent: 0, email_failed: 0 };
}

export class FakeD1 implements D1Database {
  waitlist = new Map<string, WaitlistRow>();
  funnel = new Map<string, FunnelRow>();
  // Set true to make the next run()/all() throw, to exercise error paths.
  failNext = false;

  prepare(query: string): D1PreparedStatement {
    const db = this;
    let bound: unknown[] = [];
    const stmt: D1PreparedStatement = {
      bind(...values: unknown[]) {
        bound = values;
        return stmt;
      },
      async run(): Promise<D1Result> {
        if (db.failNext) {
          db.failNext = false;
          throw new Error("simulated D1 failure");
        }
        if (query.includes("INSERT OR IGNORE INTO waitlist")) {
          const [email, source, segment, currentTool] = bound as [string, string, string | null, string | null];
          if (db.waitlist.has(email)) {
            return { success: true, meta: { changes: 0 } };
          }
          db.waitlist.set(email, {
            email,
            source: source ?? null,
            segment: segment ?? null,
            current_tool: currentTool ?? null,
            created_at: Math.floor(Date.now() / 1000),
          });
          return { success: true, meta: { changes: 1 } };
        }
        if (query.includes("INSERT INTO funnel_daily")) {
          const [day] = bound as [string];
          const metric = FUNNEL_METRIC_COLUMNS.find((c) => query.includes(c));
          if (!metric) throw new Error(`fake-d1: could not infer funnel metric from: ${query}`);
          const row = db.funnel.get(day) ?? emptyFunnelRow(day);
          row[metric] += 1;
          db.funnel.set(day, row);
          return { success: true, meta: { changes: 1 } };
        }
        throw new Error(`fake-d1: unhandled run() query: ${query}`);
      },
      async all<T = Record<string, unknown>>(): Promise<D1QueryResult<T>> {
        if (db.failNext) {
          db.failNext = false;
          throw new Error("simulated D1 failure");
        }
        if (query.includes("FROM funnel_daily")) {
          const limit = Number(bound[0] ?? 30);
          const rows = [...db.funnel.values()].sort((a, b) => (a.day < b.day ? 1 : -1)).slice(0, limit);
          return { success: true, results: rows as unknown as T[] };
        }
        throw new Error(`fake-d1: unhandled all() query: ${query}`);
      },
    };
    return stmt;
  }
}
