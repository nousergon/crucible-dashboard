"""Wiring tests for the /dash exposure (config#1957 plan §8.5)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).parent.parent


class TestDashApp:
    def test_registers_all_six_crucible_views(self):
        src = (REPO_ROOT / "dash" / "app.py").read_text()
        for view in ("Crucible_Overview", "Crucible_Validation", "Crucible_Evaluation",
                     "Crucible_Execution", "Crucible_Feedback", "Crucible_Trust"):
            assert f"../views/{view}.py" in src, view

    def test_page_paths_resolve_from_dash_dir(self):
        # st.Page resolves relative to the entrypoint's parent — every ref
        # must exist at dash/../views/.
        src = (REPO_ROOT / "dash" / "app.py").read_text()
        import re
        for ref in re.findall(r'st\.Page\("([^"]+)"', src):
            assert (REPO_ROOT / "dash" / ref).resolve().exists(), ref

    def test_config_carries_baseurl_and_cors_fix(self):
        cfg = (REPO_ROOT / "dash" / ".streamlit" / "config.toml").read_text()
        assert 'baseUrlPath = "dash"' in cfg
        # Without the public-origin allowlist, Tornado rejects every WebSocket
        # handshake through the Worker proxy and the app hangs on load (same
        # gotcha documented in live/.streamlit/config.toml).
        assert '"https://crucible.nousergon.ai"' in cfg


class TestInfraWiring:
    def test_unit_file_matches_app_layout(self):
        unit = (REPO_ROOT / "infrastructure" / "crucible-dash.service").read_text()
        assert "WorkingDirectory=/home/ec2-user/alpha-engine-dashboard/dash" in unit
        assert "--server.port=8504" in unit

    def test_nginx_routes_dash_to_web(self):
        # 9-D cutover (config#1973): /dash serves the Next.js surface on
        # :3002; the Streamlit skin (:8504) stays running as rollback but is
        # no longer the route target.
        conf = (REPO_ROOT / "infrastructure" / "nginx.conf").read_text()
        assert "location /dash" in conf
        assert conf.index("location /dash") < conf.index("location / {"), \
            "the /dash location must precede the catch-all live proxy"
        dash_block = conf[conf.index("location /dash"):conf.index("location / {")]
        assert "http://127.0.0.1:3002" in dash_block

    def test_deploy_script_provisions_restarts_and_health_checks(self):
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert "crucible-dash.service" in script          # idempotent self-provision
        assert "systemctl restart crucible-dash" in script
        assert "8504/dash/_stcore/health" in script       # health gate

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
        # leaving all 4 services crash-looping on the broken venv for ~25
        # minutes. A rollback path must exist and must be invoked on the
        # post-swap health-gate failure branch, not just logged about.
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        heal_block = script[script.index("Python-parity self-heal: box venv is"):
                             script.index("# ── 1. Refresh deps")]

        assert "_rollback_venv()" in heal_block, "no rollback function defined in the self-heal block"

        # The preserved old venv must actually get restored on rollback.
        rollback_start = heal_block.index("_rollback_venv() {")
        rollback_fn = heal_block[rollback_start:
                                  rollback_start + heal_block[rollback_start:].index("\n        }")]
        assert 'mv "$OLD_VENV_BACKUP" "$REPO_DIR/.venv"' in rollback_fn
        assert "systemctl restart dashboard nous-ergon-live crucible-dash crucible-dash-api" in rollback_fn
        assert rollback_fn.count("wait_for_health") == 4, \
            "rollback must re-verify health on all 4 services before considering itself successful"

        # The post-swap health-gate failure branch must actually call the
        # rollback — not just `fail` on its own, and not just mention
        # rollback in a comment.
        post_swap_gate = heal_block[heal_block.index("# 5. Reuse the script's existing health-gate"):]
        assert "_rollback_venv" in post_swap_gate
        assert 'fail "python-parity self-heal: post-swap health gate failed' in post_swap_gate
        assert "ROLLED BACK to previous venv successfully" in post_swap_gate

    def test_dash_web_build_gate_unaffected_by_state_compare_migration(self):
        # config#2338 scoped the fix to requirements/nginx/installer gates
        # only; the dash-web build gate is a separate cost tradeoff (npm ci +
        # next build is expensive) and keeps its existing commit-range gate
        # plus its own missing-build fallback.
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert 'paths_changed "${CURRENT_SHA}~1" "$CURRENT_SHA" dash-web/' in script
        assert '[ ! -d "$WEB_DIR/.next" ]' in script

    def test_dash_port_registered_and_collision_free(self):
        # config#1972 Part A: box_health.sh's port map is still a
        # hand-maintained comment + SERVICES/PORTS arrays, not a derived
        # registry (that's Part B) — but this at least fails loudly if a
        # future edit reintroduces the #354/config#1957 bug class: the dash
        # app's --server.port drifting out of sync with the watchdog's port
        # map, colliding with another guarded service's port, or shipping
        # unmonitored (present nowhere in SERVICES/PORTS, so box-health
        # would never page if /dash died).
        import re

        unit = (REPO_ROOT / "infrastructure" / "crucible-dash.service").read_text()
        port_match = re.search(r"--server\.port=(\d+)", unit)
        assert port_match, "crucible-dash.service must declare --server.port"
        dash_port = port_match.group(1)

        box_health = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()

        # Parse the hand-maintained "port -> service" comment map, e.g.:
        #   "#   8504 crucible-dash.service    (crucible.nousergon.ai/dash)"
        comment_map = {}
        for line in box_health.splitlines():
            m = re.match(r"#\s+(\d{3,5})\s+(\S.*?)\s{2,}\(", line)
            if m:
                comment_map.setdefault(m.group(1), []).append(m.group(2).strip())

        assert dash_port in comment_map, (
            f"port {dash_port} (crucible-dash.service's --server.port) is not "
            f"recorded in box_health.sh's port map comment"
        )
        owners = comment_map[dash_port]
        assert len(owners) == 1 and "crucible-dash" in owners[0], (
            f"port {dash_port} is claimed by {owners!r} in box_health.sh's port "
            f"map comment, not solely by crucible-dash.service — collision risk"
        )

        # No OTHER port entry in the map may also claim crucible-dash — and
        # symmetrically, nothing else may be recorded under the dash port.
        for port, names in comment_map.items():
            if port == dash_port:
                continue
            assert not any("crucible-dash" in n for n in names), (
                f"crucible-dash also appears under port {port} in box_health.sh's "
                f"port map comment"
            )

        ports_array_match = re.search(r"^PORTS=\(([^)]*)\)", box_health, re.M)
        assert ports_array_match, "box_health.sh must declare a PORTS=(...) watchdog array"
        watchdog_ports = ports_array_match.group(1).split()
        assert dash_port in watchdog_ports, (
            f"port {dash_port} is missing from box_health.sh's PORTS=(...) "
            f"watchdog array — crucible-dash would be un-monitored (box-health "
            f"would not page if /dash died)"
        )

        services_array_match = re.search(r"^SERVICES=\(([^)]*)\)", box_health, re.M)
        assert services_array_match, "box_health.sh must declare a SERVICES=(...) watchdog array"
        watchdog_services = services_array_match.group(1).split()
        assert "crucible-dash.service" in watchdog_services, (
            "crucible-dash.service is missing from box_health.sh's SERVICES=(...) "
            "watchdog array — systemctl is-active would never be checked for it"
        )
