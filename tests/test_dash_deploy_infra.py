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
        # Intent, not arithmetic. This asserted `count(...) == 4` until §2f
        # added the table-driven installer router (config-I5215) and the total
        # became 5. An equality on a count is brittle in both directions: it
        # breaks on unrelated additions, and it can be restored to passing by
        # an edit that rebalances the total while a real gate is gone. It also
        # cannot say WHICH gate regressed — the same objection box_health.sh
        # records for its own coverage check ("named, not counted").
        #
        # What actually matters here is that no gate in this region reverts to
        # a commit-range diff (the config#2338 defect this test exists for).
        # That every installer is invoked at all is guarded separately and
        # exhaustively by TestProvisioningScriptRouting.
        assert "any_file_state_stale" in installer_block
        assert "CURRENT_SHA}~1" not in installer_block
        # Floor, not equality: gates are only ever added, so this catches a
        # gate being deleted without breaking when one is added.
        assert installer_block.count("any_file_state_stale") >= 4

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



    def test_nginx_overrides_streamlit_static_immutable_cache(self):
        """Streamlit emits year-long immutable Cache-Control on /static/js/*
        (PlotlyChart.<hash>.js class, 2026-08-02). Origin must hide that header
        and emit a short max-age so a bad body cannot freeze for 365d.
        """
        conf = (REPO_ROOT / "infrastructure" / "nginx.conf").read_text()
        # Both Streamlit vhosts carry a dedicated /static/ location.
        assert conf.count("location /static/") >= 2
        assert "proxy_hide_header Cache-Control" in conf
        assert 'add_header Cache-Control "public, max-age=300" always' in conf
        # Console block proxies static to :8501; live block to :8502.
        # Pull the two static blocks by splitting on the location directive.
        parts = conf.split("location /static/")
        assert len(parts) >= 3  # preamble + >=2 blocks
        bodies = parts[1:]
        ports = set()
        for body in bodies:
            # take until next location or closing server brace-ish; just search
            head = body[:400]
            if "127.0.0.1:8501" in head:
                ports.add("8501")
            if "127.0.0.1:8502" in head:
                ports.add("8502")
        assert ports == {"8501", "8502"}, ports

    def test_deploy_purges_streamlit_static_after_requirements_install(self):
        """A requirements.txt install is the only signal the on-disk hashed
        chunk set may have rotated. deploy-on-merge must best-effort purge.
        """
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        purge = (REPO_ROOT / "infrastructure" / "purge_streamlit_static_cache.sh")
        assert purge.is_file(), "purge helper must exist"
        # Hook lives inside the requirements-install success path.
        req_block = script[script.index('REQUIREMENTS_STAMP='):script.index("# ── 1b.")]
        assert "purge_streamlit_static_cache.sh" in req_block
        assert "WARN streamlit static CF cache purge failed" in req_block
        # Operator one-shot + merge-path purge (deploy.yml job) both exist so the
        # merge button alone recovers Cost & Usage — no post-merge click.
        wf = (REPO_ROOT / ".github" / "workflows" / "purge-streamlit-static-cache.yml")
        assert wf.is_file()
        body = wf.read_text()
        assert "workflow_dispatch" in body
        assert "purge_streamlit_static_cache.sh" in body
        assert "PlotlyChart.CylVV9WQ.js" in body
        deploy_wf = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text()
        assert "purge-streamlit-static:" in deploy_wf
        assert "purge_streamlit_static_cache.sh" in deploy_wf

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

    def test_box_health_unit_identity(self):
        # Guards classify_identity() — whether a unit's User=/Group= resolves,
        # i.e. whether it could start AGAIN. `is-active` cannot answer that: on
        # 2026-07-28 eight units reported active while carrying User=svc-<name>
        # for accounts nothing had created, one restart from taking the box
        # down (nous-ergon-ops-I155).
        self._run("test_box_health_unit_identity.sh")

    def test_box_health_throttle_rate(self):
        # Guards that cgroup throttling is judged on the counter's MOVEMENT.
        # memory.events::high is monotonic per-cgroup, so the previous `> 0`
        # rule could never clear and survived its own remedy (config-I5216).
        self._run("test_box_health_throttle_rate.sh")

    def test_deploy_auto_revert(self):
        # T1-3: health checks existed but nothing rolled the box back, so a bad
        # merge left it broken until a human noticed (config-I5250 gap 3).
        self._run("test_deploy_auto_revert.sh")

    def test_deploy_manifest_gate(self):
        # Guards the gate that makes a budget.yaml-only change actually reach
        # the box. Before it, the manifest was generated ONLY by
        # install-resource-limits.sh, which nothing automated invokes.
        self._run("test_deploy_manifest_gate.sh")

    def test_deploy_on_merge_paths_changed(self):
        # Pre-existing script (config#2242), previously unwired from CI.
        self._run("test_deploy_on_merge_paths_changed.sh")

    def test_installer_runtime_override_guard(self):
        # alpha-engine-config-I6277: a `systemctl set-property --runtime`
        # cap outranks the generated /etc drop-in and install-resource-
        # limits.sh used to write a losing file with no effect on the
        # running unit. Must be shown to FAIL against an unfixed fixture,
        # not merely pass against a clean one.
        self._run("test_installer_runtime_override_guard.sh")


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


class TestProvisioningScriptRouting:
    """Every provisioning script must be routed on deploy, or declared manual-only.

    These scripts provision state OUTSIDE the git tree — systemd units,
    /usr/local/bin copies, agent configs, CloudWatch alarms — which a `git
    pull` never touches. Six of them were invoked by nothing automated, so the
    repo and the running box drifted indefinitely while every check read green
    (config-I5215). The orphans were found by an ad-hoc grep during an
    unrelated incident; this class is what makes the next one impossible.
    """

    DEPLOY = "infrastructure/deploy-on-merge.sh"

    def _rows(self):
        import re

        sh = (REPO_ROOT / self.DEPLOY).read_text()
        block = re.search(r"ROUTED_INSTALLERS=\((.*?)\n\)", sh, re.S)
        assert block, "deploy-on-merge.sh must declare ROUTED_INSTALLERS"
        rows = []
        for line in block.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(line.strip('"').split("|"))
        assert rows, "ROUTED_INSTALLERS is empty"
        return rows

    def _manual(self):
        import re

        sh = (REPO_ROOT / self.DEPLOY).read_text()
        # Two forms must both parse: an empty `=()` on one line, and a
        # multi-line list. Anchor the multi-line close on a line-start paren —
        # reasons contain parentheses (e.g. "(config-I5211)") and a bare `\)`
        # stops at the first of them.
        assert "MANUAL_ONLY_INSTALLERS=(" in sh, \
            "deploy-on-merge.sh must declare MANUAL_ONLY_INSTALLERS"
        block = re.search(r"MANUAL_ONLY_INSTALLERS=\(\)", sh) and None
        if block is None and re.search(r"MANUAL_ONLY_INSTALLERS=\(\s*\)", sh):
            return set()
        block = re.search(r"MANUAL_ONLY_INSTALLERS=\((.*?)\n\)", sh, re.S)
        assert block is not None, "MANUAL_ONLY_INSTALLERS is malformed"
        return {
            ln.strip().strip('"').split("|")[0]
            for ln in block.group(1).splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        }

    # A script provisions if it writes state a `git pull` never touches. Keyed
    # on what the script DOES, not what it is called: this guard used to glob
    # install-*.sh, so create-service-users.sh — 271 lines whose entire job is
    # creating thirteen Unix accounts the systemd units reference — matched
    # nothing, was routed by nothing, and never ran. budget.yaml shipped
    # User=svc-<name> for all thirteen anyway; every unit that restarted died
    # with 217/USER (alpha-engine-config-I4791). The guard was working exactly
    # as written and caught nothing, because the naming convention it trusted
    # was never an invariant — a provisioning script is free to be called
    # anything, and eventually one was.
    PROVISIONING_MARKERS = (
        "/etc/systemd/system",
        "/usr/local/bin",
        "/etc/alpha-engine",
        "systemctl enable",
        "useradd",
        "groupadd",
        "put-metric-alarm",
        "amazon-cloudwatch-agent-ctl",
    )

    # Scripts that contain a marker but are not themselves provisioners. Each
    # is a PAYLOAD an installer copies onto the box, the deploy driver itself,
    # or a test. Exempting by name is safe here only because the entry states
    # which installer owns it — an orphan payload has an orphan installer, and
    # that installer is still caught by the check below.
    NOT_PROVISIONERS = {
        # Both entered this list on 2026-08-13 (config-I7168) when the shared
        # ALERT_PY preamble gave every alert publisher a comment naming
        # /usr/local/bin — the marker, not the behaviour. Each was already a
        # payload; nothing about what they do changed.
        "alert_on_failure.sh": "payload; installed as a unit template by install-substrate-health-daily.sh",
        "reboot_if_needed.sh": "payload; installed by install-auto-patching.sh",
        "boot-pull.sh": "payload; installed + scheduled by install-boot-pull.sh",
        "box_health.sh": "payload; installed by install-box-health.sh",
        "box_hygiene.sh": "payload; installed by install-box-health.sh",
        "deploy-on-merge.sh": "the deploy driver itself; invoked by deploy.yml",
        "morning-signal-recover.sh": "payload; installed by install-morning-signal.sh",
        "morning-signal-watchdog.sh": "payload; installed by install-morning-signal-watchdog.sh",
        "test_deploy_manifest_gate.sh": "test",
        "test_installer_routing.sh": "test",
    }

    def _provisioning_scripts(self):
        out = []
        for path in sorted((REPO_ROOT / "infrastructure").glob("*.sh")):
            if path.name in self.NOT_PROVISIONERS:
                continue
            body = path.read_text()
            if any(m in body for m in self.PROVISIONING_MARKERS):
                out.append(path)
        return out

    def test_not_provisioner_exemptions_still_exist(self):
        # An exemption for a deleted file is dead config that silently widens
        # the guard the next time someone reuses the name.
        for name in self.NOT_PROVISIONERS:
            assert (REPO_ROOT / "infrastructure" / name).exists(), (
                f"NOT_PROVISIONERS names {name}, which no longer exists — "
                f"remove the entry"
            )

    def test_no_installer_is_orphaned(self):
        sh = (REPO_ROOT / self.DEPLOY).read_text()
        routed = {r[0] for r in self._rows()}
        manual = self._manual()

        for path in self._provisioning_scripts():
            name = path.name
            # A few predate the table and have bespoke gates; accept a real
            # invocation, not a mere mention (a comment naming a script is how
            # the original orphan sweep produced a false negative).
            invoked = f'bash "$REPO_DIR/infrastructure/{name}"' in sh
            # An installer may legitimately run in CI rather than on the box —
            # install-host-alarms.sh must, since the instance role deliberately
            # lacks cloudwatch:PutMetricAlarm (config-I5211). "Automated
            # somewhere" is the property that matters, not "automated here".
            in_ci = any(
                f"infrastructure/{name}" in wf.read_text()
                for wf in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
            )
            assert name in routed or name in manual or invoked or in_ci, (
                f"{name} provisions state outside the git tree but is invoked "
                f"by nothing: add a ROUTED_INSTALLERS row, or a "
                f"MANUAL_ONLY_INSTALLERS entry stating why it must not run on "
                f"deploy, or a call from a .github/workflows/*.yml job — or, if "
                f"it does not actually provision, a NOT_PROVISIONERS entry "
                f"naming the installer that owns it. A provisioning script "
                f"nothing calls means the box silently drifts from main "
                f"(config-I5215), or that main declares config the box was "
                f"never given (alpha-engine-config-I4791)."
            )

    def test_declared_users_require_the_account_creator_to_be_routed(self):
        """budget.yaml may not declare `user:` while create-service-users.sh is manual.

        This is the exact coupling that broke the box on 2026-07-28. The
        `user:` fields and the script that creates those accounts are two
        halves of one migration; PR #566 merged the half that REFERENCES the
        accounts without the half that CREATES them, CI was green, and every
        unit that restarted died with 217/USER.

        Neither half is wrong alone — a declared user with the creator routed
        is the migration's finished state, and no declared users with the
        creator manual-only is where it sits today. Only the combination is
        unbootable, so the combination is what is asserted.
        """
        import yaml

        spec = yaml.safe_load(
            (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
             / "budget.yaml").read_text()
        )
        declared = sorted(
            s["unit"] for s in spec["services"] if s.get("user")
        )
        if not declared:
            return  # migration not activated — nothing to couple

        routed = {r[0] for r in self._rows()}
        assert "create-service-users.sh" in routed, (
            f"budget.yaml declares user: for {declared}, which renders "
            f"User=<account> into their drop-ins — but create-service-users.sh "
            f"is not in ROUTED_INSTALLERS, so nothing creates those accounts on "
            f"the box. systemd cannot start a unit whose User= does not resolve "
            f"(217/USER). Route the creator in the same PR that declares the "
            f"users (alpha-engine-config-I4791)."
        )

    def test_renderer_refuses_unresolvable_users(self):
        """The renderer must verify declared accounts exist before writing.

        Routing the creator is necessary but not sufficient: it can be routed
        and still have failed, or the accounts can be removed out from under a
        declaration. The renderer is the last point at which this is cheap to
        catch, so it checks the host rather than trusting the manifest.
        """
        sh = (REPO_ROOT / "infrastructure" / "install-resource-limits.sh").read_text()
        assert "getent passwd" in sh, (
            "install-resource-limits.sh must verify every declared user "
            "resolves before rendering User= into any drop-in"
        )
        assert "REFUSING to install" in sh.split("CHANGED=0")[0], (
            "the user preflight must run BEFORE the render loop — refusing "
            "after some drop-ins are written leaves the box half-migrated"
        )

    def test_manual_only_entries_state_a_reason(self):
        import re

        sh = (REPO_ROOT / self.DEPLOY).read_text()
        block = re.search(r"MANUAL_ONLY_INSTALLERS=\((.*?)\n\)", sh, re.S)
        if block is None:
            return  # empty `=()` — nothing to validate
        for ln in block.group(1).splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            assert "|" in ln, f"manual-only entry needs `name|reason`: {ln}"
            assert ln.strip('"').split("|", 1)[1].strip(), f"empty reason: {ln}"

    def test_every_declared_source_path_exists(self):
        # A typo'd src path makes the gate permanently wrong — always stale
        # (installer runs every deploy) or, worse, never stale. Neither is
        # visible at runtime; both are visible here.
        for name, mode, args in self._rows():
            for arg in args.split(","):
                rel = arg.split(":")[0] if mode == "files" else arg
                assert (REPO_ROOT / "infrastructure" / rel).exists(), (
                    f"{name}: declared source infrastructure/{rel} does not exist"
                )

    def test_modes_are_known(self):
        for name, mode, _args in self._rows():
            assert mode in {"files", "stamp"}, f"{name}: unknown routing mode {mode!r}"

    def test_file_mode_rows_declare_absolute_destinations(self):
        for name, mode, args in self._rows():
            if mode != "files":
                continue
            for pair in args.split(","):
                assert ":" in pair, f"{name}: files row needs src:dst pairs, got {pair!r}"
                assert pair.split(":", 1)[1].startswith("/"), (
                    f"{name}: destination must be absolute, got {pair!r}"
                )

    def test_routing_shell_test(self):
        TestInfraShellTests()._run("test_installer_routing.sh")


class TestOffBoxHealthVerdict:
    """The watchdog's verdict must reach CloudWatch, not only SNS/Telegram.

    Metrics and alerts are separate code paths with separate failure modes. If
    only the alert path carries the verdict, a broken alert path means a
    confirmed problem is found and silently dropped — while emit_metrics keeps
    publishing, so the box-liveness alarm stays green. The box looks healthy
    precisely because the part that would say otherwise is the part that broke
    (config-I5211; the config#1646 silent exit-0 alerts no-op is this class).
    """

    def test_verdict_is_published_on_every_exit_path(self):
        sh = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
        assert "publish_verdict()" in sh
        # Two clean-exit paths (first sample clean, and self-healed during the
        # confirmation window) plus the problem path. A missed path would leave
        # a stale non-zero metric latched and page forever.
        assert sh.count("publish_verdict 0") == 2, (
            "both clean-exit paths must publish 0; a latched non-zero metric "
            "would keep the alarm firing after the problem cleared"
        )
        assert 'publish_verdict "$(printf' in sh

    def test_verdict_is_published_before_alerting(self):
        # Ordering is the whole point: a failing alert path must not also
        # prevent the count being recorded.
        sh = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
        verdict = sh.index('publish_verdict "$(printf')
        alert = sh.index("-m krepis.alerts publish")
        assert verdict < alert, "verdict must be published before the alert attempt"

    def test_alarm_exists_and_does_not_double_page_on_silence(self):
        wf = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text()
        block = wf[wf.index("alpha-engine-dashboard-health-problems"):]
        block = block[: block.index("--ok-actions")]
        assert "--metric-name health_problems" in block
        # notBreaching, deliberately: absent data means the box or watchdog is
        # down, already covered by box-disk-critical's missing-data breach.
        assert "--treat-missing-data notBreaching" in block, (
            "missing data must NOT breach here — box-disk-critical already "
            "breaches on the same silence, and two alarms for one cause "
            "double-pages"
        )

    def test_box_liveness_alarm_still_breaches_on_missing(self):
        # The complement of the above. If this ever flips to notBreaching,
        # nothing detects a dead box at all.
        wf = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text()
        block = wf[wf.index("alpha-engine-dashboard-box-disk-critical"):]
        block = block[: block.index("--ok-actions")]
        assert "--treat-missing-data breaching" in block


class TestDeployWorkflowSelfConsistency:
    """A deploy step that runs a repo file needs the repo on the runner.

    The deploy job historically needed no checkout — it only sends SSM commands,
    and the box runs scripts from its own checkout. When config-I5211 added a
    step running `bash infrastructure/install-host-alarms.sh` on the RUNNER, the
    job died with exit 127 "No such file or directory". CI was green: nothing
    tests a workflow's internal consistency, only its YAML validity.
    """

    def _deploy(self):
        return (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text()

    def test_repo_scripts_in_deploy_require_a_checkout(self):
        import re

        wf = self._deploy()
        runs_repo_file = re.findall(r"bash (infrastructure/[\w./-]+)", wf)
        if not runs_repo_file:
            return
        assert "actions/checkout@" in wf, (
            f"deploy.yml runs repo file(s) {runs_repo_file} on the runner but "
            f"never checks the repo out — the step will fail with exit 127"
        )
        # Checkout must precede the first such step, or it fails the same way.
        assert wf.index("actions/checkout@") < wf.index(f"bash {runs_repo_file[0]}")

    def test_referenced_repo_scripts_actually_exist(self):
        import re

        for rel in re.findall(r"bash (infrastructure/[\w./-]+)", self._deploy()):
            assert (REPO_ROOT / rel).is_file(), f"deploy.yml references missing {rel}"


class TestDurableStateRegistry:
    """budget.yaml::state[] is the T1-4 inventory, and the only way into backup.

    Policy T1-4: every SQLite file, vault and cert is either replicated or
    documented as accepted-loss with a stated RPO — "unlisted state is a
    defect". Audited 2026-07-28: NO application state on the box was
    replicated, and that was an absence rather than a decision
    (config-I5250 gap 1).
    """

    def _state(self):
        import yaml

        spec = yaml.safe_load(
            (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
             / "budget.yaml").read_text()
        )
        assert spec.get("state"), "budget.yaml must declare a `state:` inventory"
        return spec["state"]

    def test_every_entry_has_a_decided_disposition(self):
        seen = set()
        for e in self._state():
            path = e["path"]
            assert path not in seen, f"{path} declared twice"
            seen.add(path)
            assert e["disposition"] in {"replicate", "accepted-loss", "external"}, (
                f"{path}: disposition must be replicate | accepted-loss | external"
            )
            assert e.get("note", "").strip(), f"{path}: needs a note stating the reasoning"

    def test_accepted_loss_states_an_rpo(self):
        # The whole T1-4 proviso: accepted-loss is legitimate ONLY when stated.
        # Without an RPO, "accepted-loss" and "never considered" are the same row.
        for e in self._state():
            if e["disposition"] == "accepted-loss":
                assert str(e.get("rpo", "")).strip(), (
                    f"{e['path']}: accepted-loss requires an explicit rpo:"
                )

    def test_external_states_its_source(self):
        for e in self._state():
            if e["disposition"] == "external":
                assert str(e.get("source", "")).strip(), (
                    f"{e['path']}: external requires source: naming the real authority"
                )

    def test_shared_identity_db_is_replicated(self):
        # Largest blast radius of any single file on the box: losing it
        # invalidates sessions and users across every product at once.
        entry = next(e for e in self._state() if "nousergon-auth" in e["path"])
        assert entry["disposition"] == "replicate"

    def test_metron_personal_sqlite_is_external_after_neon_cutover(self):
        # 2026-08-04: left as replicate after the 7/31 Neon cutover →
        # box-state-backup exited non-zero every night ("declared replicate
        # but does not exist") and box-health paged CRITICAL. Neon is SoT.
        entry = next(e for e in self._state() if e["path"].endswith("/personal.sqlite"))
        assert entry["disposition"] == "external", (
            "metron personal.sqlite must not be disposition:replicate after "
            "the Neon cutover — that makes the nightly backup fail loud"
        )
        assert "neon" in entry.get("source", "").lower()

    def test_private_keys_are_not_replicated(self):
        # Copying private keys into an object store to survive a box loss
        # trades a bounded availability problem for an unbounded
        # confidentiality one. This must stay a deliberate no.
        entry = next(e for e in self._state() if e["path"].endswith("/.ssh/"))
        assert entry["disposition"] != "replicate"

    def test_nousergon_console_is_in_the_service_registry(self):
        # Enabled on the box 2026-08-04 without a budget.yaml row → watchdog
        # named it unmonitored. budget.yaml is the only enrollment path.
        import yaml

        spec = yaml.safe_load(
            (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
             / "budget.yaml").read_text()
        )
        units = {s["unit"] for s in spec["services"]}
        assert "nousergon-console.service" in units
        console = next(s for s in spec["services"] if s["unit"] == "nousergon-console.service")
        assert console.get("port") == 5180

    def test_manifest_exports_declared_paths_for_the_coverage_check(self):
        import subprocess

        gen = REPO_ROOT / "infrastructure" / "generate-box-manifest.py"
        proc = subprocess.run([sys.executable, str(gen), "--stdout"],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert "STATE_DECLARED=(" in proc.stdout
        assert "/home/ec2-user/nousergon-auth/auth.sqlite" in proc.stdout

    def test_watchdog_names_undeclared_state(self):
        sh = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
        assert "undeclared durable state" in sh
        assert "STATE_DECLARED" in sh

    def test_backup_covers_exactly_the_replicate_entries(self):
        # The script must not carry its own list — the registry is the only
        # way in, so that adding a database without declaring it fails loudly
        # rather than being silently unprotected.
        src = (REPO_ROOT / "infrastructure" / "backup_box_state.py").read_text()
        assert 'disposition") != "replicate"' in src
        for e in self._state():
            if e["disposition"] == "replicate":
                assert e["path"] not in src, (
                    f"{e['path']} is hardcoded in backup_box_state.py — it must "
                    f"come from budget.yaml only"
                )

    def test_backup_uses_the_online_backup_api_not_a_file_copy(self):
        # A byte copy of a live SQLite file is crash-consistent, not
        # application-consistent, and may need WAL recovery to open.
        src = (REPO_ROOT / "infrastructure" / "backup_box_state.py").read_text()
        assert ".backup(" in src
        assert "mode=ro" in src, "source must be opened read-only"

    def test_backup_timer_is_dead_man_covered(self):
        import yaml

        spec = yaml.safe_load(
            (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits"
             / "budget.yaml").read_text()
        )
        units = {t["unit"] for t in spec["timers"]}
        assert "box-state-backup.timer" in units, (
            "a backup that stops running must be caught by the timer dead-man"
        )

    def test_installer_and_deploy_gate_cover_the_new_units(self):
        inst = (REPO_ROOT / "infrastructure" / "install-box-health.sh").read_text()
        assert "box-state-backup.service" in inst and "box-state-backup.timer" in inst
        dep = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert "box-state-backup.timer:/etc/systemd/system/box-state-backup.timer" in dep

    def test_backup_catchup_gates_on_budget_reinstall_not_only_result(self):
        # PR626 deploy: install-box-health/daemon-reload cleared Result=exit-code
        # to success before the catch-up ran, so a Result-only gate skipped it
        # while the registry fix was the whole point of the deploy.
        dep = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert "_budget_reinstalled=1" in dep
        assert 'install-resource-limits.sh" ] && _budget_reinstalled=1' in dep
        assert "systemctl start box-state-backup.service" in dep
        assert "_budget_reinstalled" in dep and '!= "success"' in dep
