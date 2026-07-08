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
