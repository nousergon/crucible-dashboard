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


#: The one endpoint every router consumer addresses, whatever host it runs on
#: (model-router-policy R27a). Same value the Think Tank spot dispatcher pins.
_ROUTER_EDGE_URL = "https://router.nousergon.ai:8443"


def _declared_value(path: Path, key: str = "KREPIS_LITELLM_PROXY_URL") -> str | None:
    """The value *key* is assigned in *path*, in either the systemd
    `Environment="K=V"` form or the shell `export K=V` form. `None` when the
    file does not assign it at all."""
    import re

    m = re.search(
        rf'^(?:Environment="{key}=([^"]*)"|export\s+{key}=(\S+))\s*$',
        path.read_text(),
        re.M,
    )
    if m is None:
        return None
    return (m.group(1) if m.group(1) is not None else m.group(2)).strip("'\"")


#: Where the shared router contract is installed, and the string every reader
#: must name. One file, three readers — see its own header for why.
_ROUTER_ENV_SRC = _REPO / "infrastructure" / "systemd" / "morning-signal-router-env.conf"
_ROUTER_ENV_DST = "/etc/morning-signal/router-env.conf"
_SYSTEMD_DIR = _REPO / "infrastructure" / "systemd"
_RECOVER = _REPO / "infrastructure" / "morning-signal-recover.sh"


def _router_env_keys() -> dict[str, str]:
    """The KEY=value pairs the shared router contract declares."""
    import re

    out: dict[str, str] = {}
    for line in _ROUTER_ENV_SRC.read_text().splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _units_running_morning_signal_python() -> list[Path]:
    """Units whose ExecStart runs a python entrypoint out of the morning-signal
    venv — i.e. every unit that can construct an ``LLMClient``.

    DERIVED from the units on disk, never enumerated. An enumerated list is
    blind in the one direction that matters: the next unit added. That is not
    hypothetical — `morning-signal-bakeoff.service` was missing the router
    contract entirely on 2026-08-12 while the drop-in and the recovery wrapper
    both carried it, and no test could see the gap because none of them looked
    at that unit.
    """
    import re

    hits = []
    for unit in sorted(_SYSTEMD_DIR.glob("morning-signal*.service")):
        for line in unit.read_text().splitlines():
            # ExecStart only (not ExecStartPre, which runs git/pip), and only a
            # python entrypoint (pip install is not a call site).
            m = re.match(r"^ExecStart=(\S*/morning-signal/\.venv/bin/python)\s+(\S+)", line)
            if m and not m.group(2).startswith("-m pip"):
                hits.append(unit)
                break
    return hits


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


def test_every_llm_calling_unit_reads_the_shared_router_contract():
    """DERIVED from the units on disk — see `_units_running_morning_signal_python`.

    On 2026-08-12 `morning-signal.service` (via its drop-in) and
    `morning-signal-recover.sh` both carried the router contract and
    `morning-signal-bakeoff.service` carried none of it, so the bakeoff resolved
    against a default execution context with no consumer credential while
    production resolved against `ec2` with one. A bakeoff whose prod side
    reaches a different router than production is not a comparison.
    """
    units = _units_running_morning_signal_python()
    assert units, "no morning-signal unit runs a venv python entrypoint — the derivation is broken"

    dropin_text = "\n".join(p.read_text() for p in _DROPIN_DIR.glob("*.conf"))
    for unit in units:
        text = unit.read_text()
        # The drop-in directory only extends morning-signal.service, so a unit
        # may satisfy this either directly or through its own drop-ins.
        satisfied = _ROUTER_ENV_DST in text or (
            unit.name == "morning-signal.service" and _ROUTER_ENV_DST in dropin_text
        )
        assert satisfied, (
            f"{unit.name} runs a morning-signal python entrypoint but never names "
            f"EnvironmentFile={_ROUTER_ENV_DST}. It will resolve the router group "
            f"against a default execution context with no consumer credential."
        )


def test_the_recovery_wrapper_sources_the_shared_contract_rather_than_copying_it():
    """`morning-signal-recover.sh` re-runs what `morning-signal.service` runs, so
    it must resolve the same router. It SOURCES the shared file; re-exporting the
    keys inline would recreate the fork this change removed."""
    wrapper = _RECOVER.read_text()
    assert _ROUTER_ENV_DST in wrapper, (
        f"morning-signal-recover.sh does not read {_ROUTER_ENV_DST} — a recovery "
        f"run that resolves a different router than the run it is recovering is "
        f"not a recovery"
    )
    for key in _router_env_keys():
        assert f"export {key}=" not in wrapper, (
            f"{key} is exported inline by morning-signal-recover.sh AND declared "
            f"in the shared contract. Two copies is what produced the 2026-08-12 "
            f"split; the wrapper must source the file, not mirror it."
        )


def test_no_unit_redeclares_a_router_key_inline():
    """The fork guard. A unit that sets one of these itself silently overrides
    the shared contract for that process only, which is indistinguishable from
    the shared contract being wrong."""
    keys = _router_env_keys()
    assert keys, "the shared router contract declares no keys"
    for unit in sorted(_SYSTEMD_DIR.glob("morning-signal*")):
        text = unit.read_text() if unit.is_file() else ""
        for key in keys:
            assert f'Environment="{key}=' not in text, (
                f"{unit.name} redeclares {key} inline, overriding "
                f"{_ROUTER_ENV_DST} for that unit only"
            )


def test_the_router_url_is_the_authenticated_edge_not_the_loopback_process():
    """krepis defaults `litellm_proxy` to `http://127.0.0.1:8980` — the router
    PROCESS. The per-consumer credential this contract declares is translated
    into the router's own key by the nginx edge at :8443; the process behind it
    holds only the master key and has no database to resolve a virtual key
    against (`/health/readiness`: `db: Not connected`). Pairing the default URL
    with a consumer credential therefore cannot authenticate, and the router
    answers `400 no_db_connection`.

    Measured on the dashboard box: every scheduled run from 2026-08-09 through
    2026-08-12 aborted its configured primary on that 400 and aired from a
    fallback. Same assertion the Think Tank spot dispatcher makes about its own
    prelude.
    """
    declared = _router_env_keys().get("KREPIS_LITELLM_PROXY_URL")
    assert declared is not None, (
        "the shared router contract does not set KREPIS_LITELLM_PROXY_URL, so "
        "krepis falls back to the loopback router process, which cannot "
        "authenticate this consumer's credential"
    )
    # EQUALITY, not `in`. A substring test on a URL passes for any string that
    # merely CONTAINS the edge — including one where it sits after a different
    # host — which is the whole of CodeQL's
    # `py/incomplete-url-substring-sanitization`.
    assert declared == _ROUTER_EDGE_URL, (
        f"the shared router contract sets KREPIS_LITELLM_PROXY_URL={declared!r}; "
        f"it must be exactly {_ROUTER_EDGE_URL!r}. Addressing the router process "
        f"directly bypasses the edge that authenticates and rate-limits it "
        f"(model-router-policy R27c), and the loopback process cannot validate "
        f"this consumer's credential at all."
    )


def test_the_shared_contract_is_installed_and_state_compared():
    """A file every unit names as a mandatory EnvironmentFile must reach the box,
    and must re-reach it when it changes. systemd treats a missing
    EnvironmentFile as fatal to the unit start, so an uninstalled copy is not a
    degraded run — it is no run at all."""
    assert _ROUTER_ENV_SRC.name in _INSTALLER.read_text(), (
        f"{_ROUTER_ENV_SRC.name} is not installed by install-morning-signal.sh — "
        f"a rebuilt box would have units that cannot start"
    )
    assert _ROUTER_ENV_DST in _DEPLOY.read_text(), (
        f"{_ROUTER_ENV_DST} is not in deploy-on-merge.sh's state-compare pairs — "
        f"a change to the router contract would merge without reaching the box"
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
