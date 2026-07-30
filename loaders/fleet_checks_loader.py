"""Loader for the fleet check-result envelope (`ops/checks/{id}/latest.json`).

WHY A GENERIC ENVELOPE RATHER THAN A LOADER PER CHECK
-----------------------------------------------------
Brian, 2026-07-29: *"we need to build all these checks into console for easier
and persistent monitoring."* The fleet has accumulated scheduled checks —
IAM grant usage, scheduled-workflow health, the deploy-release standard sweep,
lib-pin drift — that report only to Telegram and a workflow log. A Telegram
message is a notification, not a surface: it is unqueryable, it disappears, and
its ABSENCE looks exactly like "nothing wrong." That is how four of those
checks sat failing for days on 2026-07-29 without anyone knowing.

Adding one console component per check does not scale (Fleet Status is already
13 rows). So checks write ONE common shape and the console renders any of them:

    {
      "schema_version": 1,
      "check_id": "iam_grant_usage",
      "label": "IAM grant usage (least privilege)",
      "ran_at": "2026-07-29T20:00:00+00:00",
      "status": "ok" | "attention" | "error",
      "summary": "one line an operator can act on",
      "cadence_minutes": 10080,
      "deep_link": "https://…",           # optional, per-check evidence
      "findings": [{"key": "...", "detail": "..."}]
    }

A new check gets a console row by writing that key. No console change required
— which is the point: the reason checks report to Telegram today is that
surfacing one costs a PR against the dashboard.

STALENESS IS COMPUTED, NOT TRUSTED (Status-Surface Standard §118 rule 3).
`cadence_minutes` is what the check CLAIMS; `ran_at` is when it last actually
ran. A check whose artifact is older than ~2 cadences is reported as STALE
regardless of the `status` it last wrote — a check that stopped running is not
"ok", and the last thing it wrote before dying says "ok" forever.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import _research_bucket, download_s3_json, list_s3_prefixes

CHECKS_PREFIX = "ops/checks/"

# A check is stale after this multiple of its own declared cadence. 2.5 gives
# one full missed run plus slack for a late start, without letting a check that
# has silently died read as healthy for another whole cycle.
STALE_CADENCE_MULTIPLE = 2.5

# Used when an envelope omits cadence_minutes. Daily is the fleet's most common
# check cadence; the alternative — treating a missing cadence as "never stale" —
# is the failure mode this module exists to prevent.
DEFAULT_CADENCE_MINUTES = 1440

STATUS_OK = "ok"
STATUS_ATTENTION = "attention"
STATUS_ERROR = "error"
STATUS_STALE = "stale"
STATUS_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    label: str
    status: str
    summary: str
    ran_at: datetime | None
    cadence_minutes: int
    deep_link: str | None
    findings: tuple

    @property
    def is_healthy(self) -> bool:
        return self.status == STATUS_OK


def _parse_iso(raw) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def interpret(envelope: dict | None, *, check_id: str, now: datetime) -> CheckResult:
    """Envelope → CheckResult, with staleness overriding the reported status.

    Pure. The staleness override is the load-bearing part: the last thing a
    check writes before it stops running is usually "ok", and without this a
    dead check renders green forever."""
    if not isinstance(envelope, dict):
        return CheckResult(check_id, check_id, STATUS_UNREADABLE,
                           "no readable result artifact", None,
                           DEFAULT_CADENCE_MINUTES, None, ())

    label = envelope.get("label") or check_id
    reported = envelope.get("status") or STATUS_UNREADABLE
    summary = envelope.get("summary") or "(no summary)"
    ran_at = _parse_iso(envelope.get("ran_at"))
    cadence = envelope.get("cadence_minutes") or DEFAULT_CADENCE_MINUTES
    findings = tuple(envelope.get("findings") or ())

    if ran_at is None:
        return CheckResult(check_id, label, STATUS_UNREADABLE,
                           f"{summary} (no parseable ran_at)", None,
                           cadence, envelope.get("deep_link"), findings)

    if now - ran_at > timedelta(minutes=cadence * STALE_CADENCE_MULTIPLE):
        age_h = (now - ran_at).total_seconds() / 3600
        return CheckResult(
            check_id, label, STATUS_STALE,
            f"last ran {age_h:.0f}h ago — cadence is {cadence / 60:.0f}h "
            f"(last reported: {reported})",
            ran_at, cadence, envelope.get("deep_link"), findings,
        )

    return CheckResult(check_id, label, reported, summary, ran_at, cadence,
                       envelope.get("deep_link"), findings)


def load_check_results(*, now: datetime | None = None) -> list[CheckResult]:
    """Every check publishing under `ops/checks/`, worst-first.

    Discovery is by S3 prefix rather than a hardcoded list, so a new check
    appears here the first time it runs — no console deploy in the loop."""
    now = now or datetime.now(timezone.utc)
    bucket = _research_bucket()
    out: list[CheckResult] = []
    for prefix in list_s3_prefixes(bucket, CHECKS_PREFIX):
        check_id = prefix.rstrip("/").rsplit("/", 1)[-1]
        env = download_s3_json(bucket, f"{prefix}latest.json")
        out.append(interpret(env if isinstance(env, dict) else None,
                             check_id=check_id, now=now))
    order = {STATUS_ERROR: 0, STATUS_UNREADABLE: 1, STATUS_STALE: 2,
             STATUS_ATTENTION: 3, STATUS_OK: 4}
    return sorted(out, key=lambda c: (order.get(c.status, 5), c.check_id))
