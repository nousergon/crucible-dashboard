-- Metron beta waitlist + funnel counters (Cloudflare D1, bound as WAITLIST_DB).
--
-- SOURCE OF TRUTH for schema changes is now migrations/ (wrangler d1 migrations),
-- applied automatically by .github/workflows/deploy-marketing.yml on every push to
-- main that touches marketing-metron/**. This file is the consolidated read-only
-- reference — what the live schema looks like after all migrations — kept for local
-- dev and `wrangler d1 execute --local` bring-up; never edit it in place to add a
-- column, add a new migrations/NNNN_*.sql instead.
--
-- Local bring-up (fresh D1, e.g. `wrangler d1 execute --local`):
--   npx wrangler d1 migrations apply metron-waitlist --local

CREATE TABLE IF NOT EXISTS waitlist (
  email        TEXT PRIMARY KEY,
  created_at   INTEGER NOT NULL DEFAULT (unixepoch()),
  source       TEXT,
  segment      TEXT,  -- "Which describes you?" — fixed allowlist, see functions/api/waitlist.ts
  current_tool TEXT   -- "What do you use today to check your portfolio?" — free text, <=500 chars
);

CREATE INDEX IF NOT EXISTS idx_waitlist_created_at ON waitlist (created_at);

-- One row per UTC day — see functions/lib/d1.ts (bumpFunnel) and functions/api/funnel.ts.
CREATE TABLE IF NOT EXISTS funnel_daily (
  day          TEXT PRIMARY KEY,
  visits       INTEGER NOT NULL DEFAULT 0,
  waitlist_new INTEGER NOT NULL DEFAULT 0,
  waitlist_dup INTEGER NOT NULL DEFAULT 0,
  email_sent   INTEGER NOT NULL DEFAULT 0,
  email_failed INTEGER NOT NULL DEFAULT 0
);
