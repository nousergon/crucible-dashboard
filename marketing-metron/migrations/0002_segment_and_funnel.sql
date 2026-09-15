-- metron-ops-I305 deliverables 3+4: waitlist segment fields + funnel counters.
--
-- `segment` / `current_tool` are both nullable free additions to an already-live table
-- (existing rows get NULL, no backfill needed). Values for `segment` are validated in
-- functions/api/waitlist.ts against a fixed allowlist (fire / active_investor /
-- index_investor / day_trader / other) — the column itself stays a plain TEXT so a
-- schema change is never needed to add another option; the application enforces the set.
ALTER TABLE waitlist ADD COLUMN segment TEXT;
ALTER TABLE waitlist ADD COLUMN current_tool TEXT;

-- One row per UTC day. Every counter starts at 0 and is bumped via UPSERT from
-- functions/_lib/d1.ts (bumpFunnel) — visits (functions/_middleware.ts, GET / only),
-- waitlist_new / waitlist_dup (functions/api/waitlist.ts, keyed on INSERT OR IGNORE's
-- changes count), email_sent / email_failed (functions/api/waitlist.ts, keyed on the
-- Resend response status). Read via GET /api/funnel (functions/api/funnel.ts).
CREATE TABLE IF NOT EXISTS funnel_daily (
  day          TEXT PRIMARY KEY,             -- YYYY-MM-DD, UTC
  visits       INTEGER NOT NULL DEFAULT 0,
  waitlist_new INTEGER NOT NULL DEFAULT 0,
  waitlist_dup INTEGER NOT NULL DEFAULT 0,
  email_sent   INTEGER NOT NULL DEFAULT 0,
  email_failed INTEGER NOT NULL DEFAULT 0
);
