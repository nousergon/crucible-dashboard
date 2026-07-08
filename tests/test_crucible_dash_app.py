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
        assert "--server.port=8503" in unit

    def test_nginx_routes_dash_to_8503(self):
        conf = (REPO_ROOT / "infrastructure" / "nginx.conf").read_text()
        assert "location /dash" in conf
        assert conf.index("location /dash") < conf.index("location / {"), \
            "the /dash location must precede the catch-all live proxy"
        dash_block = conf[conf.index("location /dash"):conf.index("location / {")]
        assert "http://127.0.0.1:8503" in dash_block

    def test_deploy_script_provisions_restarts_and_health_checks(self):
        script = (REPO_ROOT / "infrastructure" / "deploy-on-merge.sh").read_text()
        assert "crucible-dash.service" in script          # idempotent self-provision
        assert "systemctl restart crucible-dash" in script
        assert "8503/dash/_stcore/health" in script       # health gate
