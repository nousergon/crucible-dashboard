"""boot-pull.sh reads its checkout list from the declared roster, and never
resets a dirty tree (alpha-engine-config-I10260).

WHAT HAPPENED
-------------
boot-pull.sh pulled a hardcoded five-repo REPOS array while
nous-ergon-ops' check_ops_checkout_freshness.py graded every one of the 19
checkouts actually on the box — two lists nobody kept in sync, so 14 drifted
on a rolling basis with only the checker's own `attention` status ever
noticing.

The fix is `checkout-roster.json` (nous-ergon-ops), read here directly off
disk (this is a DIFFERENT repo from the checker, so no Python import path
exists between them — `jq` is the shared interface). This file also drops
`git reset --hard`, which discards a dirty working tree with no record of
what was lost, in favor of fetch + a dirty-tree check + `merge --ff-only`.

SCOPE, stated because it bounds the guarantee
---------------------------------------------
Source-text assertions, same idiom as test_boot_pull_health_gate.py and
test_boot_pull_failure_reporting.py: the script runs as root on the box
against /home/ec2-user paths, so executing it in CI is not meaningful — what
is pinned is the CONTRACT the source text encodes.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOOT_PULL = REPO_ROOT / "infrastructure" / "boot-pull.sh"


def _src() -> str:
    return BOOT_PULL.read_text()


def _roster_block() -> str:
    text = _src()
    start = text.index("declared checkout roster")
    end = text.index('for repo in "${REPOS[@]}"')
    return text[start:end]


def _strip_comments(text: str) -> str:
    """Full-line comments only (same convention as
    test_every_installed_timer_has_a_deadman_row.py) — truncating at the
    first `#` anywhere would also cut real code following a `#` inside a
    quoted string."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _pull_loop_block() -> str:
    """The `for repo in "${REPOS[@]}"; do ... done` loop body, CODE ONLY
    (comments stripped — this section's own explanatory comments mention
    `reset --hard` and `exit 3` by name, which would otherwise false-positive
    the assertions below). Found by its unindented closing `done` — the loop
    contains its own nested `for`/`done` (the pip-install disk-guard loop),
    so the FIRST `done` after the opening line is the wrong one."""
    text = _strip_comments(_src())
    start = text.index('for repo in "${REPOS[@]}"')
    m = re.search(r"^done$", text[start:], re.MULTILINE)
    assert m, "no unindented closing `done` found for the REPOS pull loop"
    return text[start:start + m.end()]


class TestRosterDerivesRepos:
    def test_repos_is_built_from_the_roster_not_a_literal_array(self):
        block = _roster_block()
        assert "jq -r" in block, "REPOS is not derived from checkout-roster.json via jq"
        assert 'checkouts[]' in block

    def test_only_managed_entries_are_included(self):
        block = _roster_block()
        assert 'select(.managed != false)' in block, (
            "the jq filter does not exclude `managed: false` roster rows — an "
            "unmanaged checkout (e.g. the-cyphering-ops) would be pulled anyway"
        )

    def test_a_missing_roster_fails_loud_not_silent(self):
        block = _roster_block()
        assert '! -r "$CHECKOUT_ROSTER"' in block
        assert "PULL_FAILURES=$((PULL_FAILURES + 1))" in block
        assert 'FAILED_REPOS+=("roster:unreadable")' in block

    def test_an_empty_roster_parse_fails_loud(self):
        block = _roster_block()
        assert "${#REPOS[@]} -eq 0" in block
        assert 'FAILED_REPOS+=("roster:empty")' in block

    def test_roster_path_points_at_the_nous_ergon_ops_checkout(self):
        block = _roster_block()
        assert "nous-ergon-ops/alpha-engine-dashboard/live/infrastructure/checkout-roster.json" in block


class TestNeverResetADirtyTree:
    def test_reset_hard_is_gone_from_the_pull_loop(self):
        loop = _pull_loop_block()
        assert "reset --hard" not in loop, (
            "boot-pull still resets --hard in its main pull loop — this discards "
            "uncommitted work with no record, exactly what alpha-engine-config-I10260 "
            "removes"
        )

    def test_pull_is_fast_forward_only(self):
        loop = _pull_loop_block()
        assert "merge --ff-only origin/main" in loop

    def test_merge_is_attempted_before_any_dirty_verdict(self):
        """The fast-forward is ATTEMPTED and git's refusal is the finding.

        The first shape of this check pre-empted the merge with a blanket
        `git status --porcelain` test and, on its first run (2026-09-13
        15:39 UTC), graded 10 of 19 checkouts "dirty" and paged the operator
        chat — every one held only files the box writes into its checkouts
        by design (the SSM-rendered config.yaml, a .venv, a sqlite file) and
        every one would have fast-forwarded cleanly. So: merge first; only a
        refused merge consults the tree, and then only TRACKED modifications
        (`--untracked-files=no`) make it a dirty-tree verdict.
        """
        loop = _pull_loop_block()
        merge_idx = loop.index("merge --ff-only origin/main")
        status_idx = loop.index("git status --porcelain --untracked-files=no")
        exit_idx = loop.index("exit 3")
        assert merge_idx < status_idx < exit_idx, (
            "the dirty-tree verdict must follow a REFUSED merge, never pre-empt it"
        )
        assert "git status --porcelain)" not in loop, (
            "a blanket porcelain test (untracked files included) is back — that is "
            "the 10-of-19 false-dirty page of 2026-09-13"
        )

    def test_dirty_tree_is_reported_not_silently_skipped(self):
        loop = _pull_loop_block()
        assert re.search(r'if \[ "\$_pull_rc" -eq 3 \]; then\s*\n\s*log "WARN', loop), (
            "a dirty-tree exit (3) is not distinguished and logged — it must read "
            "as its own reported condition, not fold into the generic git-failure branch"
        )
        assert "boot-pull never discards local work" in loop

    def test_dirty_tree_still_counts_as_a_reported_failure(self):
        """Reported means it reaches the existing alert path — not a second,
        silent bucket that never pages."""
        loop = _pull_loop_block()
        block = loop[loop.index('else\n        _pull_rc=$?'):]
        assert "PULL_FAILURES=$((PULL_FAILURES + 1))" in block

    def test_a_genuine_diverged_history_is_a_distinct_message_from_dirty(self):
        loop = _pull_loop_block()
        assert "fetch/fast-forward failed" in loop
