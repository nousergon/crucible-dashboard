"""dash-web must bind loopback, not every interface (alpha-engine-config-I4526).

Next.js `next start` defaults to binding 0.0.0.0. On the dashboard box that put
:3002 on every interface, reachable by anything that could route to the host.

Nothing external could actually reach it -- alpha-engine-config-I4484 narrowed
the security group to Cloudflare ranges on 443 only, and a direct-origin probe
times out. This is defense in depth: it removes the security group as the SOLE
control, so a future SG edit cannot silently expose the service.

nginx proxies to 127.0.0.1:3002, so loopback is where it should have been.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PKG = REPO_ROOT / "dash-web" / "package.json"
NGINX = REPO_ROOT / "infrastructure" / "nginx.conf"


def _start_script() -> str:
    return json.loads(PKG.read_text())["scripts"]["start"]


def test_start_binds_loopback_explicitly():
    # `next start` with no -H binds 0.0.0.0. The flag must be present and
    # explicit -- relying on an env var (HOSTNAME) is rejected deliberately:
    # HOSTNAME conventionally means the machine's name, and on this box
    # thirteen services share one environment.
    start = _start_script()
    assert re.search(r"(^|\s)(-H|--hostname)\s+127\.0\.0\.1(\s|$)", start), (
        f"dash-web start script must bind 127.0.0.1 explicitly; got: {start!r}"
    )


def test_start_does_not_bind_all_interfaces():
    start = _start_script()
    for bad in ("-H 0.0.0.0", "--hostname 0.0.0.0", "-H ::"):
        assert bad not in start, f"dash-web must not bind all interfaces ({bad})"


def test_nginx_proxies_to_the_same_loopback_port():
    # The bind and the proxy target must agree. If someone changes the port in
    # one place, this fails rather than producing a 502 in production.
    if not NGINX.exists():
        return
    start = _start_script()
    port = re.search(r"-p\s+(\d+)", start)
    assert port, f"could not read port from start script: {start!r}"
    assert f"http://127.0.0.1:{port.group(1)}" in NGINX.read_text(), (
        f"nginx.conf has no proxy_pass to 127.0.0.1:{port.group(1)}, but "
        f"dash-web binds there"
    )
