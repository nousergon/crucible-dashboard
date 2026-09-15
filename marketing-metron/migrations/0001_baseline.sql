-- Baseline: the waitlist table as it has been live since metron-ops#29 (2026-06).
-- IF NOT EXISTS so applying this migration against the already-live `metron-waitlist`
-- D1 (created by hand per the original README — wrangler d1 migrations tracks
-- "applied" in its own bookkeeping table regardless of whether the DDL had already
-- happened) is a safe no-op.
CREATE TABLE IF NOT EXISTS waitlist (
  email      TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  source     TEXT
);

CREATE INDEX IF NOT EXISTS idx_waitlist_created_at ON waitlist (created_at);
