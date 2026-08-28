#!/usr/bin/env python3
"""Route box-health's monitoring-hygiene notices to the console instead of Brian.

WHY THIS EXISTS
---------------
`box_health.sh` classifies every problem into three tiers. The `info` tier --
lines starting `notice: ` -- is hygiene about the MONITORING itself: a timer
with no declared dead-man threshold, a memory reading that is censored because
the unit is parked at its cap. Nothing is degraded; there is nothing to do
right now; the tier's own message prefix says so, literally: "monitoring
hygiene (no action urgent)".

That tier was published to `krepis.alerts` at severity `info`, and the whole
design rested on `info` being invisible to the operator. It is not.
`krepis/alerts.py` sets `SEVERITY_PUSH = {"error", "critical"}` and passes
`disable_notification=True` for everything else -- and Telegram's
`disable_notification` suppresses the PHONE PUSH, not the message. The message
still lands in the chat. SNS delivery is identical at every severity.

So there was never a tier that kept a finding out of the operator's channel,
only one that arrived without a buzz. That is why this finding has been
"fixed" repeatedly without the alerts stopping: 2026-07-29 split the tiers,
2026-08-20 (#7822) lowered the warning window 60 -> 1440. Both tuned CADENCE
and SEVERITY. Neither controls VISIBILITY, which was the actual complaint.

A channel that repeats an unchanged, explicitly-non-urgent condition trains its
reader to stop reading it -- and that costs the alerts that ARE urgent, not
just the one being repeated.

WHAT REPLACES IT
----------------
The same fleet-check envelope contract every other scheduled check publishes
(`nousergon_lib.fleet_check_result`), discovered by S3 prefix, rendered by the
console's `fleet_checks_loader`. Hygiene belongs on a board that shows how long
it has been true, not in a stream that re-announces it -- #7822 deliverable 3.

THIS IS NOT SUPPRESSION, AND THE DIFFERENCE IS TESTABLE.
`principles.md` §7: a component emitting nothing is not healthy, it is
unobserved, and *no data* is never rendered as green. Three properties keep
that true here:

  * The envelope is published on EVERY run, including runs with zero notices.
    A surface that publishes only when something is wrong cannot be
    distinguished from one that has died.
  * `ran_at` + `cadence_minutes` let the console mark this check STALE when it
    stops publishing, whatever status it last wrote. The last thing a dying
    check writes is almost always "ok".
  * A missing artifact renders as `unreadable`, never `ok`.

`warning` IS HERE TOO, AND AS OF 2026-08-28 THE CONSOLE IS ITS ONLY SURFACE
--------------------------------------------------------------------------

  notice   console ONLY.
  warning  console ONLY, channel as FALLBACK.
  clear    console for a page that did not buzz; channel for one that did.
  critical channel, hourly, untouched.

**The blocker that kept `warning` on the channel was re-measured and is gone**
(alpha-engine-config-I9044).

WAS (this docstring, 2026-08-20): "`warning`'s justification for being quiet is
that it is DELEGATED — it reaches the Overseer intake bus as alert class
`box-health` (`intake: bus` / `response: drain-queue`). Measured against the
live account: all four `alpha-engine-alert-drain-{0400,1000,1600,2200}utc`
EventBridge schedules are DISABLED under the 2026-08-07 automation pause
(alpha-engine-config-I6984), and the drain's registry row states plainly that
no cadence is auditable from what remains. The delegated consumer is not
running on a schedule. Removing `warning` from the channel today would leave it
with no reader at all."

NOW (re-measured 2026-08-28 against the live account): **those four schedules
are still DISABLED — and the drain has run daily anyway.** It is dispatched by
an EVENT-TIME leg, not by them. EventBridge *rule*
`alpha-engine-freshness-monitor-cron` (`cron(0 12 * * ? *)`, **ENABLED**) fires
the `alpha-engine-freshness-monitor` Lambda, which `lambda.invoke()`s
`alpha-engine-overseer-dispatcher` with `{"playbook": "alert-drain"}` on a
freshness CRITICAL (alpha-engine-config-I3282;
`FRESHNESS_MONITOR_DRAIN_DISPATCH_ENABLED=true`, verified live; wired at
`nousergon-data/infrastructure/overseer/playbooks.yaml:192-231`). Evidence:
`drain_ledger` objects under `s3://alpha-engine-research/overseer/drain_ledger/`
for 2026-08-24T1201Z, 08-25T1201Z, 08-26T1201Z, 08-26T1430Z, 08-27T1202Z and
08-28T1202Z, each with an EC2 spot run log.

So the delegated consumer IS alive, on a trigger nobody had measured, and the
one stated reason `warning` could not follow `notice` off the channel no longer
holds. It follows it now. What it does NOT do is follow it *unconditionally*:
`box_health.sh` keeps the tier's dedup key and its 43200-minute window, because
the CHANNEL IS STILL ITS FALLBACK. If the envelope below cannot be published,
`console_route_fallback` sends that exact page to the channel as before and says
so on stderr. A route whose failure mode is "nothing was sent" is a silent
swallow wearing a routing decision's clothes.

CLEARS INHERIT THE DESTINATION OF THE PAGE THEY TERMINATE
---------------------------------------------------------
`box_health.sh` has always recorded the OPENING page's severity in its alerted-
state file, and `publish_clears` always threw that field away — so a terminator
for a condition that never buzzed arrived in the chat as loudly as one for a
condition Brian was woken for. It now reads the field:

  * opener in krepis' phone-push set (`error`, `critical`) — the clear goes to
    the channel, exactly as before. A terminator for a page he was woken for is
    OWED to him, and the `timer job failing:` findings that dominate this box's
    pages are `critical`, so their RESOLVED messages are unchanged.
  * opener anything else — the clear is a `clear: ` line here, on the same
    envelope, so the lifecycle stays observable without a second message about
    a non-event.

A `clear: ` line never turns the console row to `attention`: it reports that
something ENDED. It also carries no first-seen age — it is an event, not a
standing finding, and it is not entered into the age map.

WHAT DOES NOT CHANGE
--------------------
`critical` still pushes, hourly, and still clears in the channel. A degraded-now
condition is worth repeating precisely because it is not standing.

FIRST-SEEN
----------
Each notice carries how long it has been standing, from a JSON map in the
box-health state directory -- the same `/var/lib/box-health` that already holds
the throttle baseline and the memory peak marks. Age is the field that makes a
standing finding legible: "true for 12 days, tracked in #7804" and "first seen
an hour ago" are categorically different, and until now nothing distinguished
them except how many times the same sentence had been sent.

Entries for notices that have cleared are dropped, so the map cannot grow
without bound and a re-appearing notice honestly reads as new.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

CHECK_ID = "box_health_hygiene"
# The prefix box_health.sh puts on a console-routed clear. It carries the
# identity key of the page it terminates, so two clears can never collapse onto
# one console row the way a truncated message text could.
CLEAR_PREFIX = "clear: "
NOTICE_PREFIX = "notice: "
LABEL = "Box health — standing findings"

# box-health.timer runs every 10 minutes. Declared honestly: understating makes
# the console call this check stale early, overstating lets a dead emitter read
# healthy for longer than it should. 30 = 3x, matching the timer's own
# max_staleness in budget.yaml.
CADENCE_MINUTES = 30

STATE_DIR = pathlib.Path(os.environ.get("STATE_DIRECTORY", "/var/lib/box-health"))
FIRST_SEEN_PATH = STATE_DIR / "hygiene-first-seen.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_first_seen(path: pathlib.Path | None = None) -> dict[str, str]:
    """Read the notice -> first-seen map. A missing or corrupt file is empty.

    Deliberately forgiving: this file is an age ANNOTATION, and losing it costs
    accuracy on one field. Refusing to publish the notice set because its
    timestamps could not be read would trade the whole surface for a decoration
    -- the inversion `emit()` itself is careful not to make.
    """
    path = path or FIRST_SEEN_PATH
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def reconcile_first_seen(
    notices: list[str],
    previous: dict[str, str],
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Carry forward the timestamp of every notice still present; drop the rest.

    Dropping cleared notices is what keeps a re-appearing notice honest: it
    comes back with today's date rather than inheriting an age it did not earn.
    """
    stamp = (now or _now()).isoformat()
    return {n: previous.get(n, stamp) for n in notices}


def write_first_seen(mapping: dict[str, str], path: pathlib.Path | None = None) -> bool:
    """Persist the map. Returns False if it could not be written.

    Reported by the caller rather than raised: an unwritable state dir means
    every notice reads as new on every run, which is a degraded age field, not
    a reason to drop the surface.
    """
    path = path or FIRST_SEEN_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(mapping, indent=2, sort_keys=True))
        tmp.replace(path)
    except OSError:
        return False
    return True


def standing_days(first_seen: str, *, now: datetime | None = None) -> float | None:
    try:
        seen = datetime.fromisoformat(first_seen)
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return ((now or _now()) - seen).total_seconds() / 86400.0


def _age_phrase(days: float | None) -> str:
    if days is None:
        return "standing since an unparseable timestamp"
    if days < 1:
        hours = max(0, int(days * 24))
        return "first seen this run" if hours == 0 else f"standing {hours}h"
    return f"standing {int(days)}d"


# A stable, readable key per notice, for the console's finding list. The notice
# text itself is the natural identity, but it carries the offending unit name
# and can be long; the key is the CLASS and the detail carries the sentence.
_KEY_PATTERNS = (
    (re.compile(r"^notice: timer has no dead-man threshold: (\S+)"), r"timer-deadman:\1"),
    (re.compile(r"^notice: memory budget observation hygiene"), "memory-observation"),
    # One key per TIMER, not one per class: the live start-dependency scan
    # (alpha-engine-config-I9062) can find several at once — four foreign
    # timers carried the defect on 2026-08-28 — and a shared key would
    # collapse them onto one console row, so fixing one would look like
    # fixing all of them.
    (
        re.compile(r"^notice: timer start-dependency: (\S+)"),
        r"timer-install-start:\1",
    ),
    # A clear names its page's identity key first. Using it verbatim makes the
    # console row and the channel terminator that would have been sent instead
    # refer to the SAME thing.
    (re.compile(r"^clear: (\S+)"), r"\1"),
)


def finding_key(notice: str) -> str:
    for pattern, repl in _KEY_PATTERNS:
        m = pattern.match(notice)
        if m:
            # expand() handles both forms: a template with a \1 backreference
            # and a literal with none.
            return m.expand(repl)
    # Unrecognised notices are NOT dropped and NOT bucketed together. A new
    # `notice:` added to box_health.sh without a pattern here must still reach
    # the console under its own identity -- the same direction the severity
    # classifier's default arm takes, for the same reason.
    return (
        notice[len(NOTICE_PREFIX):][:60]
        if notice.startswith(NOTICE_PREFIX)
        else notice[:60]
    )


def line_tier(line: str) -> str:
    """`notice`, `clear`, or `warning` — the same three-way split the shell made.

    Keyed on the SAME prefixes `box_health.sh` writes, so the two cannot drift
    into disagreeing about what a line is.
    """
    if line.startswith(NOTICE_PREFIX):
        return "notice"
    if line.startswith(CLEAR_PREFIX):
        return "clear"
    return "warning"


def build_findings(
    lines: list[str], first_seen: dict[str, str], *, now: datetime | None = None
) -> list[dict]:
    """One entry per finding, tier-tagged, carrying how long it has stood.

    The tier is on the KEY rather than only in the detail so the console can
    sort and filter by it. `notice` and `warning` findings reach this surface
    INSTEAD of the channel; a `clear` reaches it instead of the channel only
    when the page it terminates never buzzed -- see the module docstring.

    A `clear` carries no age. It is an event, not a standing finding, and
    annotating it with "first seen this run" would state the obvious while
    implying it is something that could still be standing tomorrow.
    """
    out = []
    for n in lines:
        tier = line_tier(n)
        if tier == "clear":
            out.append({"key": f"clear/{finding_key(n)}", "detail": n})
            continue
        days = standing_days(first_seen.get(n, ""), now=now) if n in first_seen else None
        out.append({
            "key": f"{tier}/{finding_key(n)}",
            "detail": f"{n} — {_age_phrase(days)}",
        })
    return out


def build_summary(
    lines: list[str],
    first_seen: dict[str, str],
    *,
    warnings: list[str] | None = None,
    clears: list[str] | None = None,
    now: datetime | None = None,
) -> str:
    """The one line an operator reads on the console row.

    Names the WARNING count separately from the total. A summary reading "4
    standing findings" hides whether any of them is a declared-invariant breach
    or all four are monitoring hygiene, and those warrant different attention —
    the same reason check_memory_budget's summary names the tightest unit rather
    than an average.
    """
    clears = clears or []
    resolved = ""
    if clears:
        plural_c = "" if len(clears) == 1 else "s"
        resolved = f"{len(clears)} alert{plural_c} resolved this run"
    if not lines:
        # A run that only resolved things says so. "no standing findings" alone
        # would be true and would hide the one thing that actually happened.
        return f"no standing findings, {resolved}" if resolved else "no standing findings"
    warnings = warnings or []
    ages = [
        d
        for d in (standing_days(first_seen.get(n, ""), now=now) for n in lines)
        if d is not None
    ]
    plural = "" if len(lines) == 1 else "s"
    head = f"{len(lines)} standing finding{plural}"
    if warnings:
        head += f" ({len(warnings)} warning)"
    if ages:
        head = f"{head}, oldest {_age_phrase(max(ages))}"
    return f"{head}; {resolved}" if resolved else head


def split_tiers(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split one stdin stream into (notices, warnings, clears).

    The classification is box_health.sh's, already applied -- this only has to
    tell the accumulators apart after they arrive on one stream. Matching the
    SAME prefixes the shell writes, so the two cannot drift into disagreeing
    about what a notice or a clear is.

    THREE-WAY, not two-way with a default bucket. When clears joined the stream
    a two-way split would have counted every one of them as a `warning`, and
    `build_summary` names the warning count separately precisely so a reader can
    tell a declared-invariant breach from hygiene. A resolution counted as a
    breach is worse than no count at all.
    """
    notices = [ln for ln in lines if line_tier(ln) == "notice"]
    warnings = [ln for ln in lines if line_tier(ln) == "warning"]
    clears = [ln for ln in lines if line_tier(ln) == "clear"]
    return notices, warnings, clears


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv

    # Findings arrive on stdin, one per line -- the same shape box_health.sh
    # already accumulates them in. Blank lines are dropped: `<<<` on a value
    # ending in a newline yields a phantom empty element, which is the exact
    # defect that once rendered a bare " - " bullet into an alert body.
    lines = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    notices, warnings, clears = split_tiers(lines)
    # Clears are events, not standing findings: they are rendered, never aged,
    # and never entered into the first-seen map (which would grow it by one
    # entry per resolution and drop them all again on the next run).
    standing = notices + warnings

    previous = load_first_seen()
    current = reconcile_first_seen(standing, previous)
    state_ok = write_first_seen(current) if not dry_run else True
    if not state_ok:
        print(
            f"emit_box_health_hygiene: {FIRST_SEEN_PATH} not writable — every "
            "notice will read as new on every run; ages below are unreliable",
            file=sys.stderr,
        )

    try:
        from nousergon_lib import fleet_check_result as fcr
    except ImportError:
        print(
            "emit_box_health_hygiene: nousergon_lib.fleet_check_result "
            "unavailable — console row NOT published",
            file=sys.stderr,
        )
        return 3

    # `attention`, never `error`: this tier's contract is that nothing is
    # currently degraded. A hygiene finding that could turn the console row red
    # would have re-created, on a second surface, exactly the miscalibration
    # this change removes from the first.
    #
    # A run whose only lines are CLEARS is `ok`, not `attention`: nothing is
    # standing, and a row that turns amber to report that something ended would
    # re-create on this surface the exact "second alert for a non-event" the
    # clear routing exists to remove.
    status = fcr.STATUS_ATTENTION if standing else fcr.STATUS_OK

    uri = fcr.emit_result(
        check_id=CHECK_ID,
        label=LABEL,
        status=status,
        summary=build_summary(standing, current, warnings=warnings, clears=clears),
        cadence_minutes=CADENCE_MINUTES,
        findings=build_findings(standing + clears, current),
        dry_run=dry_run,
    )
    if uri is None and not dry_run:
        # emit() already logged. Reported here too because box_health.sh's
        # journal is where an operator looks first.
        print(
            "emit_box_health_hygiene: envelope publish returned no URI — the "
            "console will render this check as `unreadable`, never `ok`",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
