"""boot-pull's T1-3 health gate + auto-revert contract (config-I6742).

shared-application-host-policy.md §5 T1-3 sets the floor for any path that
mutates code on the live box: the SHA moved to is recorded to a state file,
services restarted are health-checked afterward, and a failed check reverts
to the previous SHA automatically. deploy-on-merge.sh has carried all three
since config-I5250; boot-pull — the daily multi-repo safety net that
`git reset --hard origin/main`s five repos and restarts changed units — had
none of them: a unit-sync restart could leave a service dead and boot-pull
still exited 0.

Source-text assertions, same idiom (and same rationale) as
test_boot_pull_failure_reporting.py: the script runs as root on the box and
pulls live repos, so executing it in CI is not meaningful — these pin the
contract.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOOT_PULL = REPO_ROOT / "infrastructure" / "boot-pull.sh"


def _src() -> str:
    return BOOT_PULL.read_text()


def _gate_block() -> str:
    """From the health-gate banner to the PULL_FAILURES reporting branch."""
    text = _src()
    start = text.index("T1-3 post-restart health gate")
    end = text.index('if [ "$PULL_FAILURES" -gt 0 ]')
    return text[start:end]


class TestRestartAccounting:
    def test_unit_sync_restarts_are_gated(self):
        # Every restart in the CHANGED_UNITS loop must be recorded for the
        # gate — including ones whose `systemctl restart` itself errored,
        # which are exactly the ones the gate must fail loud on.
        text = _src()
        loop = text[text.index('for unit in "${CHANGED_UNITS[@]}"'):]
        loop = loop[: loop.index("done")]
        assert 'RESTARTED_SERVICES+=("$unit")' in loop

    def test_config_driven_streamlit_restarts_are_gated(self):
        text = _src()
        block = text[text.index('if [ "$CONFIGS_CHANGED" -eq 1 ]'):]
        block = block[: block.index("\nfi")]
        assert "RESTARTED_SERVICES+=" in block

    def test_prev_sha_is_captured_for_revert(self):
        # The revert target is this run's pre-pull HEAD, captured in the pull
        # loop — not a guess like HEAD~1 (see deploy-on-merge.sh's
        # LAST_GOOD_SHA_FILE comment for why guessing is wrong).
        text = _src()
        assert 'PULLED_PREV+=("$PREV_SHA")' in text


class TestHealthGate:
    def test_gate_polls_is_active(self):
        block = _gate_block()
        assert "systemctl is-active --quiet" in block

    def test_gate_skips_oneshot_units(self):
        # Restarting a Type=oneshot unit RUNS it; is-active is meaningless
        # for a job. The gate must scope itself to long-running services.
        block = _gate_block()
        assert "oneshot" in block

    def test_gate_failure_exits_nonzero(self):
        block = _gate_block()
        assert re.search(r'if \[ -n "\$FAILED_UNITS" \]; then\n(.*\n)*?\s*exit 1', block), (
            "a failed health gate must exit nonzero so systemd sees the "
            "failure — an exit 0 after a dead service is the defect this "
            "gate exists to remove"
        )


class TestAutoRevert:
    def test_revert_resets_to_previous_sha(self):
        block = _gate_block()
        assert 'reset --hard "$_prev"' in block

    def test_revert_refuses_a_garbage_sha(self):
        # Reverting to a guess is worse than not reverting (same rule as
        # deploy-on-merge.sh revert_to_last_good).
        block = _gate_block()
        assert "*[!0-9a-f]*" in block

    def test_revert_resyncs_unit_files_from_reverted_tree(self):
        # New-sha units over old-sha code is a state neither sha was tested
        # in — the 2026-07-28 run-30404044358 failure mode.
        block = _gate_block()
        assert "REVERT-SYNC" in block
        assert "daemon-reload" in block

    def test_gate_failure_publishes_a_critical_alert(self):
        block = _gate_block()
        assert "krepis.alerts publish" in block
        assert "--severity critical" in block
        assert "--source boot-pull" in block

    def test_failure_to_alert_is_itself_logged(self):
        # Same I4509 contract as the PULL_FAILURES branch: a broken reporter
        # must be loud in the log, never swallowed.
        block = _gate_block()
        assert "UNREPORTED" in block


class TestShaStateFile:
    def test_sha_set_is_recorded_to_a_state_file(self):
        text = _src()
        assert "/var/lib/boot-pull/last-pull-shas" in text
        assert "rev-parse HEAD" in _gate_block()

    def test_state_file_write_is_atomic(self):
        block = _gate_block()
        assert '.tmp"' in block and "mv " in block


class TestOrdering:
    def test_gate_runs_after_every_restart_source(self):
        # The gate must see the config-driven streamlit restarts too, so it
        # sits after the CONFIGS_CHANGED block and before failure reporting.
        text = _src()
        gate = text.index("T1-3 post-restart health gate")
        assert gate > text.index('if [ "$CONFIGS_CHANGED" -eq 1 ]')
        assert gate < text.index('if [ "$PULL_FAILURES" -gt 0 ]')
