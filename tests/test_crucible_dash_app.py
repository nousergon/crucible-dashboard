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

    def test_nginx_routes_dash_to_8504(self):
        conf = (REPO_ROOT / "infrastructure" / "nginx.conf").read_text()
        assert "location /dash" in conf
        assert conf.index("location /dash") < conf.index("location / {"), \
            "the /dash location must precede the catch-all live proxy"
        dash_block = conf[conf.index("location /dash"):conf.index("location / {")]
        assert "http://127.0.0.1:8504" in dash_block

    def test_deploy_script_provisions_restarts_and_health_checks(self):
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert "crucible-dash.service" in script          # idempotent self-provision
        assert "systemctl restart crucible-dash" in script
        assert "8504/dash/_stcore/health" in script       # health gate


class TestPortAllocation:
    """Regression guard for config#1957's 2026-07-08 deploy failure: the dash
    service shipped on :8503, which mnemon (bun, memory.nousergon.ai) already
    binds (*:8503). `streamlit run --server.port=8503` could not bind on the
    shared box, crash-looped, and the /dash/_stcore/health gate timed out —
    every deploy went red until the service was moved to the free :8504.

    box_health.sh's `port -> service` map is the box's source of truth for port
    ownership. These tests derive the dash port from the unit file and assert it
    (a) agrees everywhere it's referenced and (b) is registered to crucible-dash
    in that map and to nothing else — so a future port reuse fails in CI, not on
    a live box.
    """

    @staticmethod
    def _dash_port():
        import re
        unit = (REPO_ROOT / "infrastructure" / "crucible-dash.service").read_text()
        m = re.search(r"--server\.port=(\d+)", unit)
        assert m, "crucible-dash.service has no --server.port"
        return m.group(1)

    @staticmethod
    def _box_health_owners():
        # Parse the `#   <port> <service> ...` ownership table in box_health.sh.
        import re
        txt = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
        return dict(re.findall(r"^#\s+(\d{4})\s+(\S+)", txt, re.M))

    def test_dash_port_agrees_across_unit_nginx_and_deploy(self):
        port = self._dash_port()
        nginx = (REPO_ROOT / "infrastructure" / "nginx.conf").read_text()
        dash_block = nginx[nginx.index("location /dash"):nginx.index("location / {")]
        assert f"http://127.0.0.1:{port}" in dash_block, \
            f"nginx /dash proxy_pass disagrees with the unit's :{port}"
        deploy = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert f"{port}/dash/_stcore/health" in deploy, \
            f"deploy health gate disagrees with the unit's :{port}"

    def test_dash_port_is_registered_and_collision_free(self):
        port = self._dash_port()
        owners = self._box_health_owners()
        owner = owners.get(port, "")
        assert owner.startswith("crucible-dash"), (
            f"port {port} is not registered to crucible-dash in box_health.sh "
            f"(owner={owner or 'unlisted'!r}). Shipping the dash service on a port "
            f"another co-resident service owns crash-loops the deploy — pick a "
            f"free port and register it in the box_health.sh port map."
        )

    def test_dash_port_is_guarded_by_box_watchdog(self):
        import re
        port = self._dash_port()
        txt = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
        ports_line = re.search(r"^PORTS=\(([^)]*)\)", txt, re.M)
        assert ports_line and port in ports_line.group(1).split(), (
            f"port {port} is not in box_health.sh PORTS=() — add it so the box "
            f"watchdog monitors crucible-dash."
        )
