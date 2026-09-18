-- metron-ops-I332: record Resend DELIVERY events, not just send-accepted.
--
-- `email_sent` (functions/api/waitlist.ts) only means Resend's REST API answered 2xx to
-- our POST — a message it later bounced or dropped still counts today. This migration
-- adds the columns the Resend webhook (functions/api/resend-webhook.ts) needs to record
-- what actually happened to the message *after* Resend accepted it.
--
-- `waitlist.resend_message_id`: the Resend message id returned by the send call, stored
-- so an inbound webhook event (which names the message by id, not by our internal state)
-- can be attributed back to the signup it belongs to.
ALTER TABLE waitlist ADD COLUMN resend_message_id TEXT;
CREATE INDEX IF NOT EXISTS idx_waitlist_resend_message_id ON waitlist (resend_message_id);

-- `waitlist.delivery_status` / `delivery_event_id` / `delivered_at`: the last delivery
-- webhook event applied to this row (one of 'delivered' | 'bounced' | 'complained' |
-- 'delivery_delayed'), the id of that event (Resend's own event id — the `svix-id`
-- header on the webhook delivery, globally unique and quotable), and when we recorded
-- it. NULL on all three means no delivery event has arrived yet — distinguishable from
-- 'bounced', never conflated with it.
ALTER TABLE waitlist ADD COLUMN delivery_status TEXT;
ALTER TABLE waitlist ADD COLUMN delivery_event_id TEXT;
ALTER TABLE waitlist ADD COLUMN delivered_at INTEGER;

-- Aggregate funnel counters, same shape as email_sent/email_failed (functions/_lib/d1.ts
-- bumpFunnel). `email_delivered` / `email_bounced` are DIFFERENT facts from `email_sent`
-- — a message can be sent (2xx from Resend) and never delivered, or delivered well after
-- the send-accepted count was bumped. Both are read via GET /api/funnel
-- (functions/api/funnel.ts), which already reports measured:false rather than 0 for a
-- counter with no lifetime record.
ALTER TABLE funnel_daily ADD COLUMN email_delivered INTEGER NOT NULL DEFAULT 0;
ALTER TABLE funnel_daily ADD COLUMN email_bounced INTEGER NOT NULL DEFAULT 0;

-- Idempotency ledger for the webhook: Resend (via Svix) can redeliver the same event.
-- `event_id` is the `svix-id` header value, globally unique per delivery attempt of a
-- given event — INSERT OR IGNORE makes a redelivered event a no-op rather than a double
-- funnel bump. See functions/api/resend-webhook.ts.
CREATE TABLE IF NOT EXISTS processed_webhook_events (
  event_id     TEXT PRIMARY KEY,
  event_type   TEXT NOT NULL,
  processed_at INTEGER NOT NULL DEFAULT (unixepoch())
);
