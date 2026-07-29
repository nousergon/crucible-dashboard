"""boot-pull's FAILURE path must actually report (alpha-engine-config-I4509).

These are the tests that were missing. boot-pull's failure reporter was broken
for an unknown length of time and nobody noticed, because the only path anyone
ever exercised was the happy one -- where the reporter is never called at all.

Two independent faults were live simultaneously, either one fatal:

  1. `flow_doctor.init()` does not exist in flow-doctor 0.8.7 (it exports
     FlowDoctor / FlowDoctorBuilder and no `init`), so the call raised
     AttributeError on every failure.
  2. The heredoc hydrated 4 env vars while flow-doctor.yaml references 8, so
     construction would have raised ConfigError on TELEGRAM_BOT_TOKEN even
     after fixing (1).

Source-text assertions, deliberately: the reporting path lives inside a bash
script that runs as root on an EC2 box and pulls 6 git repos. Executing it in
CI is not meaningful, so these pin the CONTRACT -- that a real alert CLI is
invoked on failure, and that a failure to alert is itself logged loudly.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOOT_PULL = REPO_ROOT / "infrastructure" / "boot-pull.sh"


def _failure_block() -> str:
    """The `if [ "$PULL_FAILURES" -gt 0 ]` branch."""
    text = BOOT_PULL.read_text()
    start = text.index('if [ "$PULL_FAILURES" -gt 0 ]')
    end = text.index('log "=== boot-pull completed successfully ==="')
    return text[start:end]


class TestFailurePathReports:
    def test_failure_branch_invokes_an_alert_cli(self):
        # The contract: a repo pull failure must reach a human, not just the
        # log file. Whatever the mechanism, SOMETHING must be invoked here.
        block = _failure_block()
        assert "krepis.alerts publish" in block, (
            "boot-pull's failure branch must publish an alert. If the alert "
            "mechanism is being changed, update this test to the new one -- do "
            "not delete the assertion."
        )

    def test_alert_carries_severity_and_source(self):
        block = _failure_block()
        assert "--severity error" in block
        assert "--source boot-pull" in block, (
            "the alert must identify boot-pull as the source, or triage starts "
            "from 'something on the box failed'"
        )

    def test_alert_names_the_failing_repos(self):
        # An alert that says "boot-pull failed" without saying WHAT failed
        # sends you to the log file, which is the thing that was insufficient.
        block = _failure_block()
        assert "${FAILED_REPOS[*]}" in block

    def test_failure_to_alert_is_itself_logged(self):
        # The defect this whole file exists for: the reporter failed silently.
        # If publishing breaks again, that must be loud in the log rather than
        # swallowed by `|| true`.
        block = _failure_block()
        assert "UNREPORTED" in block, (
            "a failure to publish the alert must be logged explicitly -- this "
            "is the exact defect (I4509) these tests guard"
        )
        assert "|| true" not in block, (
            "`|| true` on the alert call is what let the reporter fail "
            "silently for an unknown length of time"
        )

    def test_failure_branch_still_exits_nonzero(self):
        # systemd must see the failure regardless of whether alerting worked.
        block = _failure_block()
        assert re.search(r"^\s*exit 1\s*$", block, re.M)


class TestNoResurrectedFlowDoctorInit:
    def test_flow_doctor_init_is_not_called(self):
        # `flow_doctor.init` has not existed for some time. Guarding the name
        # directly so a copy-paste from another script cannot reintroduce it.
        #
        # Comment lines are stripped first: the script explains at length WHY
        # this API is gone, and a guard that fires on its own documentation
        # would push the next person to delete the explanation.
        text = "\n".join(
            line for line in BOOT_PULL.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "flow_doctor.init(" not in text, (
            "flow_doctor.init() does not exist in flow-doctor 0.8.7 -- it "
            "exports FlowDoctor/FlowDoctorBuilder. Use FlowDoctor.from_config, "
            "or preferably krepis.alerts (the canonical CLI, config#1649)."
        )

    def test_no_partial_env_hydration_list(self):
        # The second fault: a hardcoded list of secrets to hydrate, which
        # drifted to 4 while flow-doctor.yaml grew to 8. Any reintroduced
        # hydration list must cover every ${VAR} the config actually uses.
        text = BOOT_PULL.read_text()
        if "GMAIL_APP_PASSWORD" not in text:
            return  # no hydration list at all -- nothing to drift

        cfg = REPO_ROOT / "flow-doctor.yaml"
        if not cfg.exists():
            return
        referenced = set(re.findall(r"\$\{([A-Z_]+)\}", cfg.read_text()))
        missing = {v for v in referenced if v not in text}
        assert not missing, (
            f"boot-pull hydrates secrets for flow-doctor but omits {sorted(missing)}, "
            f"which flow-doctor.yaml references. Construction will fail with "
            f"ConfigError. Either hydrate every referenced var or use a "
            f"mechanism that resolves its own secrets."
        )
