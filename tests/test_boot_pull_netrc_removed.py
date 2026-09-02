"""boot-pull must never hydrate a GitHub credential dotfile (alpha-engine-config-I9739).

git sets CURLOPT_NETRC to CURL_NETRC_OPTIONAL unconditionally, so libcurl
answers GitHub's 401 challenge out of that dotfile *before* git ever consults
a configured credential helper -- the dotfile does not compete with
`git-credential-nousergon-app`, it outranks it. Measured live 2026-09-01 on
this exact box (i-09b539c844515d549): the helper was installed, configured,
and provably able to mint a token, and was never consulted because boot-pull
rewrote the dotfile on every run.

Source-text assertions, same idiom as test_boot_pull_failure_reporting.py and
test_boot_pull_health_gate.py: the script runs as root on the box and pulls
live repos, so executing it in CI is not meaningful -- these pin the
contract.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOOT_PULL = REPO_ROOT / "infrastructure" / "boot-pull.sh"


def _src() -> str:
    return BOOT_PULL.read_text()


class TestNetrcNeverHydrated:
    def test_no_ssm_read_of_github_token(self):
        # The whole hydration block is gone, not merely disabled behind a
        # flag -- guard the SSM parameter name directly so a copy-paste from
        # another script (or from git history) cannot reintroduce it.
        assert "/alpha-engine/GITHUB_TOKEN" not in _src()

    def test_no_netrc_write(self):
        text = _src()
        assert "machine github.com login" not in text
        assert "NEW_NETRC" not in text
        assert "GH_TOKEN" not in text

    def test_netrc_is_actively_removed(self):
        # A box that still has one from an earlier boot is a box where the
        # helper is installed and silently shadowed -- exactly the measured
        # failure. Deleting it every run, not just refusing to write a new
        # one, is what makes the fix durable across the next boot.
        text = _src()
        assert 'NETRC="/home/ec2-user/.netrc"' in text
        assert "rm -f \"$NETRC\"" in text


class TestCredentialHelperAsserted:
    def test_helper_binary_checked(self):
        text = _src()
        assert "/usr/local/bin/git-credential-nousergon-app" in text
        assert '[ ! -x "$CRED_HELPER" ]' in text

    def test_helper_check_invoked(self):
        text = _src()
        assert '"$CRED_HELPER" --check nous-ergon-ops' in text

    def test_auth_failure_uses_the_existing_accumulation_idiom(self):
        # Deliverable 3's constraint: reuse the script's existing
        # failure-accumulation mechanism (PULL_FAILURES / FAILED_REPOS ->
        # the end-of-script krepis.alerts publish), not a second one.
        text = _src()
        auth_start = text.index("Authenticate to GitHub via the App credential helper")
        auth_end = text.index("Repos the micro needs at runtime")
        auth_block = text[auth_start:auth_end]
        assert "PULL_FAILURES=$((PULL_FAILURES + 1))" in auth_block
        assert 'FAILED_REPOS+=("auth:helper-missing")' in auth_block
        assert 'FAILED_REPOS+=("auth:helper-check-failed")' in auth_block

    def test_failure_counters_declared_before_the_auth_check(self):
        # PULL_FAILURES/FAILED_REPOS moved up from immediately-before-the-loop
        # so the auth check (which now runs first and can fail) can
        # accumulate into them under `set -u` without an unbound-variable
        # error.
        text = _src()
        decl_idx = text.index("PULL_FAILURES=0")
        auth_idx = text.index("Authenticate to GitHub via the App credential helper")
        assert decl_idx < auth_idx
        # And it must not be declared a second time later (which would zero
        # out a failure the auth check just recorded).
        assert text.count("PULL_FAILURES=0") == 1
        assert text.count("FAILED_REPOS=()") == 1
