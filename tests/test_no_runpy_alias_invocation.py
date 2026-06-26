"""Regression: no box-executed shell script invokes ``python -m alpha_engine_lib.*``.

``alpha_engine_lib`` is now an ALIAS shim over ``nousergon_lib`` (lib renamed at
v0.60.0). The shim's ``_AliasLoader`` does not implement ``get_code``, so running
it as a module via ``python -m alpha_engine_lib.<x>`` (runpy) raises
``AttributeError: '_AliasLoader' object has no attribute 'get_code'`` and the
process dies. Ordinary ``import alpha_engine_lib`` still works — only the ``-m``
(runpy) entrypoint is broken.

This bit morning-signal#77: the freshness watchdog wrapper shelled out to
``python -m alpha_engine_lib.alerts publish ...`` and crashed before paging, so a
real "episode missing" event went silently unalerted. The fix is to invoke the
canonical module name (``-m nousergon_lib.alerts``). This guard prevents any box
script in this repo from re-introducing the broken alias entrypoint.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# An actual ``-m alpha_engine_lib.<module>`` runpy invocation (NOT a bare import,
# NOT a prose mention). Tolerates ``python``/``python3``/``$VAR`` before ``-m``.
_RUNPY_ALIAS_RE = re.compile(r"-m\s+alpha_engine_lib\.")


def _iter_box_scripts():
    """Shell scripts under infrastructure/ — the scripts SSM/systemd execute
    on the box. These are the only place a runpy alias invocation does harm."""
    infra = _REPO_ROOT / "infrastructure"
    if not infra.is_dir():
        return
    yield from infra.rglob("*.sh")


def test_no_runpy_alias_invocation_in_box_scripts():
    violations: list[tuple[Path, int, str]] = []
    for path in _iter_box_scripts():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):  # skip comments
                continue
            if _RUNPY_ALIAS_RE.search(line):
                violations.append(
                    (path.relative_to(_REPO_ROOT), lineno, line.strip())
                )
    assert not violations, (
        "Found `python -m alpha_engine_lib.*` runpy invocation(s) in box "
        "scripts — the alias shim's _AliasLoader has no get_code, so `-m` "
        "crashes. Use the canonical `-m nousergon_lib.<module>` instead:\n"
        + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in violations)
    )
