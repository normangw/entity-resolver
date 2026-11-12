import http.server
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import monitor  # noqa: E402


def serve(answers: dict[str, tuple[int, dict]]):
    """A throwaway local server; ``answers`` maps path (or path+name) to a reply."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._reply(*answers[self.path])

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            name = json.loads(self.rfile.read(size))["company_name"]
            self._reply(*answers[f"{self.path}:{name}"])

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


HEALTHY = {
    "/health": (200, {"status": "ok", "build": "abc1234"}),
    "/ready": (200, {"status": "ready"}),
    "/resolve:Lyft": (200, {"status": "resolved"}),
    "/resolve:Zyxwv Quantum Bakery": (200, {"status": "unresolved"}),
}


def test_a_healthy_service_passes_and_reports_the_build():
    server, url = serve(HEALTHY)
    try:
        failures, warnings, info = monitor.check(url, 4, 1000, 3000)
    finally:
        server.shutdown()
    assert failures == [] and warnings == []
    assert any("abc1234" in line for line in info)
    assert any("p95" in line for line in info)


def test_a_wrong_answer_is_a_failure():
    wrong = {**HEALTHY, "/resolve:Zyxwv Quantum Bakery": (200, {"status": "resolved"})}
    server, url = serve(wrong)
    try:
        failures, _, _ = monitor.check(url, 4, 1000, 3000)
    finally:
        server.shutdown()
    assert any("expected unresolved" in f for f in failures)


def test_not_ready_is_a_failure_and_wikidata_busy_is_only_a_warning():
    server, url = serve({**HEALTHY, "/ready": (503, {"status": "not ready"})})
    try:
        failures, _, _ = monitor.check(url, 2, 1000, 3000)
    finally:
        server.shutdown()
    assert any("not ready" in f for f in failures)

    busy = {**HEALTHY, "/resolve:Lyft": (503, {"detail": "Wikidata is unavailable"})}
    server, url = serve(busy)
    try:
        failures, warnings, _ = monitor.check(url, 4, 1000, 3000)
    finally:
        server.shutdown()
    assert failures == []
    assert any("503" in w for w in warnings)


def test_an_unreachable_service_is_a_failure():
    failures, _, _ = monitor.check("http://127.0.0.1:1", 2, 1000, 3000)
    assert failures and "cannot reach" in failures[0]
