"""Every morning-signal drop-in in the repo must be installed AND
state-compared (alpha-engine-config-I6656).

`install-morning-signal.sh` copies a HARDCODED list of drop-in filenames, and
`deploy-on-merge.sh` state-compares a hardcoded list of src:dst pairs. Add a
`.conf` to `infrastructure/systemd/morning-signal.service.d/` without touching
both lists and it is codified, reviewed, merged — and never reaches the box.
A box rebuild then comes up running a different unit than the one in the repo,
which is the exact state `install-morning-signal.sh`'s own header says these
units used to be in.

Found 2026-08-08: `10-timeout.conf` (the TimeoutStartSec raise an episode died
without) and `20-router.conf` (the router-edge env) were applied by hand to
the live box and were in neither list, nor in the repo.

The same day showed the other half of the failure: an edit made directly to
`/usr/local/bin/morning-signal-recover.sh` was silently reverted by the next
merge, because `deploy-on-merge.sh` state-compares that path against the repo
and re-installs on difference. The deployed artifact is not the source.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_DROPIN_DIR = _REPO / "infrastructure" / "systemd" / "morning-signal.service.d"
_INSTALLER = _REPO / "infrastructure" / "install-morning-signal.sh"
_DEPLOY = _REPO / "infrastructure" / "deploy-on-merge.sh"


def _dropins() -> list[str]:
    return sorted(p.name for p in _DROPIN_DIR.glob("*.conf"))


def _router_dropin_keys() -> list[str]:
    """Environment variable names declared by `20-router.conf`."""
    import re

    text = (_DROPIN_DIR / "20-router.conf").read_text()
    return sorted(set(re.findall(r'^Environment="([A-Za-z_][A-Za-z0-9_]*)=', text, re.M)))


def test_there_are_dropins_to_assert_over():
    """A coverage test whose input set is empty passes by covering nothing."""
    assert _dropins(), f"no drop-ins found under {_DROPIN_DIR}"


@pytest.mark.parametrize("conf", _dropins())
def test_installer_copies_every_dropin(conf: str):
    src = _INSTALLER.read_text()
    assert conf in src, (
        f"{conf} is in the repo but not in install-morning-signal.sh's copy "
        f"loop — it will not exist on a rebuilt box"
    )


@pytest.mark.parametrize("conf", _dropins())
def test_deploy_on_merge_state_compares_every_dropin(conf: str):
    src = _DEPLOY.read_text()
    assert f"morning-signal.service.d/{conf}" in src, (
        f"{conf} is not in deploy-on-merge.sh's state-compare pairs — a change "
        f"to it would merge without ever reaching the box"
    )


def test_the_recovery_wrapper_mirrors_the_router_dropin():
    """`morning-signal-recover.sh` re-runs what `morning-signal.service` runs.
    On 2026-08-08 the unit gained three router env vars via `20-router.conf`
    and the wrapper did not, so the recovery run resolved against
    `exec_context=laptop` while executing on EC2 — a different set of registry
    entries than the run it was recovering."""
    wrapper = (_REPO / "infrastructure" / "morning-signal-recover.sh").read_text()

    # DERIVED from the drop-in, never enumerated here. A hardcoded key list
    # asserts only over the variables somebody remembered to add to it, so it
    # is blind in exactly the direction that matters: the NEXT variable the
    # drop-in gains and the wrapper does not.
    keys = _router_dropin_keys()
    assert keys, "20-router.conf declares no Environment= keys to mirror"
    for key in keys:
        assert f"export {key}=" in wrapper, (
            f"{key} is declared by the unit's drop-in but not exported by "
            f"morning-signal-recover.sh"
        )


def test_the_router_url_is_the_authenticated_edge_not_the_loopback_process():
    """krepis defaults `litellm_proxy` to `http://127.0.0.1:8980` — the router
    PROCESS. The per-consumer credential this unit declares is translated into
    the router's own key by the nginx edge at :8443; the process behind it
    holds only the master key and has no database to resolve a virtual key
    against (`/health/readiness`: `db: Not connected`). Pairing the default URL
    with a consumer credential therefore cannot authenticate, and the router
    answers `400 no_db_connection`.

    Measured on the dashboard box: every scheduled run from 2026-08-09 through
    2026-08-12 aborted its configured primary on that 400 and aired from a
    fallback. Same assertion the Think Tank spot dispatcher already makes about
    its own prelude.
    """
    for label, text in (
        ("20-router.conf", (_DROPIN_DIR / "20-router.conf").read_text()),
        (
            "morning-signal-recover.sh",
            (_REPO / "infrastructure" / "morning-signal-recover.sh").read_text(),
        ),
    ):
        assert "KREPIS_LITELLM_PROXY_URL" in text, (
            f"{label} does not set KREPIS_LITELLM_PROXY_URL, so krepis falls "
            f"back to the loopback router process, which cannot authenticate "
            f"this consumer's credential"
        )
        assert "https://router.nousergon.ai:8443" in text, (
            f"{label} must address the authenticated edge"
        )
        assert "KREPIS_LITELLM_PROXY_URL=http://127.0.0.1" not in text, (
            f"{label} addresses the router process directly, bypassing the "
            f"edge that authenticates and rate-limits it (model-router-policy "
            f"R27c)"
        )


def test_only_one_delivery_path_for_these_units():
    """boot-pull.sh must not carry a second sync for morning-signal.

    A short-lived second path was added on 2026-08-08 (PR633) before it was
    noticed that `install-morning-signal.sh` + `deploy-on-merge.sh` already
    deliver these files. Two sources for one artifact diverge, and nothing on
    the box says which one it is following.
    """
    boot_pull = (_REPO / "infrastructure" / "boot-pull.sh").read_text()
    assert "MORNING_SIGNAL_SRC" not in boot_pull, (
        "boot-pull.sh must not sync morning-signal units — "
        "install-morning-signal.sh owns that, invoked by deploy-on-merge.sh"
    )
