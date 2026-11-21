"""Reconcile application memory against systemd and the cgroup v2 tree."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

if __package__:
    from .workload_pauses import WorkloadPause, verify_pause_guard
else:
    from workload_pauses import WorkloadPause, verify_pause_guard

CGROUP_ROOT = Path("/sys/fs/cgroup")
MIB = 1024**2
_UNIT = re.compile(r"[A-Za-z0-9_.@:\\\\-]+\.service", re.ASCII)
_PROPERTIES = (
    "Id", "Names", "LoadState", "ActiveState", "Type", "ControlGroup",
    "UnitFileState", "MemoryMax", "MainPID", "SubState", "FragmentPath", "TriggeredBy",
)
_INACTIVE = {"inactive", "failed"}
_LOCAL_UNITS = ("/etc/systemd/system/", "/run/systemd/system/", "/usr/local/lib/systemd/system/")


class _BudgetLimits(TypedDict, total=False):
    memory_swap_max: str
    uncensor_until: str | datetime
    cap_proposals: str


class BudgetedUnit(_BudgetLimits):
    unit: str
    memory_high: str
    memory_max: str


class UnitName(TypedDict):
    unit: str


class ClassifiedUnit(UnitName):
    reason: str


class _ObservationOptions(TypedDict, total=False):
    timer_jobs: list[BudgetedUnit] | None
    headroom_warn_fraction: float | str
    observe_only: list[ClassifiedUnit]
    infrastructure_services: list[ClassifiedUnit]
    timers: list[UnitName]


class ObservationSpec(_ObservationOptions):
    services: list[BudgetedUnit]


class MemoryBudget(ObservationSpec):
    ram_mb: int | str
    reserve_fraction: float | str
    max_overcommit_ratio: float | str
    max_steady_state_fraction: float | str


@dataclass(frozen=True)
class Service:
    unit: str
    names: frozenset[str]
    load: str
    active: str
    kind: str
    group: str
    enablement: str
    memory_max: str
    main_pid: int
    substate: str = ""
    fragment: str = ""
    triggered_by: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Observation:
    classification: str
    state: str
    control_group: str | None
    working_set_bytes: int | None
    memory_max: str | None
    reason: str | None = None
    load_state: str | None = None


@dataclass(frozen=True)
class Snapshot:
    timestamp: str
    rows: dict[str, Observation]
    paths: dict[str, Path]
    findings: list[str]
    admission_findings: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)

    def observation(self, name: str) -> Observation | None:
        return self.rows.get(self.aliases.get(name, name))

    @property
    def known_application_bytes(self) -> int:
        return sum(
            row.working_set_bytes for row in self.rows.values()
            if row.classification == "application" and row.working_set_bytes is not None
        )

    @property
    def application_bytes(self) -> int | None:
        return None if self.findings else self.known_application_bytes


def _command(*args: str) -> str:
    result = subprocess.run(
        args, text=True, capture_output=True, timeout=15,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr.strip()}")
    return result.stdout


def _valid_unit(unit: str) -> bool:
    return bool(_UNIT.fullmatch(unit)) and not unit.startswith("-")


def live_services(expected: set[str]) -> dict[str, Service]:
    """Resolve live and declared names, including aliases and inactive entries."""
    listing = _command(
        "systemctl", "list-units", "--all", "--type=service",
        "--plain", "--no-legend", "--no-pager",
    )
    listed = {line.split()[0] for line in listing.splitlines() if line.strip()}
    if not listed or any(not _valid_unit(unit) for unit in listed | expected):
        raise ValueError("systemd service inventory is empty or contains invalid names")
    names = sorted(listed | expected)
    shown = _command(
        "systemctl", "show", "--all", "--no-pager",
        *(f"--property={key}" for key in _PROPERTIES), "--", *names,
    )
    services: dict[str, Service] = {}
    for block in shown.strip().split("\n\n"):
        values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = values.get("Id", "")
        if not _valid_unit(unit) or not values.get("LoadState") or not values.get("ActiveState"):
            raise ValueError("systemd service inventory contains an incomplete entry")
        aliases = frozenset({unit, *values.get("Names", "").split()})
        raw_pid = values.get("MainPID", "") or "0"
        if not raw_pid.isdecimal() or any(not _valid_unit(alias) for alias in aliases):
            raise ValueError(f"{unit}: invalid service identity returned by systemd")
        service = Service(
            unit, aliases, values["LoadState"], values["ActiveState"],
            values.get("Type", ""), values.get("ControlGroup", ""),
            values.get("UnitFileState", ""), values.get("MemoryMax", ""),
            int(raw_pid), values.get("SubState", ""), values.get("FragmentPath", ""),
            frozenset(values.get("TriggeredBy", "").split()),
        )
        for alias in aliases:
            if alias in services and services[alias] != service:
                raise ValueError(f"{alias}: systemd returned inconsistent service identities")
            services[alias] = service
    missing = sorted(set(names) - services.keys())
    if missing:
        raise ValueError(f"systemd did not return properties for: {', '.join(missing)}")
    return services


def listener_ports() -> set[int]:
    ports: set[int] = set()
    for line in _command("ss", "-H", "-ltn").splitlines():
        fields = line.split()
        if len(fields) < 5 or not fields[3].rpartition(":")[2].isdecimal():
            raise ValueError("could not identify the local port in ss output")
        ports.add(int(fields[3].rpartition(":")[2]))
    return ports


def cgroup_path(root: Path, control_group: str) -> Path:
    parts = control_group.split("/")
    if len(parts) < 2 or parts[0] or any(not part or part in {".", ".."} for part in parts[1:]):
        raise ValueError(f"invalid systemd ControlGroup: {control_group!r}")
    return root.joinpath(*parts[1:])


def _counter(path: Path, key: str | None = None) -> int:
    counters = path.read_text().strip()
    if key is not None:
        matches = [fields[1] for line in counters.splitlines()
                   if len(fields := line.split()) == 2 and fields[0] == key]
        counters = matches[0] if len(matches) == 1 else ""
    if not re.fullmatch(r"[0-9]+", counters):
        raise ValueError(f"{path}: invalid or missing {key or 'numeric counter'}")
    return int(counters)


def read_working_set(path: Path) -> int:
    before = path.stat()
    anon = _counter(path / "memory.stat", "anon")
    swap = _counter(path / "memory.swap.current")
    after = path.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError(f"{path}: cgroup was replaced during measurement")
    return anon + swap


def _direct_processes(path: Path) -> set[int]:
    return {int(pid) for pid in (path / "cgroup.procs").read_text().split()}


def _local_working_set(path: Path, children: list[Path]) -> int:
    """Bracket child reads with parent reads to identify local, unowned charge."""
    for _ in range(3):
        before = read_working_set(path)
        accounted = sum(read_working_set(child) for child in children)
        after = read_working_set(path)
        if before == after and accounted <= after:
            return after - accounted
    raise ValueError("hierarchical counters changed during ownership reconciliation")


def _unowned_cgroups(root: Path, owners: set[Path]) -> tuple[dict[str, Observation], set[Path], list[str]]:
    """Stop at service-owned subtrees: their memory counters are hierarchical."""
    rows: dict[str, Observation] = {}
    findings: list[str] = []

    scanned: set[Path] = set()

    def walk_error(error: OSError) -> None:
        findings.append(f"cgroup inventory unreadable: {error}")

    for path_name, dirs, _ in os.walk(root, onerror=walk_error):
        path = Path(path_name)
        scanned.add(path)
        children = [path / name for name in dirs]
        dirs[:] = [child for child in dirs if path / child not in owners]
        group = "/" if path == root else "/" + path.relative_to(root).as_posix()
        try:
            direct = _direct_processes(path)
            if path == root:
                # Kernel threads and PID 1 can reside at the cgroup root. Any
                # other userspace process there has no managed service owner.
                for pid in direct - {1}:
                    try:
                        proc_stat = Path(f"/proc/{pid}/stat").read_text()
                    except FileNotFoundError:
                        continue  # the process exited after cgroup.procs
                    flags = int(proc_stat.rsplit(")", 1)[1].split()[6])
                    if not flags & 0x00200000:  # Linux PF_KTHREAD
                        raise ValueError("unowned userspace process at the cgroup root")
                continue
            if group == "/init.scope" and direct == {1} and not children:
                continue  # the systemd manager itself is platform infrastructure
            observed = _local_working_set(path, children) if children else read_working_set(path)
            if direct or observed:
                reason = "unowned cgroup with processes or non-reclaimable memory"
                rows[group] = Observation("unowned", "observed", group, observed, None, reason)
                findings.append(f"{group}: {reason}")
        except (OSError, ValueError, IndexError) as error:
            findings.append(f"{group}: cgroup inventory or memory unreadable: {error}")
            rows[group] = Observation("unowned", "unmeasured", group, None, None, str(error))
    return rows, scanned, findings


def _declared_classes(spec: ObservationSpec) -> dict[str, tuple[str, str | None]]:
    classes: dict[str, tuple[str, str | None]] = {}
    groups = (
        ("application", [(row["unit"], None) for row in spec["services"]]),
        ("batch", [(row["unit"], None) for row in spec.get("timer_jobs") or []]),
        ("observe-only", [(row["unit"], row.get("reason")) for row in spec.get("observe_only") or []]),
        ("infrastructure", [(row["unit"], row.get("reason")) for row in spec.get("infrastructure_services") or []]),
    )
    for group, entries in groups:
        for unit, reason in entries:
            if not _valid_unit(unit) or unit in classes:
                raise ValueError(f"{group}: invalid or duplicate service {unit!r}")
            if group in {"observe-only", "infrastructure"} and not reason:
                raise ValueError(f"{group}: {unit} needs an observation rationale")
            classes[unit] = group, reason
    if not any(category == "application" for category, _ in classes.values()):
        raise ValueError("memory budget declares no application services")
    return classes


def _paused(
    service: Service, pause: WorkloadPause, known_paths: set[Path], ports: set[int],
) -> bool:
    if (service.active != "inactive" or service.group or service.main_pid
            or any(path.name in service.names for path in known_paths)
            or pause.port in ports):
        return False
    if service.load == "not-found" and not service.enablement:
        verify_pause_guard(service.unit)
        return True
    return (
        service.load in {"loaded", "masked"}
        and service.enablement in {"disabled", "masked", "static", "indirect"}
    )


def reconcile(
    spec: ObservationSpec, services: dict[str, Service], pauses: dict[str, WorkloadPause],
    root: Path = CGROUP_ROOT, ports: set[int] | None = None,
) -> Snapshot:
    """Report coverage and working set. Every incomplete snapshot blocks admission."""
    classes = _declared_classes(spec)
    findings: list[str] = []
    rows: dict[str, Observation] = {}
    admission_findings: list[str] = []
    paths: dict[str, Path] = {}
    owners: dict[Path, str] = {}
    unique = {service.unit: service for service in services.values()}
    aliases = {alias: service.unit for alias, service in services.items()}
    declared_timers = {entry["unit"] for entry in spec.get("timers") or []}
    for service in unique.values():
        if not service.group:
            continue
        try:
            path = cgroup_path(root, service.group)
        except ValueError as error:
            findings.append(f"{service.unit}: {error}")
            continue
        if path in owners:
            owner = owners[path]
            detail = f"{service.group}: conflicting owners {owner} and {service.unit}"
            findings.append(detail)
            rows[owner] = Observation("unclassified", "unmeasured", service.group, None, None, detail)
            rows[service.unit] = rows[owner]
        owners[path] = service.unit
        paths.update({alias: path for alias in service.names})
    for path, unit in owners.items():
        parent = next((owners[p] for p in path.parents if p in owners), None)
        if parent is not None:
            findings.append(f"{unit}: ControlGroup is already covered by parent service {parent}")
            rows[unit] = Observation("unclassified", "unmeasured", str(path), None, None)

    unowned, scanned, errors = _unowned_cgroups(root, set(owners))
    findings.extend(errors)
    known_paths = set(owners) | scanned
    rows.update(unowned)
    mandatory = {unit for unit, (category, _) in classes.items() if category in {"application", "batch"}}
    mandatory.update(unit for unit in pauses if unit.endswith(".service"))
    for unit in sorted(mandatory | unique.keys()):
        service = services.get(unit)
        if service is None:
            findings.append(f"{unit}: no systemd inventory entry")
            rows[unit] = Observation("unclassified", "unmeasured", None, None, None)
            continue
        if service.unit != unit or unit in rows:
            continue
        declarations = service.names.intersection(classes)
        pause_names = service.names.intersection(pauses)
        classes_of_aliases = {classes[name][0] for name in declarations}
        equivalent_platform_aliases = classes_of_aliases == {"infrastructure"}
        if (len(declarations) > 1 and not equivalent_platform_aliases) or len(pause_names) > 1:
            findings.append(f"{unit}: aliases have multiple memory or pause declarations")
        declared = sorted(declarations)
        paused = sorted(pause_names)
        category, reason = classes[declared[0]] if declared else ("unclassified", None)
        live_max = service.memory_max or None
        group = service.group or None
        path = paths.get(unit)
        working = None
        state = "unmeasured"
        if paused:
            pause = pauses[paused[0]]
            try:
                approved = _paused(service, pause, known_paths, ports or set())
            except (OSError, RuntimeError, ValueError) as error:
                findings.append(f"{unit}: pause guard unreadable: {error}")
                approved = False
            if approved:
                if service.load == "not-found":
                    live_max = None
                if not declared:
                    category = "paused-oneshot" if service.kind == "oneshot" else "paused-application"
                pause_note = f"{pause.provenance}: {pause.reason}"
                if category == "paused-application" and not (live_max and re.fullmatch(r"[1-9][0-9]*", live_max)):
                    pause_note += "; resume requires a declared finite MemoryMax"
                    live_max = None
                rows[unit] = Observation(category, "declared-off", None, 0, live_max, pause_note, service.load)
                continue
            findings.append(f"{unit}: declared pause does not match systemd, cgroup or listener state")
        if path is not None:
            try:
                working = read_working_set(path)
            except (OSError, ValueError) as error:
                findings.append(f"{unit}: working set unreadable: {error}")
            else:
                state = "observed"
        elif service.active in _INACTIVE or service.substate == "exited":
            state = "between-runs" if service.kind == "oneshot" else "inactive"
            if category == "application":
                findings.append(f"{unit}: application has no cgroup and is not verified paused")
        else:
            findings.append(f"{unit}: {service.active} service has no readable ControlGroup")
        consuming = service.active not in _INACTIVE and service.substate != "exited"
        live = path is not None or consuming
        installed_application = (
            service.fragment.startswith(_LOCAL_UNITS) or bool(service.triggered_by & declared_timers)
        )
        if category == "unclassified" and (live or installed_application):
            if service.kind == "oneshot":
                category = "unbudgeted-oneshot"
                reason = "one-shot has no peak/headroom declaration; a point sample cannot establish its peak"
            else:
                reason = "service has no declared memory classification"
            admission_findings.append(f"{unit}: {reason}")
            if live:
                findings.append(f"{unit}: {reason}")
        rows[unit] = Observation(category, state, group, working, live_max, reason, service.load)

    return Snapshot(datetime.now(timezone.utc).isoformat(), rows, paths, findings, admission_findings, aliases)


def capture(spec: ObservationSpec, pauses: dict[str, WorkloadPause], root: Path = CGROUP_ROOT) -> Snapshot:
    expected = {unit for unit, (category, _) in _declared_classes(spec).items()
                if category in {"application", "batch"}}
    expected.update(unit for unit in pauses if unit.endswith(".service"))
    ports: set[int] = listener_ports() if any(pause.port is not None for pause in pauses.values()) else set()
    return reconcile(spec, live_services(expected), pauses, root, ports)
