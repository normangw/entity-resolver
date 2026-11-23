import http.server
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import monitor  # noqa: E402


def serve(answers: dict):
    """A throwaway local server; ``answers`` maps path (or path+name) to a reply.

    A reply may also be a list of replies, given in turn (the last one repeats).
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def _reply(self, reply) -> None:
            if isinstance(reply, list):
                reply = reply.pop(0) if len(reply) > 1 else reply[0]
            status, body = reply
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._reply(answers[self.path])

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            name = json.loads(self.rfile.read(size))["company_name"]
            self._reply(answers[f"{self.path}:{name}"])

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
    assert any("status expected 'unresolved'" in f for f in failures)


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


def test_smoke_cases_check_every_expected_field(tmp_path):
    cases = tmp_path / "smoke.jsonl"
    cases.write_text(
        "# a comment\n"
        '{"company_name": "Lyft", "status": "resolved", "parent_company": null}\n'
        "\n"
        '{"company_name": "Zyxwv Quantum Bakery", "status": "unresolved"}\n'
    )
    loaded = monitor.load_cases(str(cases))
    assert [c["company_name"] for c in loaded] == ["Lyft", "Zyxwv Quantum Bakery"]

    server, url = serve(HEALTHY)
    try:
        failures, _, _ = monitor.check(url, 0, 1000, 3000, cases=loaded)
        assert failures == []
        wrong = [{"company_name": "Lyft", "status": "resolved", "parent_company": "X"}]
        failures, _, _ = monitor.check(url, 0, 1000, 3000, cases=wrong)
    finally:
        server.shutdown()
    assert failures == ["'Lyft': parent_company expected 'X', got None"]


def test_a_smoke_case_needs_an_expectation(tmp_path):
    bad = tmp_path / "smoke.jsonl"
    bad.write_text('{"company_name": "Lyft"}\n')
    try:
        monitor.load_cases(str(bad))
    except ValueError as exc:
        assert "expectation" in str(exc)
    else:
        raise AssertionError("a case with nothing to check was accepted")


def test_a_wrong_version_is_a_failure():
    server, url = serve(
        {**HEALTHY, "/health": (200, {"status": "ok", "version": "0.1.0"})}
    )
    try:
        failures, _, _ = monitor.check(url, 2, 1000, 3000, expect_version="0.2.0")
    finally:
        server.shutdown()
    assert any("expected 0.2.0" in f for f in failures)


def test_the_seed_smoke_file_is_valid():
    path = Path(__file__).parents[1] / "eval" / "smoke-seed.jsonl"
    assert len(monitor.load_cases(str(path))) >= 5


BUSY = (503, {"detail": "Wikidata is unavailable"})
LOCAL_CASE = {"company_name": "Lyft", "status": "resolved", "source": "local"}
WIKIDATA_CASE = {"company_name": "YouTube", "status": "resolved"}
SMOKE_ANSWERS = {
    **HEALTHY,
    "/resolve:Lyft": (200, {"status": "resolved", "source": "local"}),
    "/resolve:YouTube": (200, {"status": "resolved", "source": "wikidata"}),
}


def smoke(answers, cases):
    server, url = serve(answers)
    try:
        return monitor.check(url, 0, 1000, 3000, cases=cases, busy_retry_seconds=0)
    finally:
        server.shutdown()


def test_smoke_a_busy_wikidata_answer_is_retried_then_a_warning():
    answers = {**SMOKE_ANSWERS, "/resolve:YouTube": BUSY}
    failures, warnings, _ = smoke(answers, [LOCAL_CASE, WIKIDATA_CASE])
    assert failures == []
    assert warnings == ["'YouTube': 503 twice (Wikidata busy?)"]


def test_smoke_a_busy_answer_that_recovers_on_retry_passes_cleanly():
    ok = SMOKE_ANSWERS["/resolve:YouTube"]
    answers = {**SMOKE_ANSWERS, "/resolve:YouTube": [BUSY, ok]}
    failures, warnings, _ = smoke(answers, [LOCAL_CASE, WIKIDATA_CASE])
    assert failures == [] and warnings == []


def test_smoke_a_busy_local_answer_is_a_failure():
    answers = {**SMOKE_ANSWERS, "/resolve:Lyft": BUSY}
    failures, _, _ = smoke(answers, [LOCAL_CASE, WIKIDATA_CASE])
    assert failures == ["'Lyft': 503 on a local-index answer"]


def test_smoke_a_wrong_wikidata_answer_is_still_a_failure():
    answers = {**SMOKE_ANSWERS, "/resolve:YouTube": (200, {"status": "unresolved"})}
    failures, _, _ = smoke(answers, [LOCAL_CASE, WIKIDATA_CASE])
    assert failures == ["'YouTube': status expected 'resolved', got 'unresolved'"]


def test_warnings_use_the_warn_exit_code(tmp_path, monkeypatch, capsys):
    cases = tmp_path / "smoke.jsonl"
    cases.write_text(f"{json.dumps(LOCAL_CASE)}\n{json.dumps(WIKIDATA_CASE)}\n")
    server, url = serve({**SMOKE_ANSWERS, "/resolve:YouTube": BUSY})
    argv = ["monitor.py", url, "--smoke", str(cases), "--busy-retry-seconds", "0"]
    try:
        monkeypatch.setattr(sys, "argv", argv)
        assert monitor.main() == 0
        monkeypatch.setattr(sys, "argv", [*argv, "--warn-exit-code", "3"])
        assert monitor.main() == 3
    finally:
        server.shutdown()
    assert "ok, with warnings" in capsys.readouterr().out
