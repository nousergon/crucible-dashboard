"""Anti-fork guard for the G16 spot-bootstrap collapse (alpha-engine-config-I7372).

`infrastructure/spot_backtest.sh` and `infrastructure/spot_train.sh` used to
hand-carry their own inline bootstrap heredoc — interpreter install, repo
clone(s), systemd watchdog/timer, SSM dispatch — the exact duplication
`krepis.spot_bootstrap` now renders once for the whole fleet
(nousergon-data#1294/#1296, crucible-predictor#461/#462/#463: three defects
that reached sibling copies of this same heredoc hours to days apart). Both
launchers in this repo now call `krepis.spot_bootstrap.render(...)` instead.

Three layers, because no single one covers the whole fork surface:

1. `scan_for_inline_bootstraps()` — the fleet's canonical behavioural
   detector (>=2 signature categories: supervision / interpreter / checkout /
   dispatch). Derived from the tree, not enumerated: it does not name this
   repo's files, so a fork under a new name or a renamed function prefix
   still trips it.

   KNOWN GAP (alpha-engine-config-I7378, filed 2026-08-14, not fixed here —
   editing krepis is out of scope for this PR): `scan_for_inline_bootstraps`
   clears an ENTIRE FILE on a single `-m krepis.spot_bootstrap` delegate
   match, before any signature is evaluated. A heredoc bootstrap reintroduced
   ALONGSIDE a legitimate render call in the same file is invisible to it.
   Both files in this repo now carry that delegate marker permanently, so
   this repo relies on layer 3 below to catch that specific case until
   I7378 lands.

2. A repo-local regex over `infrastructure/**/*.sh` for the silent
   `command -v python3.12 ... || <VAR>=python3` interpreter fallback
   (alpha-engine-config-I7372 defect 1). This is narrower than a "bootstrap"
   and can reappear in a single `run_ssm` step without tripping
   `scan_for_inline_bootstraps` at all (a lone python resolution line carries
   no clone/watchdog/dispatch signature) — this closes that gap directly,
   independent of I7378.

3. A repo-local heredoc-aware scan for `git clone ... /home/ec2-user/`
   reintroduced inside ANY heredoc under `infrastructure/**/*.sh`, regardless
   of whether the file also carries a legitimate
   `-m krepis.spot_bootstrap` delegate line elsewhere. This is the direct
   repo-local mitigation for the I7378 gap above: it does not exempt a file
   for carrying the delegate marker.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from krepis.spot_bootstrap import scan_for_inline_bootstraps

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _infra_shell_files(root: Path) -> list[Path]:
    infra = root / "infrastructure"
    if not infra.is_dir():
        return []
    return sorted(p for p in infra.rglob("*.sh") if p.is_file())


# ── Layer 1: the fleet's canonical behavioural detector ─────────────────────


def test_scan_for_inline_bootstraps_finds_nothing_in_this_repo():
    findings = scan_for_inline_bootstraps(_REPO_ROOT)
    assert not findings, (
        "krepis.spot_bootstrap.scan_for_inline_bootstraps found inline spot "
        "bootstrap(s) — both infrastructure/spot_backtest.sh and "
        "infrastructure/spot_train.sh must render via "
        "`python -m krepis.spot_bootstrap render` (alpha-engine-config-I7372), "
        f"never hand-carry their own heredoc:\n"
        + "\n".join(f"  {f}" for f in findings)
    )


def test_scan_for_inline_bootstraps_fires_on_a_reintroduced_heredoc(tmp_path):
    """Proves the detector is live, not just import-clean. A synthetic fork
    carrying the classic signature set (watchdog timer + interpreter install
    + repo clone + SSM dispatch) must trip it."""
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    fork = infra / "reintroduced_spot_bootstrap.sh"
    fork.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -eo pipefail
            systemd-run --on-active=3600 --unit=fake-watchdog /sbin/shutdown -h now
            dnf install -y -q python3.12 python3.12-pip git gcc
            git clone --depth 1 --branch main https://example.invalid/repo.git /home/ec2-user/repo
            printf '%s' "$SCRIPT" | python -m krepis.ssm_dispatcher run --instance-id i-123
            """
        )
    )
    findings = scan_for_inline_bootstraps(tmp_path)
    assert findings, "scan_for_inline_bootstraps did not fire on a planted fork — detector is dead"
    assert any(f.path == fork for f in findings)


# ── Layer 2: repo-local guard against the silent interpreter fallback ───────

# Matches `command -v python3.12 ... && <VAR>=... python3.12 ... || <VAR>=... python3`
# in either the multi-line bootstrap form or the single-quoted ENV_SOURCE
# one-liner form (both existed in this repo before alpha-engine-config-I7372).
# Deliberately does NOT match the strict replacement (`command -v python3.12
# >/dev/null || { ...exit 1...}; PY=python3.12`) — that has no `&&`/fallback
# `||` assignment branch at all.
_SILENT_INTERPRETER_FALLBACK_RE = re.compile(
    r"command\s+-v\s+python3\.12\b[^\n]*&&[^\n]*=[^\n]*python3\.12[^\n]*\|\|[^\n]*=[^\n]*python3\b(?!\.\d)"
)


def _silent_fallback_violations(root: Path) -> list[tuple[Path, int, str]]:
    violations: list[tuple[Path, int, str]] = []
    for path in _infra_shell_files(root):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _SILENT_INTERPRETER_FALLBACK_RE.search(line):
                violations.append((path.relative_to(root), lineno, line.strip()))
    return violations


def test_no_silent_interpreter_fallback_in_box_scripts():
    violations = _silent_fallback_violations(_REPO_ROOT)
    assert not violations, (
        "Found a silent `command -v python3.12 ... || <VAR>=python3` "
        "interpreter fallback in a box script (alpha-engine-config-I7372 "
        "defect 1 — requirements.txt is resolved against 3.12; falling back "
        "to the AMI's python3 resolves different wheels). Replace with the "
        "literal `python3.12` (matching krepis.spot_bootstrap.PYTHON) or a "
        "strict resolution that exits non-zero when 3.12 is absent:\n"
        + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in violations)
    )


def test_silent_interpreter_fallback_guard_fires_on_reintroduction(tmp_path):
    """Proves the regex is live: plant the exact pre-cutover pattern (both
    the multi-line and single-quoted ENV_SOURCE shapes) and confirm it's
    caught, then confirm the strict replacement is NOT flagged."""
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    bad = infra / "reintroduced_fallback.sh"
    bad.write_text(
        "command -v python3.12 >/dev/null && PYTHON_BIN=python3.12 || PYTHON_BIN=python3\n"
        "ENV_SOURCE='export FOO=bar; command -v python3.12 >/dev/null && PY=python3.12 || PY=python3;'\n"
    )
    violations = _silent_fallback_violations(tmp_path)
    assert len(violations) == 2, f"expected both planted fallbacks to trip the guard, got {violations}"

    good = infra / "strict_resolution.sh"
    good.write_text(
        'command -v python3.12 >/dev/null || { echo "FATAL: python3.12 not found" >&2; exit 1; }\n'
        "PY=python3.12\n"
    )
    good_violations = [v for v in _silent_fallback_violations(tmp_path) if v[0].name == "strict_resolution.sh"]
    assert not good_violations, f"strict replacement false-flagged: {good_violations}"


# ── Layer 3: repo-local guard for the I7378 delegate-clears-whole-file gap ──

_HEREDOC_START_RE = re.compile(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?\s*$")
_GIT_CLONE_RE = re.compile(r"\bgit\s+clone\b")
_HOME_EC2_USER_RE = re.compile(r"/home/ec2-user/")


def _heredoc_clone_violations(root: Path) -> list[tuple[Path, int, str]]:
    """Every heredoc block under infrastructure/**/*.sh that clones a repo
    straight into /home/ec2-user/ — regardless of whether the same file also
    carries a `-m krepis.spot_bootstrap` delegate line elsewhere (that
    delegate marker is exactly what makes scan_for_inline_bootstraps blind to
    this case per alpha-engine-config-I7378, so this check does not exempt
    on it)."""
    violations: list[tuple[Path, int, str]] = []
    for path in _infra_shell_files(root):
        lines = path.read_text().splitlines()
        i = 0
        n = len(lines)
        while i < n:
            m = _HEREDOC_START_RE.search(lines[i])
            if m:
                delim = m.group(1)
                j = i + 1
                block: list[str] = []
                while j < n and lines[j].strip() != delim:
                    block.append(lines[j])
                    j += 1
                block_text = "\n".join(block)
                if _GIT_CLONE_RE.search(block_text) and _HOME_EC2_USER_RE.search(block_text):
                    violations.append((path.relative_to(root), i + 1, delim))
                i = j
            i += 1
    return violations


def test_no_heredoc_shaped_clone_into_home_ec2_user_in_box_scripts():
    violations = _heredoc_clone_violations(_REPO_ROOT)
    assert not violations, (
        "Found a heredoc under infrastructure/**/*.sh that `git clone`s into "
        "/home/ec2-user/ — this is the inline-bootstrap clone shape "
        "alpha-engine-config-I7372 removed. krepis.spot_bootstrap's "
        "scan_for_inline_bootstraps clears a whole file on a single "
        "`-m krepis.spot_bootstrap` delegate match (alpha-engine-config-I7378), "
        "so a heredoc reintroduced beside a legitimate render call would be "
        "invisible to it; this repo-local check does not carry that "
        f"exemption:\n" + "\n".join(f"  {p}:{ln}  <<{d}" for p, ln, d in violations)
    )


def test_heredoc_clone_guard_fires_even_beside_a_delegate_marker(tmp_path):
    """Proves layer 3 closes the I7378 gap: a file carrying a legitimate
    `-m krepis.spot_bootstrap` delegate line AND a reintroduced heredoc clone
    must still be caught, even though scan_for_inline_bootstraps alone would
    clear the whole file on the delegate match."""
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    fork = infra / "delegate_plus_reintroduced_heredoc.sh"
    fork.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -eo pipefail
            _script="$("$LIB_PYTHON" -m krepis.spot_bootstrap render --repo-url https://example.invalid/a.git --checkout /home/ec2-user/a)"
            run_ssm "bootstrap" "$_script" 600

            run_ssm "legacy-side-clone" "$(cat <<BOOTSTRAP
            git clone --depth 1 --branch main https://example.invalid/b.git /home/ec2-user/b
            BOOTSTRAP
            )" 600
            """
        )
    )
    # Confirm the I7378 blind spot is real: the fleet detector alone clears
    # this file (delegate match short-circuits before signatures are read).
    assert not scan_for_inline_bootstraps(tmp_path), (
        "scan_for_inline_bootstraps no longer has the I7378 blind spot — "
        "if this assertion fails, alpha-engine-config-I7378 has shipped and "
        "layer 3 here may be redundant (safe to keep, but worth noting in "
        "the next touch of this file)."
    )
    violations = _heredoc_clone_violations(tmp_path)
    assert violations, "heredoc-clone guard did not fire on a delegate-plus-reintroduced-heredoc fork"
    assert any(p.name == "delegate_plus_reintroduced_heredoc.sh" for p, _, _ in violations)
