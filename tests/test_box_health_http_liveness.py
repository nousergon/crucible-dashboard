"""A listening port is not liveness.

WHY THIS EXISTS
---------------
`box_health.sh` proved a service was alive by checking that its port appeared
in `ss -tln`. The kernel keeps a socket bound while the server behind it
answers nothing at all, so that check passes throughout the failure it most
needs to catch.

Measured, 2026-08-03: `vires.service` sat wedged for ~18 minutes — pinned at
its cgroup MemoryHigh, stalled >50% of wall-clock on reclaim. From on the box,
`curl -m 20 http://127.0.0.1:8530/health` returned `000` after timing out,
while `systemctl is-active` said `active`, port 8530 was listening, and four
consecutive watchdog ticks reported no port problem. A human found it.

The two tests that matter here run REAL servers on loopback: one that answers,
one that accepts the connection and never replies. The second is indetectable
to the port check by construction — `test_the_port_check_cannot_tell_them_apart`
asserts exactly that, so this file also documents why the new check is not
redundant with the old one.
"""

import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
BOX_HEALTH = (REPO_ROOT / "infrastructure" / "box_health.sh").read_text()
GENERATOR = REPO_ROOT / "infrastructure" / "generate-box-manifest.py"
BUDGET = yaml.safe_load(
    (REPO_ROOT / "infrastructure" / "systemd" / "resource-limits" / "budget.yaml").read_text()
)

# The probe timeout box_health.sh actually uses, lifted from the script so the
# tests cannot drift from it. Falls back rather than raising at import time, so
# a script missing the constant fails the CONTRACT test below with a readable
# message instead of collapsing collection of the whole module.
_m = re.search(r"^HTTP_PROBE_TIMEOUT=(\d+)", BOX_HEALTH, re.M)
PROBE_TIMEOUT = int(_m.group(1)) if _m else 3


class _Answers(BaseHTTPRequestHandler):
    """A healthy service. Answers 404 — deliberately NOT 200.

    Measured on the box the same day: five of thirteen HTTP ports answer 404 to
    a bare GET (the Next.js apps and nous-ergon-live serve under base paths) and
    one answers 400. All are healthy. A probe demanding 200 would page on six
    working services, so the predicate must be "answered at all".
    """

    def do_GET(self):  # noqa: N802 - stdlib interface
        self.send_error(404)

    def log_message(self, *_args):
        pass


@pytest.fixture()
def answering_server():
    srv = HTTPServer(("127.0.0.1", 0), _Answers)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture()
def wedged_server():
    """Accepts connections and never writes a byte — the failure mode.

    A raw listening socket that never `accept()`s past the backlog, and never
    responds to what it does accept. This is what a process stalled on memory
    reclaim looks like from outside: TCP completes, HTTP never does.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    held = []

    def accept_and_ignore():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            held.append(conn)  # keep it open, never reply

    threading.Thread(target=accept_and_ignore, daemon=True).start()
    yield sock.getsockname()[1]
    sock.close()
    for c in held:
        c.close()


def _probe(port: int) -> str:
    """The probe box_health.sh runs, with the same flags and timeout."""
    return subprocess.run(
        ["curl", "-s", "-m", str(PROBE_TIMEOUT), "-o", "/dev/null",
         "-w", "%{http_code}", f"http://127.0.0.1:{port}/"],
        capture_output=True, text=True,
    ).stdout.strip() or "000"


def _port_is_listening(port: int) -> bool:
    """The check this replaces: can something bind-check the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_a_service_that_answers_is_alive_even_on_404(answering_server):
    assert _probe(answering_server) == "404"


def test_a_wedged_service_reports_no_status_line(wedged_server):
    """The whole point: 000 means nothing was ever answered."""
    assert _probe(wedged_server) == "000"


def test_the_port_check_cannot_tell_them_apart(answering_server, wedged_server):
    """Why the new check is not redundant.

    Both sockets are bound and accepting. `ss -tln | grep :port` — and any
    equivalent connect-check — passes for both. The old check had no way to see
    the difference, which is why an 18-minute outage read as green.
    """
    assert _port_is_listening(answering_server)
    assert _port_is_listening(wedged_server)
    assert _probe(answering_server) != _probe(wedged_server)


def test_box_health_runs_this_probe_and_keys_on_000():
    """Pins the integration: the predicate above must be the one in the script."""
    assert "HTTP_PROBE_TIMEOUT" in BOX_HEALTH
    assert "%{http_code}" in BOX_HEALTH
    assert re.search(r'\[ "\$code" = "000" \]', BOX_HEALTH), (
        "box_health.sh no longer treats an absent status line as the outage "
        "condition — the probe would then page on 404s from healthy services"
    )
    assert "SERVICE_PORT" in BOX_HEALTH


def test_the_problem_line_carries_no_moving_number():
    """LOAD-BEARING: snapshot_problems confirms by exact line intersection over
    RETRY_ATTEMPTS samples. A line carrying a latency, a status code, or any
    other per-sample value can never confirm, so the check would stay silent
    with a perfectly working probe. Same trap that kept the memory-pressure
    line silent even after its parser was fixed."""
    emitted = re.findall(r'echo "(service not answering HTTP[^"]*)"', BOX_HEALTH)
    assert emitted, "expected a `service not answering HTTP ...` problem line"
    for line in emitted:
        # $unit and the constant timeout are stable across samples; a status
        # code or elapsed time would not be.
        assert "$code" not in line, line
        assert "time_total" not in line, line


def test_missing_map_is_reported_rather_than_silently_uncovered():
    """A manifest from the older generator has no SERVICE_PORT. Covering
    nothing while appearing to run is the exact class this check exists for."""
    assert "HTTP liveness is UNMONITORED" in BOX_HEALTH


def test_generator_emits_a_pairing_for_every_ported_service(tmp_path):
    """The map must be explicit. SERVICES and PORTS are two independent lists
    (`ports` skips `port: none`), so pairing them by index would name the wrong
    service in an alert."""
    out = subprocess.run(
        ["python3", str(GENERATOR), "--stdout"],
        capture_output=True, text=True, check=True,
    ).stdout
    # Only the SERVICE_PORT block — the manifest also carries
    # TIMER_MAX_STALENESS, whose entries share this literal shape.
    block = re.search(r"declare -A SERVICE_PORT=\((.*?)\n\)", out, re.S).group(1)
    pairs = dict(re.findall(r'^\s*\["([^"]+)"\]=(\S+)$', block, re.M))
    expected = {
        s["unit"]: str(s["port"])
        for s in BUDGET["services"]
        if str(s.get("port")) != "none"
    }
    assert pairs == expected


def test_fallback_arrays_still_describe_the_real_box():
    """box_health.sh's hardcoded fallback (used when the manifest is missing)
    is hand-maintained, and a hand-maintained copy of a generated list is what
    the manifest was introduced to end: on 2026-07-27 the installed list had 8
    services, git had 5, and neither covered nginx. The fallback survived that
    change unguarded — this pins it to budget.yaml as SETS, since the fallback
    is deliberately not index-paired and nothing pairs it."""
    services = set(re.search(r"SERVICES=\(([^)]*)\)", BOX_HEALTH).group(1).split())
    ports = set(re.search(r"PORTS=\(([^)]*)\)", BOX_HEALTH).group(1).split())
    services.discard("\\")  # line continuations inside the array literal
    declared_services = {s["unit"] for s in BUDGET["services"]}
    declared_ports = {
        str(s["port"]) for s in BUDGET["services"] if str(s.get("port")) != "none"
    }
    assert services == declared_services
    assert ports == declared_ports


def _bash4() -> str:
    """A bash with associative arrays.

    The box runs bash 5 and so does CI; macOS ships 3.2, where `declare -A`
    fails and every one of these tests would pass VACUOUSLY — an empty map,
    no probes, no output. Resolving an interpreter explicitly (and skipping
    loudly when there is none) is the difference between running the shipped
    code and appearing to.
    """
    for candidate in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"):
        if not Path(candidate).exists():
            continue
        ver = subprocess.run([candidate, "-c", "echo ${BASH_VERSINFO[0]}"],
                             capture_output=True, text=True).stdout.strip()
        if ver.isdigit() and int(ver) >= 4:
            return candidate
    pytest.skip("no bash >= 4 available; `declare -A` would fail vacuously")


def _run_shipped_function(service_port: dict[str, int], manifest_ok: int = 1) -> str:
    """Extract `http_liveness_problems` from box_health.sh and RUN it.

    Not a reimplementation of the loop — the real one, lifted by name. Half the
    guards this fleet has shipped were correct in both halves and wrong in the
    pairing; a loop proven only by reading it is a loop nobody has run.
    """
    entries = " ".join(f'["{u}"]={p}' for u, p in service_port.items())
    script = (
        f'set -u\n'
        f'MANIFEST_OK={manifest_ok}\n'
        f'HTTP_PROBE_TIMEOUT={PROBE_TIMEOUT}\n'
        f'declare -A SERVICE_PORT=({entries})\n'
        f'source <(sed -n "/^http_liveness_problems() {{/,/^}}/p" '
        f'  "{REPO_ROOT / "infrastructure" / "box_health.sh"}")\n'
        f'http_liveness_problems\n'
    )
    return subprocess.run(
        [_bash4(), "-c", script], capture_output=True, text=True
    ).stdout


def test_shipped_function_names_the_wedged_service_and_only_that_one(
    answering_server, wedged_server
):
    out = _run_shipped_function(
        {"healthy.service": answering_server, "wedged.service": wedged_server}
    )
    assert "wedged.service" in out
    assert "healthy.service" not in out, (
        "a service answering 404 was reported as not answering — the probe is "
        "keying on the status code rather than on whether one arrived"
    )


def test_shipped_function_is_silent_when_everything_answers(answering_server):
    assert _run_shipped_function({"healthy.service": answering_server}).strip() == ""


def test_shipped_function_reports_an_empty_map_instead_of_covering_nothing():
    out = _run_shipped_function({})
    assert "UNMONITORED" in out


def test_shipped_function_does_nothing_without_a_manifest():
    """MANIFEST_OK=0 is already reported loudly by the caller; probing an
    unpaired list would name the wrong service."""
    assert _run_shipped_function({}, manifest_ok=0).strip() == ""
