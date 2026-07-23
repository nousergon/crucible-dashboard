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
        # A historical comment noting :8504's reuse history is fine — the
        # watchdog's actual SERVICES/PORTS arrays must not carry the retired
        # unit/port forward.
        box_health = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
        import re
        services_array_match = re.search(r"^SERVICES=\(([^)]*)\)", box_health, re.M)
        assert services_array_match
        assert "crucible-dash.service" not in services_array_match.group(1).split()
        ports_array_match = re.search(r"^PORTS=\(([^)]*)\)", box_health, re.M)
        assert ports_array_match
        assert "8504" not in ports_array_match.group(1).split()

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

    def test_dash_web_build_gate_unaffected_by_state_compare_migration(self):
        # config#2338 scoped the fix to requirements/nginx/installer gates
        # only; the dash-web build gate is a separate cost tradeoff (npm ci +
        # next build is expensive) and keeps its existing commit-range gate
        # plus its own missing-build fallback.
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert 'paths_changed "${CURRENT_SHA}~1" "$CURRENT_SHA" dash-web/' in script
        assert '[ ! -d "$WEB_DIR/.next" ]' in script

    def test_port_8504_freed_by_retirement_not_reclaimed_silently(self):
        # config#1972 Part A lives on for the remaining hand-maintained
        # port map: :8504 was crucible-dash's (retired config#1973) — this
        # guards against a future service silently reusing the port without
        # updating the comment map / SERVICES / PORTS arrays in lockstep,
        # the exact drift class #1972/#354 existed to catch.
        import re

        box_health = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
        services_array_match = re.search(r"^SERVICES=\(([^)]*)\)", box_health, re.M)
        assert services_array_match
        assert "crucible-dash.service" not in services_array_match.group(1).split()

        ports_array_match = re.search(r"^PORTS=\(([^)]*)\)", box_health, re.M)
        assert ports_array_match, "box_health.sh must declare a PORTS=(...) watchdog array"
        assert "8504" not in ports_array_match.group(1).split(), (
            "8504 (retired crucible-dash's port) should not be watched unless "
            "a new service has claimed it — if so, update this test with the "
            "new owner"
        )
