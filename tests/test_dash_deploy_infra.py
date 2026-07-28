"""Wiring tests for the /dash exposure (config#1957 plan §8.5) and the
generic deploy-on-merge.sh infra gates.

The pre-cutover Streamlit /dash skin (dash/app.py, crucible-dash.service)
was retired after a clean 9-D soak with no rollback incidents (config#1973
tail, 2026-07-23) — /dash is served exclusively by dash-web (Next.js) +
dash_api (FastAPI) now. Tests below assert the retirement is complete and
that deploy-on-merge.sh's OTHER infra gates (unrelated to the retired skin)
still hold.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).parent.parent


def watchdog_units_and_ports():
    """Every unit/port the box watchdog covers, across BOTH of its surfaces.

    Since I4492 the authoritative registry is
    `infrastructure/systemd/resource-limits/budget.yaml` — box_health.sh sources
    a manifest generated from it. box_health.sh also keeps a hardcoded fallback
    list used when that manifest is missing.

    Both are returned together so a retirement guard cannot be satisfied by
    cleaning one and forgetting the other. The previous version of this helper
    was a `^SERVICES=\\(` regex against box_health.sh alone; when the arrays
    moved inside an `else` block it silently matched nothing, and an
    `assert match` was the only thing standing between that and a guard that
    passes vacuously.
    """
    import re

    import yaml

    units, ports = set(), set()

    spec = yaml.safe_load(
        (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
         / "budget.yaml").read_text()
    )
    for svc in spec["services"]:
        units.add(svc["unit"])
        if str(svc.get("port", "none")) != "none":
            ports.add(str(svc["port"]))

    box_health = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
    # Tolerates indentation and backslash line-continuations; the arrays live
    # inside an if/else now.
    for name, sink in (("SERVICES", units), ("PORTS", ports)):
        m = re.search(rf"^\s*{name}=\((.*?)\)", box_health, re.M | re.S)
        assert m, f"box_health.sh must declare a fallback {name}=(...) array"
        sink.update(m.group(1).replace("\\\n", " ").split())

    return units, ports


class TestStreamlitSkinRetired:
    def test_dash_app_and_unit_removed(self):
        assert not (REPO_ROOT / "dash").exists(), \
            "dash/ (the retired Streamlit /dash skin) should be fully removed"
        assert not (REPO_ROOT / "infrastructure" / "crucible-dash.service").exists()

    def test_deploy_script_tears_down_stale_unit_instead_of_provisioning_it(self):
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        # No self-provision/restart of the retired unit anymore.
        assert "install crucible-dash unit" not in script
        assert "systemctl restart crucible-dash 2>>" not in script
        assert "8504/dash/_stcore/health" not in script       # retired health gate
        assert 'DASH_URL="http://localhost:8504' not in script
        # A teardown path exists so a box still running the old unit (or one
        # that never had it) both converge to "not installed", no manual step.
        assert "if [ -f /etc/systemd/system/crucible-dash.service ]; then" in script
        assert "systemctl disable crucible-dash" in script
        assert "rm -f /etc/systemd/system/crucible-dash.service" in script

    def test_box_health_no_longer_watches_retired_service_or_port(self):
        # A historical comment noting :8504's reuse history is fine — neither
        # the authoritative registry nor the watchdog's fallback may carry the
        # retired unit/port forward.
        #
        # Checks BOTH surfaces since I4492: budget.yaml is now the box's single
        # service registry (box_health.sh sources a manifest generated from it),
        # and box_health.sh keeps a hardcoded fallback for when that manifest is
        # missing. A guard that watched only one of them would miss the other.
        units, ports = watchdog_units_and_ports()
        assert "crucible-dash.service" not in units
        assert "8504" not in ports

    def test_nginx_routes_dash_to_web(self):
        # 9-D cutover (config#1973): /dash serves the Next.js surface on
        # :3002. The Streamlit skin (:8504) that used to sit behind a
        # one-line rollback is gone — rollback is a plain git revert now.
        conf = (REPO_ROOT / "infrastructure" / "nginx.conf").read_text()
        assert "location /dash" in conf
        assert conf.index("location /dash") < conf.index("location / {"), \
            "the /dash location must precede the catch-all live proxy"
        dash_block = conf[conf.index("location /dash"):conf.index("location / {")]
        assert "http://127.0.0.1:3002" in dash_block


class TestInfraWiring:
    def test_requirements_nginx_installer_gates_are_state_compared(self):
        # config#2338: a deploy that never executes (SSM delivery failure)
        # must not permanently skip the missed commit's requirements/nginx/
        # installer changes. These gates used to diff `${CURRENT_SHA}~1..
        # ${CURRENT_SHA}` (a single-commit window that a missed deploy blows
        # right past); they must now state-compare the repo file against the
        # box's installed/live copy instead, mirroring the §3b-3d unit
        # pattern (cmp repo vs /etc/systemd/system/*.service) which is
        # self-healing by construction regardless of how many deploys were
        # skipped.
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()

        assert "file_state_stale" in script
        assert "any_file_state_stale" in script

        # requirements.txt: stamp-file state-compare, not a HEAD~1 diff.
        req_block = script[script.index('REQUIREMENTS_STAMP='):script.index("# ── 2. Reload nginx")]
        assert 'file_state_stale "$REQUIREMENTS_STAMP" "requirements.txt"' in req_block
        assert "CURRENT_SHA}~1" not in req_block

        # nginx.conf: cmp repo copy directly against the live nginx conf.
        nginx_block = script[script.index('NGINX_CONF_REPO='):script.index("# ── 2b.")]
        assert 'file_state_stale "$NGINX_CONF_LIVE" "$NGINX_CONF_REPO"' in nginx_block
        assert "CURRENT_SHA}~1" not in nginx_block

        # §2b-2e installer gates: any_file_state_stale over explicit
        # src:dst pairs, not a `paths_changed ... ~1` commit-range gate.
        # Anchor on the "# ── 3." prefix, not a full title — main renames
        # section-3 headings independently of this test's concern.
        installer_block = script[script.index("# ── 2b."):script.index("# ── 3.")]
        assert installer_block.count("any_file_state_stale") == 4
        assert "CURRENT_SHA}~1" not in installer_block

    def test_python_parity_self_heal_venv_built_at_final_path_no_relocation(self):
        # config#2835: the 2026-07-17 outage happened because the self-heal
        # built the new venv at a STAGING path, pip-installed into it there
        # (baking staging-path shebangs into every console script), then
        # `mv`'d it into place — the shebangs then pointed at a deleted
        # path. The fix is to never pip-install into a venv and then
        # relocate it: the venv must be created directly at its final path.
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        heal_block = script[script.index("Python-parity self-heal: box venv is"):
                             script.index("# ── 1. Refresh deps")]

        # The venv must be created and pip-installed at $REPO_DIR/.venv
        # directly (the FINAL path) — not at a separate "new venv" staging
        # variable that later gets `mv`'d into place.
        assert '"$NEW_PY_BIN" -m venv "$REPO_DIR/.venv"' in heal_block
        # pip is invoked via the interpreter (`python -m pip`), never the
        # `.venv/bin/pip` console-script wrapper — see
        # test_pip_invoked_via_interpreter_not_console_script for why.
        assert '"$REPO_DIR/.venv/bin/python" -m pip install' in heal_block

        # No mv of a freshly-built/installed venv INTO the final .venv path
        # — that pattern is exactly the shebang-breaking relocation bug.
        # (Moving the OLD venv OUT to a backup path is fine and expected.)
        assert 'mv "$NEW_VENV_PATH" "$REPO_DIR/.venv"' not in heal_block
        assert "NEW_VENV_PATH" not in heal_block

    def test_pip_invoked_via_interpreter_not_console_script(self):
        # config#2938 (2026-07-18 Deploy false-red, run 29654297139): the §1
        # dep-refresh invoked pip through the `.venv/bin/pip` console-script
        # wrapper, whose absolute-path `#!` shebang is baked in at venv-build
        # time. On a box whose venv had a stale/relocated wrapper the file
        # still EXISTED (so the old `-f ".venv/bin/pip"` guard passed) but
        # `env` could not execve it: `env: '.venv/bin/pip': No such file or
        # directory` (rc=127), failing every deploy that changed
        # requirements.txt. The pip MODULE in site-packages is unaffected, so
        # the robust invocation is `.venv/bin/python -m pip`, which uses the
        # working interpreter directly and is immune to wrapper-shebang drift.
        # Guard the whole class: no bare `.venv/bin/pip` EXECUTION anywhere,
        # and the §1 gate keys on the interpreter, not the wrapper file.
        # Repo-wide chokepoint: NO box-side shell script may invoke the bare
        # `.venv/bin/pip` wrapper (only `.venv/bin/python -m pip`). This is the
        # structural guard, not a per-call-site patch — it fails CI if any
        # future script reintroduces the fragile wrapper anywhere.
        def strip_comments(text):
            return "\n".join(
                ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
            )

        offenders = []
        for sh in sorted((REPO_ROOT / "infrastructure").glob("*.sh")):
            code = strip_comments(sh.read_text())
            if ".venv/bin/pip" in code:
                offenders.append(sh.name)
        assert not offenders, (
            "these scripts invoke the fragile .venv/bin/pip console-script "
            f"wrapper instead of .venv/bin/python -m pip: {offenders}"
        )

        # deploy-on-merge.sh specifically must gate §1 on the interpreter and
        # run the requirements install via the interpreter.
        deploy_code = strip_comments(
            (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        )
        assert '[ -x ".venv/bin/python" ]' in deploy_code, (
            "the §1 dep-refresh gate must test the venv interpreter is "
            "executable, not the presence of the .venv/bin/pip wrapper file"
        )
        assert ".venv/bin/python -m pip install -r requirements.txt" in deploy_code

    def test_python_parity_self_heal_has_rollback_on_failed_health_gate(self):
        # config#2835 defect 2: the old flow's post-swap health-gate failure
        # called `fail` directly WITHOUT restoring the preserved old venv,
        # leaving all services crash-looping on the broken venv for ~25
        # minutes. A rollback path must exist and must be invoked on the
        # post-swap health-gate failure branch, not just logged about.
        # (3 venv-backed services since config#1973 retired crucible-dash.)
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        heal_block = script[script.index("Python-parity self-heal: box venv is"):
                             script.index("# ── 1. Refresh deps")]

        assert "_rollback_venv()" in heal_block, "no rollback function defined in the self-heal block"

        # The preserved old venv must actually get restored on rollback.
        rollback_start = heal_block.index("_rollback_venv() {")
        rollback_fn = heal_block[rollback_start:
                                  rollback_start + heal_block[rollback_start:].index("\n        }")]
        assert 'mv "$OLD_VENV_BACKUP" "$REPO_DIR/.venv"' in rollback_fn
        assert "systemctl restart dashboard nous-ergon-live crucible-dash-api" in rollback_fn
        assert rollback_fn.count("wait_for_health") == 3, \
            "rollback must re-verify health on all 3 remaining services before considering itself successful"

        # The post-swap health-gate failure branch must actually call the
        # rollback — not just `fail` on its own, and not just mention
        # rollback in a comment.
        post_swap_gate = heal_block[heal_block.index("# 5. Reuse the script's existing health-gate"):]
        assert "_rollback_venv" in post_swap_gate
        assert 'fail "python-parity self-heal: post-swap health gate failed' in post_swap_gate
        assert "ROLLED BACK to previous venv successfully" in post_swap_gate

    def test_python_parity_self_heal_probes_venv_health_not_just_version(self):
        # config#2955: the 2026-07-18 Deploy false-red (run 29654297139,
        # config#2938) proved a venv can pass the python-VERSION check while
        # its console-script wrappers/site-packages are otherwise unhealthy
        # — §0 must not no-op purely on version match. A functionality probe
        # (`python -m pip --version`) gates the no-op branch, and a failed
        # probe must fall through to the SAME rebuild+rollback path as a
        # version mismatch (not a separate/new path).
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        heal_start = script.index('PYVER_SSOT_FILE="$REPO_DIR/.python-version"')
        heal_block = script[heal_start:script.index("# ── 1. Refresh deps")]

        # The no-op branch requires BOTH version match AND a working pip
        # module in the box venv — not version match alone.
        no_op_gate = heal_block[:heal_block.index('log "OK   Python-parity self-heal')]
        assert '"$REPO_DIR/.venv/bin/python" -m pip --version' in no_op_gate, (
            "the self-heal no-op branch must probe venv functionality "
            "(python -m pip --version), not just the python version"
        )

        # A version-match-but-unhealthy venv must log distinctly and still
        # fall into the rebuild block (same rollback-guarded path pinned by
        # test_python_parity_self_heal_has_rollback_on_failed_health_gate).
        assert "functionality probe failed" in heal_block
        rebuild_trigger = heal_block[heal_block.index("functionality probe failed"):]
        assert '"$NEW_PY_BIN" -m venv "$REPO_DIR/.venv"' in rebuild_trigger, (
            "a failed functionality probe must route through the same "
            "rebuild path as a python-version drift, not a new one"
        )
        assert "_rollback_venv()" in rebuild_trigger

    def test_dash_web_build_gate_unaffected_by_state_compare_migration(self):
        # config#2338 scoped the fix to requirements/nginx/installer gates
        # only; the dash-web build gate is a separate cost tradeoff (npm ci +
        # next build is expensive) and keeps its existing commit-range gate
        # plus its own missing-build fallback.
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert 'paths_changed "${CURRENT_SHA}~1" "$CURRENT_SHA" dash-web/' in script
        assert '[ ! -d "$WEB_DIR/.next" ]' in script

    def test_port_8504_freed_by_retirement_not_reclaimed_silently(self):
        # config#1972 Part A: :8504 was crucible-dash's (retired config#1973) —
        # this guards against a future service silently reusing the port
        # without updating the port map in lockstep, the exact drift class
        # #1972/#354 existed to catch.
        units, ports = watchdog_units_and_ports()

        assert "crucible-dash.service" not in units

        assert "8504" not in ports, (
            "8504 (retired crucible-dash's port) should not be watched unless "
            "a new service has claimed it — if so, update this test with the "
            "new owner, and give it a `port:` row in budget.yaml"
        )


class TestInfraShellTests:
    """Run the repo's bash test scripts under pytest so CI actually executes them.

    `.github/workflows/ci.yml` runs `pytest tests/` and nothing else — it has
    no shell-test step. Both scripts below were written as "invoked directly"
    runners, which means neither had ever run in CI: a test nobody runs is
    indistinguishable from no test, and it presents as green. Shelling out
    from pytest is what makes them gates rather than documentation.
    """

    def _run(self, name):
        import subprocess

        script = REPO_ROOT / "infrastructure" / name
        assert script.is_file(), f"{name} is missing"
        proc = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, (
            f"{name} failed (exit {proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}"
        )

    def test_box_health_timer_deadman(self):
        # Guards classify_timer() — the box's only dead-man monitor for
        # timer-driven jobs (policy T0-4, config-I4487). Its predecessor read
        # a mid-trigger timer as dead and paged hourly on the watchdog's own
        # timer, 144 of 144 runs.
        self._run("test_box_health_timer_deadman.sh")

    def test_deploy_manifest_gate(self):
        # Guards the gate that makes a budget.yaml-only change actually reach
        # the box. Before it, the manifest was generated ONLY by
        # install-resource-limits.sh, which nothing automated invokes.
        self._run("test_deploy_manifest_gate.sh")

    def test_deploy_on_merge_paths_changed(self):
        # Pre-existing script (config#2242), previously unwired from CI.
        self._run("test_deploy_on_merge_paths_changed.sh")


class TestTimerDeadManRegistry:
    """budget.yaml's `timers:` block is the dead-man's threshold registry.

    box_health.sh names any enabled timer with no row here, so the registry is
    load-bearing: a malformed or missing entry degrades coverage silently on
    the box unless it fails here first (config-I5209).
    """

    def _timers(self):
        import yaml

        spec = yaml.safe_load(
            (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
             / "budget.yaml").read_text()
        )
        assert spec.get("timers"), "budget.yaml must declare a `timers:` block"
        return spec["timers"]

    def test_every_timer_row_is_well_formed(self):
        # A threshold that silently became a wrong number is worse than no
        # threshold, because it reports as covered.
        import re

        seen = set()
        for row in self._timers():
            unit = row["unit"]
            assert unit.endswith(".timer"), f"{unit} is not a .timer unit"
            assert unit not in seen, f"{unit} declared twice in budget.yaml"
            seen.add(unit)
            assert re.fullmatch(r"[1-9]\d*[smhd]", str(row["max_staleness"])), (
                f"{unit}: max_staleness must be <positive int><s|m|h|d>, "
                f"got {row['max_staleness']!r}"
            )
            # A bare number is unreviewable — the reasoning is the artifact.
            assert row.get("note", "").strip(), f"{unit}: needs a `note:` justifying its budget"

    def test_watchdogs_own_timer_is_covered(self):
        # If box-health.timer itself stops firing, nothing else on the box
        # notices — every other check runs *from* it.
        units = {r["unit"] for r in self._timers()}
        assert "box-health.timer" in units

    def test_manifest_generator_emits_thresholds_in_seconds(self):
        # The watchdog stays pure bash, so unit conversion happens at install
        # time. Guard the conversion, not just the YAML.
        import subprocess

        gen = REPO_ROOT / "infrastructure" / "generate-box-manifest.py"
        proc = subprocess.run(
            [sys.executable, str(gen), "--stdout"],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "declare -A TIMER_MAX_STALENESS=(" in proc.stdout
        assert '["box-health.timer"]=1800' in proc.stdout      # 30m
        assert '["metron-refresh.timer"]=93600' in proc.stdout  # 26h

    def test_bad_duration_fails_the_install_not_the_watchdog(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "genmanifest", REPO_ROOT / "infrastructure" / "generate-box-manifest.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.parse_duration("x.timer", "26h") == 93600
        assert mod.parse_duration("x.timer", "45m") == 2700
        for bad in ["26", "h", "0h", "-5m", "26 h", "abc", "", "1w"]:
            try:
                mod.parse_duration("x.timer", bad)
            except ValueError:
                continue
            raise AssertionError(f"parse_duration accepted invalid duration {bad!r}")

    def test_timer_staleness_shell_test(self):
        self_ = TestInfraShellTests()
        self_._run("test_box_health_timer_staleness.sh")


class TestManifestPropagation:
    """The generated manifest must reach the box on merge, not by hand.

    /etc/alpha-engine/box-services.conf is derived from budget.yaml. Its only
    generator used to be install-resource-limits.sh, which is invoked by no
    workflow, no deploy path and no CI — so I4492's "one list" premise held in
    the repo and silently failed on the box. Verified live 2026-07-28: I5209
    merged and deployed while the installed manifest carried none of its
    thresholds.
    """

    def test_box_health_installer_generates_the_manifest(self):
        sh = (REPO_ROOT / "infrastructure" / "install-box-health.sh").read_text()
        assert "generate-box-manifest.py" in sh, (
            "install-box-health.sh must render the manifest — it is box_health.sh's "
            "input, and the installer is what deploy-on-merge.sh actually calls"
        )

    def test_deploy_gate_compares_rendered_manifest_not_just_file_pairs(self):
        sh = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert "manifest_stale()" in sh
        assert "manifest_stale || any_file_state_stale" in sh, (
            "the box-health gate must consult manifest_stale; a budget.yaml-only "
            "change leaves every compared src:dst pair byte-identical"
        )

    def test_gate_does_not_use_command_substitution_for_the_render(self):
        # $(...) strips trailing newlines, which made the gate report stale on
        # every deploy — a gate that is always true is not a gate.
        sh = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        body = sh.split("manifest_stale() {", 1)[1].split("\n}", 1)[0]
        assert "--stdout > " in body, "render must go to a file, not $(...) or a pipe"
        assert "rendered=$(" not in body
