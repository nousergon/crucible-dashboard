"""Regression: no box-executed shell script invokes ``python -m alpha_engine_lib.*``
(or functionally imports it at all).

``alpha_engine_lib`` is now an ALIAS shim over ``nousergon_lib`` (lib renamed at
v0.60.0), slated for removal in a future major bump (config#1172). The shim's
``_AliasLoader`` does not implement ``get_code``, so running it as a module via
``python -m alpha_engine_lib.<x>`` (runpy) raises ``AttributeError:
'_AliasLoader' object has no attribute 'get_code'`` and the process dies.
Ordinary ``import alpha_engine_lib`` still works today — only the ``-m``
(runpy) entrypoint is broken — but it's still a live dependency on a shim
planned for removal, so it's guarded here too.

This bit morning-signal#77: the freshness watchdog wrapper shelled out to
``python -m alpha_engine_lib.alerts publish ...`` and crashed before paging, so a
real "episode missing" event went silently unalerted. The fix is to invoke the
canonical module name (``-m nousergon_lib.alerts``). This guard prevents any box
script in this repo from re-introducing the broken alias entrypoint.

A fresh fleet-wide grep during the config#1172 groom pass (2026-07-14) also
found a bare ``from alpha_engine_lib import trading_calendar`` inside this
repo's ``spot_backtest.sh`` (a ``python -c`` invocation, not ``-m`` — so it
wasn't broken, just riding the shim). That class of reference — functional
import, no ``-m`` — is what ``test_no_functional_alpha_engine_lib_import_in_box_scripts``
below guards against.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# An actual ``-m alpha_engine_lib.<module>`` runpy invocation (NOT a bare import,
# NOT a prose mention). Tolerates ``python``/``python3``/``$VAR`` before ``-m``.
_RUNPY_ALIAS_RE = re.compile(r"-m\s+alpha_engine_lib\.")

# ``-m nousergon_lib.<module>`` is ALSO forbidden in box scripts (config#1649):
# for every module extracted to krepis (alerts, ec2_spot, ssm_*, ...) the
# nousergon_lib name is a guard-less re-export shim — under ``python -m`` on
# lib 0.81.0 it exits 0 WITHOUT executing (the config#1646 silent-no-op class;
# 0.81.1's __main__ delegate is belt-and-suspenders, not the canonical path).
# Box scripts must invoke the real module: ``-m krepis.<module>``.
# Exemption: modules that are REAL in nousergon-lib (not shims) may be listed
# here — none in this repo's box scripts today.
_RUNPY_NL_SHIM_RE = re.compile(r"-m\s+nousergon_lib\.")
_REAL_NL_MODULE_EXEMPTIONS: tuple[str, ...] = ()

# Any functional import of the deprecated alias — bare ``import alpha_engine_lib``
# or ``from alpha_engine_lib import ...`` — NOT a ``-m`` runpy invocation (that
# class is covered by _RUNPY_ALIAS_RE above) and NOT a prose/comment mention.
_IMPORT_ALIAS_RE = re.compile(r"\bimport\s+alpha_engine_lib\b")


def _iter_box_scripts():
    """Shell scripts under infrastructure/ — the scripts SSM/systemd execute
    on the box. These are the only place a runpy alias invocation does harm."""
    infra = _REPO_ROOT / "infrastructure"
    if not infra.is_dir():
        return
    yield from infra.rglob("*.sh")


def _collect_violations(pattern, exemptions=()):
    violations: list[tuple[Path, int, str]] = []
    for path in _iter_box_scripts():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):  # skip comments
                continue
            if any(ex in line for ex in exemptions):
                continue
            if pattern.search(line):
                violations.append(
                    (path.relative_to(_REPO_ROOT), lineno, line.strip())
                )
    return violations


def test_no_runpy_alias_invocation_in_box_scripts():
    violations = _collect_violations(_RUNPY_ALIAS_RE)
    assert not violations, (
        "Found `python -m alpha_engine_lib.*` runpy invocation(s) in box "
        "scripts — the alias shim's _AliasLoader has no get_code, so `-m` "
        "crashes. Use the canonical `-m nousergon_lib.<module>` instead:\n"
        + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in violations)
    )


def test_no_runpy_nousergon_lib_shim_invocation_in_box_scripts():
    """config#1649: box scripts must call ``-m krepis.<module>``, never the
    guard-less ``-m nousergon_lib.<module>`` re-export shim (silent exit-0
    no-op on lib 0.81.0 — the config#1646 class that ran a whole weekly SF
    with zero work)."""
    violations = _collect_violations(
        _RUNPY_NL_SHIM_RE, exemptions=_REAL_NL_MODULE_EXEMPTIONS
    )
    assert not violations, (
        "Found `python -m nousergon_lib.*` runpy invocation(s) in box scripts "
        "— for krepis-extracted modules this is a guard-less re-export shim "
        f"(silent no-op on lib 0.81.0, config#1646/#1649): {violations}. "
        "Invoke `-m krepis.<module>` instead, or add a REAL nousergon_lib "
        "module to _REAL_NL_MODULE_EXEMPTIONS with justification."
    )


def test_no_functional_alpha_engine_lib_import_in_box_scripts():
    """config#1172: no box script may functionally import the deprecated
    alpha_engine_lib alias shim (bare ``import`` or ``from ... import``) —
    it's slated for removal from nousergon-lib in a future major bump, and
    every reference must be off the alias before that can happen."""
    violations = _collect_violations(_IMPORT_ALIAS_RE)
    assert not violations, (
        "Found functional `import alpha_engine_lib` / `from alpha_engine_lib "
        "import ...` in box script(s) — alpha_engine_lib is a deprecated "
        "alias shim over nousergon_lib (config#1172), slated for removal. "
        "Use `nousergon_lib` instead:\n"
        + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in violations)
    )
