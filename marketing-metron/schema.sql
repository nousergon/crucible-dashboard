-- Metron beta waitlist (Cloudflare D1, bound as WAITLIST_DB).
-- One row per interested email; the email is the primary key so re-submits are
-- idempotent (INSERT OR IGNORE). Apply with:
--   npx wrangler d1 execute metron-waitlist --remote --file=./schema.sql
CREATE TABLE IF NOT EXISTS waitlist (
  email      TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  source     TEXT
);

CREATE INDEX IF NOT EXISTS idx_waitlist_created_at ON waitlist (created_at);
