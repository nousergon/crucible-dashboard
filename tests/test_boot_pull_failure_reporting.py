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


class TestHelperCheckReasonReachesTheAlert:
    """alpha-engine-config-I13105: on 2026-11-02 the `--check` failed twice
    and the alert said only `auth:helper-check-failed ... See
    /var/log/boot-pull.log`, a file nothing off the box can read. The helper's
    own last stderr line (which names the failing step and never carries a
    token) now rides in the alert BODY, while the dedup key stays the
    FAILED_REPOS set it always was."""

    @staticmethod
    def _auth_block() -> str:
        text = BOOT_PULL.read_text()
        start = text.index('AUTH_CHECK_DETAIL=""')
        end = text.index("unset _cred_check_out")
        return text[start:end + len("unset _cred_check_out")]

    def _run(self, tmp_path, helper_body: str):
        import subprocess

        helper = tmp_path / "fake-helper"
        helper.write_text("#!/usr/bin/env bash\n" + helper_body + "\n")
        helper.chmod(0o755)
        log = tmp_path / "boot-pull.log"
        script = (
            "set -uo pipefail\n"
            f'LOG="{log}"\n'
            'log() { echo "$*" >> "$LOG"; }\n'
            "PULL_FAILURES=0\nFAILED_REPOS=()\n"
            f'CRED_HELPER="{helper}"\n'
            + self._auth_block()
            + '\nprintf "%s|%s|%s" "$PULL_FAILURES" "${FAILED_REPOS[*]}" "$AUTH_CHECK_DETAIL"\n'
        )
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        return res.stdout, log.read_text()

    def test_failed_check_records_the_helpers_reason(self, tmp_path):
        out, log = self._run(
            tmp_path,
            'echo "git-credential-nousergon-app: token cache MISS for repo=nous-ergon-ops: no cached entry" >&2\n'
            'echo "git-credential-nousergon-app: cannot read /alpha-engine/groom/github_app_id" >&2\n'
            "exit 1",
        )
        failures, repos, detail = out.split("|", 2)
        assert failures == "1"
        assert repos == "auth:helper-check-failed"
        assert detail == "git-credential-nousergon-app: cannot read /alpha-engine/groom/github_app_id"
        assert "token cache MISS" in log  # full helper output still lands in the log
        assert "FAIL" in log and "cannot read /alpha-engine/groom/github_app_id" in log

    def test_passing_check_sets_no_detail(self, tmp_path):
        out, log = self._run(tmp_path, 'echo "git-credential-nousergon-app: OK — minted"; exit 0')
        assert out == "0||"
        assert "OK — minted" in log

    def test_detail_is_in_the_message_not_the_dedup_key(self):
        block = _failure_block()
        msg_line = next(l for l in block.splitlines() if "--message" in l)
        assert "${AUTH_CHECK_DETAIL:+" in msg_line
        dkey_line = next(l for l in block.splitlines() if l.strip().startswith("_dkey="))
        assert "AUTH_CHECK_DETAIL" not in dkey_line
