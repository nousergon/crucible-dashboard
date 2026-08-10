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
    dropin = (_DROPIN_DIR / "20-router.conf").read_text()

    for key in (
        "KREPIS_EXEC_CONTEXT",
        "LLM_MODEL_REGISTRY_PATH",
        "KREPIS_ROUTER_CREDENTIAL_SECRET",
    ):
        assert key in dropin, f"{key} missing from 20-router.conf"
        assert f"export {key}=" in wrapper, (
            f"{key} is declared by the unit's drop-in but not exported by "
            f"morning-signal-recover.sh"
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
