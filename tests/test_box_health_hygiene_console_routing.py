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
from datetime import datetime, timedelta, timezone

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
EMITTER_PATH = REPO / "infrastructure" / "emit_box_health_hygiene.py"
BOX_HEALTH = (REPO / "infrastructure" / "box_health.sh").read_text()

_spec = importlib.util.spec_from_file_location("emit_box_health_hygiene", EMITTER_PATH)
emitter = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(emitter)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

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

    `box_health.sh` exits early when nothing is confirmed. If the emitter were
    called only from the bottom of the script, the envelope would publish ONLY
    on runs that already found a problem — and a surface that publishes only
    when something is wrong cannot be distinguished from one that has died.
    """
    calls = BOX_HEALTH.count("emit_hygiene_envelope ")
    definition = BOX_HEALTH.count("emit_hygiene_envelope() {")
    assert definition == 1, "the emitter helper is defined more than once"
    assert calls - definition >= 3, (
        f"found {calls - definition} call site(s); expected at least 3 — the two "
        "all-healthy early exits and the final partitioned publish. A missing "
        "early-exit call means a clean box publishes nothing, which the console "
        "cannot distinguish from a dead emitter."
    )


def test_the_helper_is_defined_before_its_first_call_site():
    """bash is linear: a call above the definition is a runtime failure.

    Pinned because it is invisible to `bash -n`, which parses without executing,
    and the two early-exit call sites sit hundreds of lines above the tier
    partition where the helper would most naturally have been written.
    """
    definition = BOX_HEALTH.index("emit_hygiene_envelope() {")
    first_call = BOX_HEALTH.index('emit_hygiene_envelope ""')
    assert definition < first_call, (
        "emit_hygiene_envelope is called before it is defined; every early-exit "
        "run would die on `command not found`."
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


def test_the_warning_tier_keeps_its_channel_publish():
    """It may NOT simply follow `notice` off the channel.

    `warning`'s claim to being quiet is that it is DELEGATED — it reaches the
    Overseer intake bus as alert class `box-health` (`intake: bus` / `response:
    drain-queue`), so a human is not the only reader. Measured live 2026-08-20:
    all four `alpha-engine-alert-drain-{0400,1000,1600,2200}utc` EventBridge
    schedules are DISABLED under the 2026-08-07 automation pause (I6984), and
    the drain's own registry row states no cadence is auditable from what
    remains.

    So the delegated consumer is not running on a schedule. Removing this tier
    from the channel today would leave it with NO reader — arriving dressed as
    consistency with the notice change. Re-examine when the drain is unpaused
    (alpha-engine-config-I7858).
    """
    assert "publish_problems warning " in BOX_HEALTH, (
        "the warning tier lost its channel publish while the Overseer drain "
        "schedules are disabled — the finding would reach nobody."
    )


def test_the_warning_tier_also_reaches_the_console():
    """In addition, never instead. The console is what makes the long channel
    window safe: the standing set is visible continuously with each finding's
    age, rather than having to be remembered between repeats."""
    i = BOX_HEALTH.index("emit_hygiene_envelope \"$(")
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
    notices, warnings = emitter.split_tiers([TIMER_NOTICE, WARNING_LINE, MEMORY_NOTICE])
    assert notices == [TIMER_NOTICE, MEMORY_NOTICE]
    assert warnings == [WARNING_LINE]
    assert '"notice: "*) echo info ;;' in BOX_HEALTH, (
        "the shell classifier no longer keys on the `notice: ` prefix that "
        "split_tiers mirrors; the two have drifted."
    )
