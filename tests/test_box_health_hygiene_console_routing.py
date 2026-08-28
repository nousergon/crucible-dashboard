"""The info tier reaches the console, and reaches it even when it is clean.

WHY
---
`box_health.sh`'s `info` tier -- lines starting `notice: ` -- is hygiene about
the monitoring itself. It published to `krepis.alerts` at severity `info`, and
the design rested on that being invisible to Brian. It was not:
`krepis/alerts.py` sets `SEVERITY_PUSH = {"error", "critical"}` and passes
`disable_notification=True` for everything else, and Telegram's
`disable_notification` suppresses the phone PUSH, not the MESSAGE. Every info
line still landed in the chat; SNS delivery is identical at every severity.

So no tier ever kept a finding out of the operator's channel. That is why the
finding survived two fixes -- a tier split (2026-07-29) and a window drop from
60 to 1440 (#7822) -- that between them adjusted severity and cadence and never
touched visibility.

WHAT THESE TESTS DEFEND
-----------------------
The failure mode of this change is that it becomes SUPPRESSION. Routing and
suppression are indistinguishable from the notification channel, and only one
of them is legitimate. principles.md §7: a component emitting nothing is not
healthy, it is unobserved, and *no data* is never rendered as green. So the
properties below are asserted, not assumed:

  * the envelope publishes on every run, INCLUDING runs with no notices;
  * a notice never silently vanishes for want of a key pattern;
  * the status can never be `error`, because this tier's contract is that
    nothing is currently degraded;
  * `cadence_minutes` is honest, so the console can call the emitter stale.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.box_health_helpers import run_lifecycle

REPO = pathlib.Path(__file__).resolve().parents[1]
EMITTER_PATH = REPO / "infrastructure" / "emit_box_health_hygiene.py"
BOX_HEALTH = (REPO / "infrastructure" / "box_health.sh").read_text()

_spec = importlib.util.spec_from_file_location("emit_box_health_hygiene", EMITTER_PATH)
emitter = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(emitter)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _function_body(name: str) -> str:
    """The shipped shell text of one function, so an assertion about its
    contents cannot be satisfied by a comment elsewhere in the file."""
    m = re.search(rf"^{name}\(\)\s*\{{.*?^\}}", BOX_HEALTH, re.M | re.S)
    assert m, f"{name}() not found in box_health.sh"
    return m.group(0)

TIMER_NOTICE = (
    "notice: timer has no dead-man threshold: emit-service-memory.timer — "
    "add a timers: row to budget.yaml"
)
MEMORY_NOTICE = "notice: memory budget observation hygiene (detail in journal)"


# ── the wiring in box_health.sh ─────────────────────────────────────────────

def test_info_tier_no_longer_publishes_to_the_alert_channel():
    assert "publish_problems info " not in BOX_HEALTH, (
        "the info tier publishes to krepis.alerts again — which delivers it "
        "visibly into Brian's chat, silently only in the sense of no phone buzz."
    )


def test_the_envelope_is_emitted_on_every_exit_path():
    """Including the all-healthy early exits.

    `box_health.sh` exits early when nothing is confirmed. If the envelope were
    emitted only from the bottom of the script, it would publish ONLY on runs
    that already found a problem — and a surface that publishes only when
    something is wrong cannot be distinguished from one that has died.

    SINCE alpha-engine-config-I9044 the emit happens INSIDE
    `finalize_alert_lifecycle`, which is what each exit path calls. This test
    counts THAT, and separately asserts the envelope is emitted from inside it,
    because a count of `emit_hygiene_envelope ` occurrences also matches the
    prose in this file's own comments — a guard that passes on documentation is
    not a guard.
    """
    calls = len(re.findall(r"^\s*finalize_alert_lifecycle ", BOX_HEALTH, re.M))
    assert BOX_HEALTH.count("finalize_alert_lifecycle() {") == 1, (
        "the lifecycle finalizer is defined more than once"
    )
    assert calls >= 3, (
        f"found {calls} call site(s); expected at least 3 — the two all-healthy "
        "early exits and the final partitioned publish. A missing early-exit "
        "call means a clean box publishes nothing, which the console cannot "
        "distinguish from a dead emitter."
    )
    body = _function_body("finalize_alert_lifecycle")
    assert "emit_hygiene_envelope " in body, (
        "finalize_alert_lifecycle no longer emits the console envelope, so the "
        "exit paths that call it publish nothing to the console at all."
    )


def test_the_helpers_are_defined_before_their_first_call_site():
    """bash is linear: a call above the definition is a runtime failure.

    Pinned because it is invisible to `bash -n`, which parses without executing,
    and the two early-exit call sites sit hundreds of lines above the tier
    partition where these helpers would most naturally have been written.
    """
    first_call = re.search(r"^\s*finalize_alert_lifecycle ", BOX_HEALTH, re.M).start()
    for helper in (
        "emit_hygiene_envelope",
        "publish_clears",
        "publish_channel_clear",
        "console_route_fallback",
        "clear_destination",
        "publish_page",
        "finalize_alert_lifecycle",
    ):
        assert BOX_HEALTH.index(f"{helper}() {{") < first_call, (
            f"{helper} is called before it is defined; every early-exit run "
            "would die on `command not found`."
        )


# ── first-seen bookkeeping ──────────────────────────────────────────────────

def test_a_standing_notice_keeps_its_original_first_seen():
    previous = {TIMER_NOTICE: (NOW - timedelta(days=12)).isoformat()}
    got = emitter.reconcile_first_seen([TIMER_NOTICE], previous, now=NOW)
    assert got[TIMER_NOTICE] == previous[TIMER_NOTICE]


def test_a_new_notice_is_stamped_now():
    got = emitter.reconcile_first_seen([MEMORY_NOTICE], {}, now=NOW)
    assert got[MEMORY_NOTICE] == NOW.isoformat()


def test_a_cleared_notice_is_dropped_and_returns_as_new():
    """Otherwise a re-appearing notice inherits an age it did not earn."""
    previous = {TIMER_NOTICE: (NOW - timedelta(days=30)).isoformat()}
    cleared = emitter.reconcile_first_seen([], previous, now=NOW)
    assert cleared == {}
    returned = emitter.reconcile_first_seen([TIMER_NOTICE], cleared, now=NOW)
    assert returned[TIMER_NOTICE] == NOW.isoformat()


def test_an_unreadable_state_file_degrades_the_age_not_the_surface(tmp_path):
    bad = tmp_path / "hygiene-first-seen.json"
    bad.write_text("{ this is not json")
    assert emitter.load_first_seen(bad) == {}


def test_a_state_file_holding_a_list_is_rejected_not_iterated(tmp_path):
    bad = tmp_path / "hygiene-first-seen.json"
    bad.write_text(json.dumps(["notice: something"]))
    assert emitter.load_first_seen(bad) == {}


def test_write_then_read_round_trips(tmp_path):
    path = tmp_path / "sub" / "hygiene-first-seen.json"
    mapping = {TIMER_NOTICE: NOW.isoformat()}
    assert emitter.write_first_seen(mapping, path) is True
    assert emitter.load_first_seen(path) == mapping


# ── findings ────────────────────────────────────────────────────────────────

def test_every_notice_produces_exactly_one_finding():
    """No notice is dropped, and none is merged into another."""
    notices = [TIMER_NOTICE, MEMORY_NOTICE]
    first_seen = emitter.reconcile_first_seen(notices, {}, now=NOW)
    findings = emitter.build_findings(notices, first_seen, now=NOW)
    assert len(findings) == 2
    assert {f["key"] for f in findings} == {
        "notice/timer-deadman:emit-service-memory.timer",
        "notice/memory-observation",
    }


def test_an_unrecognised_notice_still_reaches_the_console_under_its_own_key():
    """A `notice:` added to box_health.sh with no key pattern must not vanish.

    Same direction as the severity classifier's default arm: an unclassified
    finding fails toward being SEEN, never toward being quiet.
    """
    novel = "notice: something nobody has written a key pattern for yet"
    first_seen = emitter.reconcile_first_seen([novel], {}, now=NOW)
    findings = emitter.build_findings([novel], first_seen, now=NOW)
    assert len(findings) == 1
    assert findings[0]["key"]
    assert findings[0]["key"] != "notice/memory-observation"
    assert novel in findings[0]["detail"]


def test_two_timer_notices_do_not_collide_on_one_key():
    a = "notice: timer has no dead-man threshold: alpha.timer — add a timers: row to budget.yaml"
    b = "notice: timer has no dead-man threshold: beta.timer — add a timers: row to budget.yaml"
    keys = {emitter.finding_key(a), emitter.finding_key(b)}
    assert len(keys) == 2, "two distinct timers collapsed into one console row"


def test_the_detail_carries_how_long_the_finding_has_stood():
    first_seen = {TIMER_NOTICE: (NOW - timedelta(days=12)).isoformat()}
    findings = emitter.build_findings([TIMER_NOTICE], first_seen, now=NOW)
    assert "standing 12d" in findings[0]["detail"]


# ── summary and status ──────────────────────────────────────────────────────

def test_a_clean_run_says_so_rather_than_saying_nothing():
    assert emitter.build_summary([], {}, now=NOW) == "no standing findings"


def test_the_summary_names_the_oldest_finding():
    notices = [TIMER_NOTICE, MEMORY_NOTICE]
    first_seen = {
        TIMER_NOTICE: (NOW - timedelta(days=3)).isoformat(),
        MEMORY_NOTICE: (NOW - timedelta(days=19)).isoformat(),
    }
    summary = emitter.build_summary(notices, first_seen, now=NOW)
    assert "2 standing findings" in summary
    assert "standing 19d" in summary


def test_the_cadence_is_declared_honestly():
    """Understating makes the console call this stale early; overstating lets a
    dead emitter read healthy for longer than it should. box-health.timer fires
    every 10 minutes and budget.yaml gives it a 30m max_staleness."""
    assert emitter.CADENCE_MINUTES == 30


def test_the_check_id_matches_its_s3_path_segment():
    assert emitter.CHECK_ID == "box_health_hygiene"
    assert "/" not in emitter.CHECK_ID


@pytest.mark.parametrize("notices", [[], [TIMER_NOTICE], [TIMER_NOTICE, MEMORY_NOTICE]])
def test_hygiene_can_never_turn_the_console_row_red(notices, monkeypatch, tmp_path):
    """`attention`, never `error`.

    This tier's contract is that nothing is currently degraded. A hygiene
    finding able to turn a console row red would re-create, on a second surface,
    exactly the miscalibration this change removed from the first.
    """
    captured = {}

    class _FakeFCR:
        STATUS_OK = "ok"
        STATUS_ATTENTION = "attention"
        STATUS_ERROR = "error"

        @staticmethod
        def emit_result(**kwargs):
            captured.update(kwargs)
            return "s3://bucket/key"

    import sys as _sys
    import types as _types

    fake_pkg = _types.ModuleType("nousergon_lib")
    fake_pkg.fleet_check_result = _FakeFCR
    monkeypatch.setitem(_sys.modules, "nousergon_lib", fake_pkg)
    monkeypatch.setitem(_sys.modules, "nousergon_lib.fleet_check_result", _FakeFCR)
    monkeypatch.setattr(
        emitter, "FIRST_SEEN_PATH", tmp_path / "hygiene-first-seen.json"
    )
    monkeypatch.setattr("sys.stdin", _Stdin("\n".join(notices)))

    rc = emitter.main([])
    assert rc == 0
    assert captured["status"] != "error"
    assert captured["status"] == ("attention" if notices else "ok")
    assert len(captured["findings"]) == len(notices)


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# ── the installer ───────────────────────────────────────────────────────────

def test_the_emitter_is_installed_beside_the_publisher():
    """box_health.sh is installed to /usr/local/bin; the emitter must follow.

    `HYGIENE_EMITTER` resolves as a sibling of `$BASH_SOURCE`. An installed
    box_health.sh with no sibling loses the ENTIRE info tier silently: the
    helper's guard returns 0, the run stays green, and only the console row
    going stale would ever say so.

    This is the alpha-engine-config-I7168 class, which took the box's primary
    watchdog down on 2026-08-13 when `alert_py.sh` was referenced as an
    installed sibling that the installer never placed.
    """
    installer = (REPO / "infrastructure" / "install-box-health.sh").read_text()
    assert "/usr/local/bin/emit_box_health_hygiene.py" in installer, (
        "install-box-health.sh does not install the hygiene emitter beside "
        "box_health.sh — the info tier would resolve to a path that does not "
        "exist on the box."
    )


def test_the_emitter_is_resolved_as_a_sibling_not_a_hardcoded_checkout():
    """A literal /home/ec2-user/<repo>/... path is the drift I7168 named:
    this repo was renamed once already, and one `mv` would take it out."""
    i = BOX_HEALTH.index("HYGIENE_EMITTER=")
    line = BOX_HEALTH[i : BOX_HEALTH.index("\n", i)]
    assert "BASH_SOURCE" in line, line
    assert "/home/ec2-user" not in line, line


# ── the warning tier: console AND channel, and why the asymmetry is deliberate ──

WARNING_LINE = "memory budget: BREACH (detail in journal)"


def test_the_warning_tier_is_console_routed_with_the_channel_as_its_fallback():
    """SUPERSEDED 2026-08-28 (alpha-engine-config-I9044), on a re-measurement.

    This test used to require the warning tier to KEEP its channel publish. The
    premise was that the tier's delegated consumer was dead: measured
    2026-08-20, all four `alpha-engine-alert-drain-{0400,1000,1600,2200}utc`
    EventBridge schedules were DISABLED under the 2026-08-07 automation pause
    (I6984), so taking the tier off the channel would leave it with no reader.

    Re-measured 2026-08-28: those four schedules are STILL disabled, and the
    drain has run daily anyway — dispatched by an event-time leg nobody had
    measured. EventBridge rule `alpha-engine-freshness-monitor-cron`
    (`cron(0 12 * * ? *)`, ENABLED) fires the freshness-monitor Lambda, which
    invokes the overseer dispatcher with the `alert-drain` playbook (I3282).
    Evidence: `drain_ledger` objects for 2026-08-24 through 2026-08-28 under
    `s3://alpha-engine-research/overseer/drain_ledger/`.

    So the tier follows `notice` off the channel. What it keeps is a FALLBACK,
    which is the difference between routing and suppression, and that is what
    this test now pins — statically here, behaviourally in
    TestTheFallbackIsTheInvariant below.
    """
    call = re.search(r"^publish_problems warning .*$", BOX_HEALTH, re.M)
    assert call, "the warning tier is classified but never published anywhere"
    assert call.group(0).rstrip().endswith("console"), (
        "the warning tier publishes straight to the channel again. It is "
        f"console-routed since I9044: {call.group(0)}"
    )
    body = _function_body("console_route_fallback")
    assert "publish_page " in body, (
        "the fallback no longer publishes a deferred page — the console became "
        "the tier's only surface with nothing behind it, which is suppression."
    )


def test_the_warning_tier_reaches_the_console():
    """Both lower tiers travel on one envelope, and it is now their only
    surface: the standing set is visible continuously with each finding's age,
    rather than having to be remembered between channel repeats."""
    i = BOX_HEALTH.index('finalize_alert_lifecycle "$(')
    call = BOX_HEALTH[i : BOX_HEALTH.index("\n", i)]
    assert "$notices" in call and "$warnings" in call, call


def test_a_warning_and_a_notice_do_not_collide_on_one_console_key():
    lines = [WARNING_LINE, TIMER_NOTICE]
    first_seen = emitter.reconcile_first_seen(lines, {}, now=NOW)
    findings = emitter.build_findings(lines, first_seen, now=NOW)
    keys = [f["key"] for f in findings]
    assert len(set(keys)) == 2
    assert any(k.startswith("warning/") for k in keys)
    assert any(k.startswith("notice/") for k in keys)


def test_the_tier_is_on_the_console_key_so_the_row_can_be_filtered():
    """A console row that cannot tell a declared-invariant breach from
    monitoring hygiene has flattened the distinction the tiers exist to make."""
    findings = emitter.build_findings(
        [WARNING_LINE], emitter.reconcile_first_seen([WARNING_LINE], {}, now=NOW), now=NOW
    )
    assert findings[0]["key"].startswith("warning/")


def test_the_summary_names_the_warning_count_separately():
    """"4 standing findings" hides whether any is a breach or all four are
    hygiene, and those warrant different attention."""
    lines = [WARNING_LINE, TIMER_NOTICE, MEMORY_NOTICE]
    first_seen = emitter.reconcile_first_seen(lines, {}, now=NOW)
    summary = emitter.build_summary(lines, first_seen, warnings=[WARNING_LINE], now=NOW)
    assert "3 standing findings" in summary
    assert "(1 warning)" in summary


def test_split_tiers_agrees_with_the_shell_classifier_on_what_a_notice_is():
    notices, warnings, clears = emitter.split_tiers(
        [TIMER_NOTICE, WARNING_LINE, MEMORY_NOTICE]
    )
    assert notices == [TIMER_NOTICE, MEMORY_NOTICE]
    assert warnings == [WARNING_LINE]
    assert clears == []
    assert '"notice: "*) echo info ;;' in BOX_HEALTH, (
        "the shell classifier no longer keys on the `notice: ` prefix that "
        "split_tiers mirrors; the two have drifted."
    )


# ── clears inherit the destination of the page they terminate (I9044) ───────
#
# WHAT THESE DEFEND, and why they RUN the shell rather than read it.
#
# Brian objected to two `[INFO] box-health: RESOLVED` messages. The rule this
# section pins is narrow and exact: a terminator goes wherever its OPENING page
# went. A page that buzzed his phone is owed a terminator in the channel; a page
# that never buzzed is not owed a second message about a non-event, and its
# terminator is recorded on the console instead.
#
# The two RESOLVED messages that prompted this are `timer job failing:`
# findings, which publish at `critical` -- so under this rule they still reach
# the channel, correctly, and their real cause is a deploy firing
# morning-signal's units off-schedule (alpha-engine-config-I9000). What this
# change actually silences is the `warning` and `notice` tiers. The first test
# below therefore uses the REAL timer-failure path and its real identity key:
# regressing THAT is the failure that would hurt.
#
# Two pieces of prior feedback shape the assertions:
#   * a test asserting the message STRING cannot catch a wrong TIER -- so every
#     assertion below is about the DESTINATION and the routing decision, never
#     about the words in the message;
#   * an alert-noise fix once nearly shipped the noise it was removing -- so the
#     fallback is asserted from both directions: it must fire when the console
#     is unavailable, and it must NOT fire when the console worked.

CRITICAL_TIMER_LINE = (
    "timer job failing: morning-signal.timer (Result=exit-code, last run "
    "2026-08-28 05:30:00, next attempt 06:30:00)"
)
WARNING_BUDGET_LINE = "memory budget: BREACH (detail in journal)"

# The scenario bodies below are shell, appended to the extracted functions.
# `_open` pages a condition and persists the state file exactly as a real run
# does; `_resolve` is the NEXT run, in which the condition is gone.

_OPEN_CRITICAL_TIMER = f'''
_key=$(timer_failure_dedup_key "morning-signal.timer" "exit-code" "")
publish_problems critical 43200 "health alert" "{CRITICAL_TIMER_LINE}" "$_key"
ALERTED_NOW="${{ALERTED_NOW%$'\\n'}}"
finalize_alert_lifecycle ""
'''

_OPEN_WARNING = f'''
publish_problems warning 43200 "budget/coverage finding (no action urgent)" \\
    "{WARNING_BUDGET_LINE}" "" console
ALERTED_NOW="${{ALERTED_NOW%$'\\n'}}"
finalize_alert_lifecycle "{WARNING_BUDGET_LINE}"
'''

# A clean tick: nothing is confirmed, so ALERTED_NOW stays empty and every
# standing page is due a terminator. This is the most common recovery path
# there is, and the one the clears were arriving from.
_RESOLVE = '''
finalize_alert_lifecycle ""
'''


def _open_then_resolve(open_body, tmp_path, open_env=None, resolve_env=None):
    """Two consecutive runs against ONE state directory, as the box does it."""
    first = run_lifecycle(open_body, tmp_path, overrides=open_env)
    assert first.proc.returncode == 0, first.proc.stderr
    second = run_lifecycle(_RESOLVE, tmp_path, overrides=resolve_env)
    assert second.proc.returncode == 0, second.proc.stderr
    return first, second


class TestClearInheritsItsOpenersDestination:
    def test_a_critical_opener_still_clears_in_the_channel(self, tmp_path):
        """The regression that would actually hurt.

        Uses the REAL `timer job failing:` path -- publish_problems at
        `critical` with the I7677 identity-key override -- not a synthetic
        critical, because that is the path every RESOLVED message Brian has
        actually received came down. A terminator for a page he was woken for
        is OWED to him.
        """
        opened, resolved = _open_then_resolve(_OPEN_CRITICAL_TIMER, tmp_path)
        (key,) = opened.channel_pages.keys()
        assert opened.channel_pages[key] == "critical"
        assert key.startswith("boxhealth-critical-timerfail-morning-signal.timer")

        assert key in resolved.channel_clears, (
            "a page that buzzed the phone ended without a terminator in the "
            f"channel. Channel clears seen: {list(resolved.channel_clears)}"
        )
        assert not any(
            ln.startswith("clear: ") for ln in resolved.console_lines
        ), "a critical clear was diverted to the console instead of being sent"

    def test_a_warning_opener_clears_on_the_console_and_not_in_the_channel(
        self, tmp_path
    ):
        """The tier this change actually silences.

        The opener never buzzed -- krepis' phone-push set is {error, critical}
        -- so its terminator is a console record, not a fourth message.
        """
        opened, resolved = _open_then_resolve(_OPEN_WARNING, tmp_path)
        assert opened.channel_pages == {}, (
            "the warning tier published to the channel; it is console-routed "
            f"since I9044. Pages seen: {opened.channel_pages}"
        )
        assert resolved.channel_clears == {}, (
            "a terminator for a page that never buzzed was sent to the "
            "operator's channel anyway"
        )
        clears = [ln for ln in resolved.console_lines if ln.startswith("clear: ")]
        assert len(clears) == 1, (
            "the clear reached NEITHER surface -- that is the silent swallow "
            f"this change may not become. Console lines: {resolved.console_lines}"
        )
        assert "boxhealth-warning-" in clears[0], (
            "the console clear does not name the identity key of the page it "
            f"terminates: {clears[0]!r}"
        )

    @pytest.mark.parametrize(
        "severity,expected",
        [
            ("critical", "channel"),
            ("error", "channel"),
            ("warning", "console"),
            ("info", "console"),
            ("", "channel"),
        ],
    )
    def test_the_routing_predicate_is_total_and_fails_toward_the_channel(
        self, severity, expected, tmp_path
    ):
        """Run the shipped `clear_destination` over every severity in play.

        The EMPTY case is the one that matters: a state row whose severity field
        could not be read is a page we cannot prove was quiet, and an owed
        terminator is never withheld on a guess. Same direction as the severity
        classifier's default arm.
        """
        run = run_lifecycle(
            f'krepis_push_set_load\nclear_destination "{severity}" > "$FAKE_METRIC_LOG"\n',
            tmp_path,
        )
        assert run.proc.returncode == 0, run.proc.stderr
        assert run.metrics == [expected]

    def test_the_push_set_is_read_from_krepis_not_restated(self, tmp_path):
        """If krepis narrows its push set, the routing follows it.

        A hardcoded copy would keep sending `error` terminators into the channel
        for pages that had stopped buzzing -- this change's own defect,
        re-created by drift.
        """
        run = run_lifecycle(
            'krepis_push_set_load\nclear_destination "error" > "$FAKE_METRIC_LOG"\n',
            tmp_path,
            overrides={"FAKE_PUSH_SET": "critical"},
        )
        assert run.metrics == ["console"]


class TestTheFallbackIsTheInvariant:
    """alpha-engine-config-I7857: a route whose failure mode is "nothing was
    sent" is a silent swallow wearing a routing decision's clothes."""

    def test_a_console_failure_sends_the_clear_to_the_channel_after_all(
        self, tmp_path
    ):
        opened, resolved = _open_then_resolve(
            _OPEN_WARNING, tmp_path, resolve_env={"FAKE_CONSOLE_RC": "1"}
        )
        assert len(resolved.channel_clears) == 1, (
            "the console was unavailable and the terminator went nowhere"
        )
        assert "console route unavailable" in resolved.proc.stderr

    def test_a_console_failure_sends_the_deferred_warning_page_too(self, tmp_path):
        """Not only clears. The `warning` tier's page takes the same route and
        the same fallback, through the SAME publish_page call a channel-routed
        page uses -- a fallback that publishes by a different route is not proof
        the route still works."""
        run = run_lifecycle(
            _OPEN_WARNING, tmp_path, overrides={"FAKE_CONSOLE_RC": "1"}
        )
        assert list(run.channel_pages.values()) == ["warning"], (
            "the console was unavailable and the warning page reached nobody"
        )

    def test_a_working_console_publishes_nothing_to_the_channel(self, tmp_path):
        """The other direction, and the one an alert-noise fix gets wrong: the
        fallback must not fire when the console worked, or the change ships the
        noise it removes."""
        run = run_lifecycle(_OPEN_WARNING, tmp_path)
        assert run.calls == [], (
            f"the console worked and the channel was used anyway: {run.calls}"
        )
        assert any(
            WARNING_BUDGET_LINE in ln for ln in run.console_lines
        ), "the warning tier reached neither surface"

    def test_an_absent_console_emitter_falls_back_rather_than_returning_ok(
        self, tmp_path
    ):
        """`emit_hygiene_envelope`'s guards used to return 0. With the console
        as the sole delivery path, "the emitter is not installed" and "the
        envelope was published" must not be the same answer."""
        opened = run_lifecycle(_OPEN_WARNING, tmp_path)
        assert opened.proc.returncode == 0
        missing = run_lifecycle(
            'HYGIENE_EMITTER="/nonexistent/emit_box_health_hygiene.py"\n' + _RESOLVE,
            tmp_path,
        )
        assert len(missing.channel_clears) == 1, (
            "the emitter was missing, the console recorded nothing, and the "
            "terminator was dropped"
        )


class TestTheUnpublishedGaugeStaysHonest:
    """`health_clears_unpublished` is the number whose non-zero is the finding.
    Inflating it with successful deliveries retires it."""

    def test_a_console_routed_clear_is_delivered_not_unpublished(self, tmp_path):
        _, resolved = _open_then_resolve(_OPEN_WARNING, tmp_path)
        assert resolved.clears_unpublished == 0

    def test_a_clear_that_reached_neither_surface_is_counted(self, tmp_path):
        """Console down AND the channel publish failing is the one case the
        gauge exists for."""
        _, resolved = _open_then_resolve(
            _OPEN_WARNING,
            tmp_path,
            resolve_env={"FAKE_CONSOLE_RC": "1", "FAKE_CLEAR_RC": "1"},
        )
        assert resolved.clears_unpublished == 1

    def test_a_failed_channel_clear_is_still_counted(self, tmp_path):
        """The pre-existing behaviour for a critical opener, unchanged."""
        _, resolved = _open_then_resolve(
            _OPEN_CRITICAL_TIMER, tmp_path, resolve_env={"FAKE_CLEAR_RC": "1"}
        )
        assert resolved.clears_unpublished == 1

    def test_the_gauge_is_published_on_a_run_with_no_clears_at_all(self, tmp_path):
        """Zero is published too. A gauge that only appears when it is non-zero
        cannot be told from a dead emitter."""
        run = run_lifecycle(_RESOLVE, tmp_path)
        assert run.clears_unpublished == 0


class TestWhatPublishClearsAlreadyGotRight:
    """Regression cover for the properties the rewrite had to preserve."""

    def test_every_gone_key_is_attempted_not_only_the_first(self, tmp_path):
        """The `</dev/null` property. The loop is fed by a here-string, and a
        child that reads stdin eats the remaining keys -- every clear after the
        first would silently never be attempted, and nothing else in the system
        would say so."""
        body = '''
_a=$(timer_failure_dedup_key "alpha.timer" "exit-code" "")
_b=$(timer_failure_dedup_key "beta.timer" "exit-code" "")
publish_problems critical 43200 "health alert" "timer job failing: alpha.timer (x)" "$_a"
publish_problems critical 43200 "health alert" "timer job failing: beta.timer (x)" "$_b"
ALERTED_NOW="${ALERTED_NOW%$'\\n'}"
finalize_alert_lifecycle ""
'''
        opened, resolved = _open_then_resolve(body, tmp_path)
        assert len(opened.channel_pages) == 2
        assert len(resolved.channel_clears) == 2, (
            "only the first key was cleared -- the loop's stdin was eaten. "
            f"Cleared: {list(resolved.channel_clears)}"
        )

    def test_the_diff_is_taken_on_the_key_not_the_message_text(self, tmp_path):
        """A page whose message text changed while its identity key did not is
        the SAME condition, still standing, and must not be cleared."""
        opened = run_lifecycle(_OPEN_CRITICAL_TIMER, tmp_path)
        (key,) = opened.channel_pages.keys()
        restated = run_lifecycle(
            f'''
publish_problems critical 43200 "health alert" \\
    "timer job failing: morning-signal.timer (Result=exit-code, last run 2026-08-28 06:30:00, next attempt 07:30:00)" "{key}"
ALERTED_NOW="${{ALERTED_NOW%$'\\n'}}"
finalize_alert_lifecycle ""
''',
            tmp_path,
        )
        assert restated.channel_clears == {}, (
            "the message text changed and the condition was declared over"
        )

    def test_the_state_write_happens_after_the_fallback_reads_it(self, tmp_path):
        """Ordering inside finalize_alert_lifecycle, proven behaviourally.

        `publish_page` derives `--state` from the state file. If
        `alerted_state_write` ran before the fallback, a page opening for the
        first time would be published as `still_open` -- the lifecycle metadata
        alert_drain_ingest.py pairs on would be wrong exactly when the console
        is down.
        """
        run = run_lifecycle(
            _OPEN_WARNING, tmp_path, overrides={"FAKE_CONSOLE_RC": "1"}
        )
        (key,) = run.channel_pages.keys()
        assert run.page_state(key) == "opened", (
            "a first-ever page was published as a repeat, which means the state "
            "file was rewritten before the fallback read it"
        )

    def test_an_empty_prior_state_clears_nothing(self, tmp_path):
        """Empty means "nothing was alerted", never "clear everything" -- the
        first run after a deploy, a reboot, or a recreated state dir."""
        run = run_lifecycle(_RESOLVE, tmp_path)
        assert run.channel_clears == {}
        assert [ln for ln in run.console_lines if ln.startswith("clear: ")] == []


# ── the `clear` tier on the envelope ───────────────────────────────────────

CLEAR_LINE = (
    "clear: boxhealth-warning-memory_budget:_BREACH — warning page resolved: "
    "memory budget: BREACH (detail in journal)"
)
OTHER_CLEAR_LINE = (
    "clear: boxhealth-warning-coverage_gap — warning page resolved: "
    "watchdog: 3 timers have no dead-man row"
)


def test_a_clear_is_its_own_tier_not_a_warning():
    """A two-way split would have counted every resolution as a breach.

    `build_summary` names the warning count separately precisely so a reader can
    tell a declared-invariant breach from hygiene; a resolution counted as a
    breach is worse than no count at all.
    """
    notices, warnings, clears = emitter.split_tiers(
        [TIMER_NOTICE, WARNING_LINE, CLEAR_LINE]
    )
    assert notices == [TIMER_NOTICE]
    assert warnings == [WARNING_LINE]
    assert clears == [CLEAR_LINE]


def test_a_clear_is_keyed_on_the_identity_of_the_page_it_terminates():
    """Not on truncated message text, which two clears can share."""
    findings = emitter.build_findings([CLEAR_LINE, OTHER_CLEAR_LINE], {}, now=NOW)
    keys = [f["key"] for f in findings]
    assert keys == [
        "clear/boxhealth-warning-memory_budget:_BREACH",
        "clear/boxhealth-warning-coverage_gap",
    ]


def test_a_clear_carries_no_standing_age():
    """It is an event, not a standing finding. "first seen this run" would state
    the obvious while implying it could still be standing tomorrow."""
    findings = emitter.build_findings([CLEAR_LINE], {}, now=NOW)
    assert "standing" not in findings[0]["detail"]
    assert findings[0]["detail"] == CLEAR_LINE


def test_a_run_that_only_resolved_things_is_ok_and_says_so(monkeypatch, tmp_path):
    """`ok`, not `attention`: nothing is standing.

    A row turning amber to report that something ENDED would re-create on this
    surface the "second alert for a non-event" the clear routing removes from
    the channel — and the resolution must still be VISIBLE, or the console has
    swallowed what the channel stopped sending.
    """
    captured = _emit_with_fake_fcr([CLEAR_LINE], monkeypatch, tmp_path)
    assert captured["status"] == "ok"
    assert [f["key"] for f in captured["findings"]] == [
        "clear/boxhealth-warning-memory_budget:_BREACH"
    ]
    assert "1 alert resolved this run" in captured["summary"]
    assert captured["summary"].startswith("no standing findings")


def test_a_clear_does_not_inflate_the_standing_count(monkeypatch, tmp_path):
    """The summary is the one line an operator reads. A resolution counted as a
    standing finding makes the board report the opposite of what happened."""
    captured = _emit_with_fake_fcr(
        [WARNING_LINE, TIMER_NOTICE, CLEAR_LINE], monkeypatch, tmp_path
    )
    assert captured["status"] == "attention"
    assert "2 standing findings" in captured["summary"]
    assert "(1 warning)" in captured["summary"]
    assert "1 alert resolved this run" in captured["summary"]
    assert len(captured["findings"]) == 3


def test_a_clear_never_enters_the_first_seen_age_map(monkeypatch, tmp_path):
    """Otherwise the map grows by one entry per resolution and drops them all
    again on the next run — churn on a file whose whole job is to be stable."""
    path = tmp_path / "hygiene-first-seen.json"
    _emit_with_fake_fcr([WARNING_LINE, CLEAR_LINE], monkeypatch, tmp_path)
    assert list(emitter.load_first_seen(path)) == [WARNING_LINE]


def _emit_with_fake_fcr(lines, monkeypatch, tmp_path):
    captured = {}

    class _FakeFCR:
        STATUS_OK = "ok"
        STATUS_ATTENTION = "attention"
        STATUS_ERROR = "error"

        @staticmethod
        def emit_result(**kwargs):
            captured.update(kwargs)
            return "s3://bucket/key"

    import sys as _sys
    import types as _types

    fake_pkg = _types.ModuleType("nousergon_lib")
    fake_pkg.fleet_check_result = _FakeFCR
    monkeypatch.setitem(_sys.modules, "nousergon_lib", fake_pkg)
    monkeypatch.setitem(_sys.modules, "nousergon_lib.fleet_check_result", _FakeFCR)
    monkeypatch.setattr(emitter, "FIRST_SEEN_PATH", tmp_path / "hygiene-first-seen.json")
    monkeypatch.setattr("sys.stdin", _Stdin("\n".join(lines)))
    assert emitter.main([]) == 0
    return captured


def test_the_docstring_no_longer_claims_the_drain_is_dead():
    """The correction itself, pinned.

    An instruction-bearing docstring is load-bearing: this one's claim that the
    delegated consumer was not running is the entire reason the `warning` tier
    stayed on the channel for eight days. Re-measured 2026-08-28, the drain runs
    daily on an event-time leg. A stale claim left in place would be re-read as
    a live blocker by the next agent.
    """
    doc = emitter.__doc__
    assert "alpha-engine-freshness-monitor-cron" in doc, (
        "the docstring does not name the trigger the drain actually runs on"
    )
    assert "drain_ledger" in doc, "the correction cites no evidence"
    stale = "The delegated consumer is not\nrunning on a schedule."
    assert doc.count(stale) == 1, (
        "the superseded claim appears more than once, so at least one instance "
        "is stated outside the quoted WAS block as current fact"
    )
    was, now = doc.index("WAS (this docstring"), doc.index("NOW (re-measured")
    assert was < doc.index(stale) < now, (
        "the superseded claim is stated outside the quoted WAS block. A "
        "correction has to leave the old claim readable as HISTORY -- deleting "
        "it silently is how the same measurement gets re-made every month -- "
        "but it may not be left standing as fact."
    )
