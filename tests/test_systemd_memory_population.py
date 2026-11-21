from dataclasses import replace
from pathlib import Path

import pytest

from infrastructure import check_memory_budget as budget
from infrastructure import systemd_memory as memory
from infrastructure.workload_pauses import WorkloadPause


MIB = 1024**2


def service(unit, group="", *, state="active", kind="simple", maximum="104857600"):
    return memory.Service(unit, frozenset({unit}), "loaded", state, kind, group, "disabled", maximum, 0)


def cgroup(root, group, *, anon=32, swap=8, processes="142", populated=1):
    directory = root / group.lstrip("/")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cgroup.procs").write_text(processes)
    (directory / "cgroup.events").write_text(f"populated {populated}\n")
    (directory / "memory.stat").write_text(f"anon {anon * MIB}\nfile 0\n")
    if swap is not None:
        (directory / "memory.swap.current").write_text(str(swap * MIB))
    return directory


@pytest.fixture
def root(tmp_path: Path):
    cgroup(tmp_path, "/", processes="")
    return tmp_path


SPEC = {"services": [{"unit": "app.service", "memory_max": "100M"}]}


def test_systemd_inventory_preserves_escaped_instance_aliases(monkeypatch):
    canonical = r"systemd-fsck@dev-disk-by\x2duuid-AAAA\x2d0000.service"
    alias = r"systemd-fsck@dev-disk-by\x2duuid-BBBB\x2d1111.service"
    properties = (
        f"Id={canonical}\n"
        r'Names="systemd-fsck@dev-disk-by\\x2duuid-AAAA\\x2d0000.service" '
        r'"systemd-fsck@dev-disk-by\\x2duuid-BBBB\\x2d1111.service"'
        "\nLoadState=loaded\nActiveState=active\nType=oneshot\nMainPID=0\n"
    )

    def command(*args):
        if args[1] == "list-units":
            return f"{canonical} loaded active exited File System Check\n"
        assert canonical in args
        assert alias in args
        return properties

    monkeypatch.setattr(memory, "_command", command)
    services = memory.live_services({alias})
    assert services[canonical] == services[alias]
    assert services[canonical].names == {canonical, alias}


def test_custom_slice_and_alias_deduplication(root):
    group = "/custom.slice/app.service"
    cgroup(root, "/custom.slice", processes="")
    cgroup(root, group)
    resolved = service("app.service", group)
    snapshot = memory.reconcile(SPEC, {"app.service": resolved, "alias.service": resolved}, {}, root)
    assert snapshot.application_bytes == 40 * MIB
    assert snapshot.rows["app.service"].control_group == group
    assert list(snapshot.rows) == ["app.service"]
    assert not snapshot.findings


def test_undeclared_running_service_is_visible_and_blocks_admission(root):
    cgroup(root, "/app.service")
    cgroup(root, "/stray.service", anon=3, swap=4)
    snapshot = memory.reconcile(
        SPEC, {"app.service": service("app.service", "/app.service"),
               "stray.service": service("stray.service", "/stray.service", kind="oneshot")}, {}, root,
    )
    assert snapshot.application_bytes is None
    assert snapshot.rows["stray.service"].working_set_bytes == 7 * MIB
    assert any("stray.service" in finding for finding in snapshot.findings)


def test_missing_swap_remains_unknown(root):
    cgroup(root, "/app.service", swap=None)
    snapshot = memory.reconcile(SPEC, {"app.service": service("app.service", "/app.service")}, {}, root)
    assert snapshot.application_bytes is None
    assert snapshot.rows["app.service"].working_set_bytes is None
    assert any("memory.swap.current" in finding for finding in snapshot.findings)


def test_pause_requires_matching_inactive_systemd_and_absent_cgroup(root):
    stopped = service("app.service", state="inactive")
    snapshot = memory.reconcile(SPEC, {"app.service": stopped}, {}, root)
    assert snapshot.application_bytes is None
    pauses = {"app.service": WorkloadPause(None, "test-approval", "paused")}
    paused = memory.reconcile(SPEC, {"app.service": stopped}, pauses, root)
    assert paused.application_bytes == 0
    assert paused.rows["app.service"].classification == "application"
    assert paused.rows["app.service"].state == "declared-off"
    assert paused.rows["app.service"].memory_max == "104857600"


def test_unowned_nested_scope_blocks_admission_without_double_counting(root):
    cgroup(root, "/app.service")
    cgroup(root, "/app.service/worker", anon=20)
    cgroup(root, "/user.slice", anon=2, processes="")
    cgroup(root, "/user.slice/foreign.scope", anon=2)
    snapshot = memory.reconcile(SPEC, {"app.service": service("app.service", "/app.service")}, {}, root)
    assert snapshot.known_application_bytes == 40 * MIB
    assert any("/user.slice/foreign.scope" in finding for finding in snapshot.findings)
    assert not any("worker" in finding for finding in snapshot.findings)



def test_unowned_slice_local_charge_blocks_admission_without_counting_child_twice(root):
    cgroup(root, "/slice", anon=400 + 32, processes="")
    cgroup(root, "/slice/app.service")
    snapshot = memory.reconcile(
        SPEC, {"app.service": service("app.service", "/slice/app.service")}, {}, root
    )
    assert snapshot.known_application_bytes == 40 * MIB
    assert snapshot.rows["/slice"].working_set_bytes == 400 * MIB
    assert snapshot.application_bytes is None


def test_alias_only_budget_name_stays_measurable_in_the_record(root, tmp_path):
    cgroup(root, "/real.service")
    canonical = replace(service("real.service", "/real.service"),
                        names=frozenset({"real.service", "app.service"}))
    snapshot = memory.reconcile(SPEC, {"real.service": canonical, "app.service": canonical}, {}, root)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1024000 kB\n")
    sample = budget.memory_sample({**SPEC, "max_steady_state_fraction": 0.5}, snapshot, meminfo)
    assert sample["units"] == {"app.service": 40}
    assert set(sample["unit_observations"]) == {"real.service"}
    assert snapshot.known_application_bytes == 40 * MIB


def test_back_to_back_operational_oneshots_remain_admission_unknown_between_runs(root):
    cgroup(root, "/app.service")
    task = replace(service("job.service", state="inactive", kind="oneshot"),
                   fragment="/etc/systemd/system/job.service")
    snapshot = memory.reconcile(SPEC, {"app.service": service("app.service", "/app.service"),
                                      "job.service": task}, {}, root)
    assert snapshot.application_bytes == 40 * MIB
    assert snapshot.rows["job.service"].state == "between-runs"
    assert snapshot.rows["job.service"].working_set_bytes is None
    assert any("job.service" in finding for finding in snapshot.admission_findings)


def test_guarded_off_base_absence_never_reports_infinity_as_a_loaded_cap(root, monkeypatch):
    canonical = replace(service("app.service", state="inactive", maximum="infinity"),
                        load="not-found", enablement="")
    monkeypatch.setattr(memory, "verify_pause_guard", lambda unit: None)
    snapshot = memory.reconcile(
        SPEC, {"app.service": canonical}, {"app.service": WorkloadPause(None, "test", "held")}, root
    )
    row = snapshot.observation("app.service")
    assert row is not None
    assert row.state == "declared-off" and row.load_state == "not-found"
    assert row.memory_max is None


def test_admission_refuses_an_over_limit_snapshot_even_when_fully_measured(root):
    cgroup(root, "/app.service", anon=45, swap=6)
    snapshot = memory.reconcile(SPEC, {"app.service": service("app.service", "/app.service")}, {}, root)
    spec = {**SPEC, "ram_mb": 100, "reserve_fraction": 0.15,
            "max_steady_state_fraction": 0.5, "max_overcommit_ratio": 2.0}
    assert snapshot.application_bytes == 51 * MIB
    assert budget.admission_findings(spec, snapshot, 100) == [
        "steady-state working set exceeds 50 MiB (T1-8)"
    ]


def test_active_maintenance_requires_matching_cap_and_loaded_active_unit(root, monkeypatch):
    group = "/app.service"
    cgroup(root, group)
    properties = {"ActiveState": "active", "LoadState": "loaded", "ControlGroup": group,
                  "Names": "app.service alias.service", "MemoryMax": "104857600", "MemoryHigh": "83886080"}
    monkeypatch.setattr(budget, "_CGROUP_ROOT", root)
    monkeypatch.setattr(budget, "systemd_show", lambda _unit, prop: properties[prop])
    spec = {"services": [{"unit": "app.service", "memory_max": "100M", "memory_high": "80M"}]}
    assert not budget.maintenance_findings(spec, ["app.service"], {})
    pause = {"alias.service": WorkloadPause(None, "test-approval", "paused")}
    assert "paused" in budget.maintenance_findings(spec, ["app.service"], pause)[0]
    properties["MemoryMax"] = str(101 * MIB)
    assert "MemoryMax" in budget.maintenance_findings(spec, ["app.service"], {})[0]
    properties["ActiveState"] = "inactive"
    assert "not already active" in budget.maintenance_findings(spec, ["app.service"], {})[0]
    assert "outside" in budget.maintenance_findings(spec, ["unknown.service"], {})[0]
