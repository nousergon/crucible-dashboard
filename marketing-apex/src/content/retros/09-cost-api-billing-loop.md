---
title: 'The read that was also a purchase — 44,167 billed cost queries from one false zero'
date: '2026-09-03'
severity: 'P1'
domain: 'Cost management / observability'
summary: >-
  An agent session made 44,167 calls to a cost API billed per request, re-asking two
  questions whose answers could not change, because our setup showed a tagged cost as
  $0.00 instead of unmeasurable. The cost API is now removed fleet-wide.
---

**Date:** 2026-09-03 → 09-06 (found 09-09) · **Severity:** P1 · **Resolution:** cost API removed fleet-wide; unmeasurable values render as UNMEASURABLE; daily spend alarm live

### What happened

Over four days an agent session made 44,167 calls to a cost-reporting API that bills a cent per request. Normal volume is a handful per day. Nothing paged; a person found it three days after it stopped.

### Cause

- **False zero.** A query filtered to our cost tag returned $0.00 because the tag had never been activated for cost reporting. Budget gates read that as "spent $0, pass". The session saw a wrong-looking answer and kept re-asking.
- **Immutable answer, never cached.** 88% of calls were the same two queries for a month already closed. 44,167 calls produced about ten distinct answers.
- **No notification.** Our expense collector had the spike and an "over budget" flag within hours, but only wrote them to a console page.

### Fix

- No code or permission in the fleet calls the cost API now. Spend comes from the free billing export, and guard tests fail if the API returns.
- An inactive tag or missing input renders as UNMEASURABLE with its cause, never $0.00.
- A daily spend alarm that makes no billed calls pages when one service spends a large share of the trailing month in a day. Replayed on this incident, it fires on day one.
- A budget breach automatically denies the overspending service.

All live since early October with no recurrence.

### Lessons

- Never render "unmeasurable" as a number.
- Cache immutable answers as facts about the data.
- Measurement without notification is not monitoring.
- A guard must not consume what it guards.
- Diagnose a runaway caller by distinct requests, not call count.
