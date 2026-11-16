"""wt against a fake Watch server: the right paths, headers and bodies go out, and replies print."""
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wt import cli, config

INCIDENT = {"id": "u-1", "number": "INC-0042", "status": "OPEN", "current_tier": "T1",
            "acknowledged_at": None, "title": "cpu high", "sla_deadline_at": None,
            "transitions": [{"at": "2026-11-16T10:00:00Z", "from_status": "OPEN", "from_tier": "T1",
                             "to_status": "OPEN", "to_tier": "T2", "actor": "7", "reason": "r"}]}
ASSESSMENT = {"id": "a-1", "number": "ASM-0007", "status": "investigating", "title": "latency",
              "incident": None, "verdict": "", "confidence": 0.5, "created": True}


class Fake:
    def __init__(self):
        self.calls = []
        self.replies = {}

    def reply(self, method, path, status=200, body=None):
        self.replies[(method, path)] = (status, body)


@pytest.fixture
def server(tmp_path, monkeypatch):
    fake = Fake()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            fake.calls.append((self.command, self.path, dict(self.headers), body))
            status, reply = fake.replies.get((self.command, self.path), (404, {"detail": "nope"}))
            payload = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _serve

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    monkeypatch.setenv("WT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("WT_SERVER", url)
    monkeypatch.delenv("WT_AS", raising=False)
    monkeypatch.delenv("WT_WEBHOOK_SECRET", raising=False)
    config.set_key(url, "t1a", "wm_1_abc")
    fake.url = url
    yield fake
    httpd.shutdown()


def test_whoami_sends_the_users_key(server, capsys):
    server.reply("GET", "/api/me", body={"id": 1, "username": "t1a", "tiers": ["T1"],
                                         "is_staff": False})
    assert cli.main(["--as", "t1a", "whoami"]) == 0
    assert server.calls[0][2]["Authorization"] == "Bearer wm_1_abc"
    assert "t1a (id 1) tiers: T1" in capsys.readouterr().out


def test_wt_as_env_and_missing_user(server, monkeypatch):
    with pytest.raises(SystemExit, match="say who to act as"):
        cli.main(["whoami"])
    with pytest.raises(SystemExit, match="no key for 'ghost'"):
        cli.main(["--as", "ghost", "whoami"])
    monkeypatch.setenv("WT_AS", "t1a")
    server.reply("GET", "/api/me", body={"id": 1, "username": "t1a", "tiers": [],
                                         "is_staff": True})
    assert cli.main(["whoami"]) == 0


def test_incidents_filters_and_listing(server, capsys):
    server.reply("GET", "/api/incidents/?status=OPEN&current_tier=T2", body=[INCIDENT])
    server.reply("GET", "/api/incidents/?", body=[])
    cli.main(["--as", "t1a", "incidents", "--tier", "T2"])
    cli.main(["--as", "t1a", "incidents", "--status", "all"])
    out = capsys.readouterr().out
    assert "INC-0042  OPEN     T1" in out
    assert "No incidents." in out


def test_action_posts_reason_and_expected_tier(server, capsys):
    escalated = {**INCIDENT, "current_tier": "T2"}
    server.reply("POST", "/api/incidents/INC-0042/escalate/", body=escalated)
    argv = ["--as", "t1a", "escalate", "INC-0042", "--expect", "t1", "--reason", "why"]
    assert cli.main(argv) == 0
    assert server.calls[0][3] == {"reason": "why", "expected_tier": "T1"}
    assert "T2" in capsys.readouterr().out


def test_api_error_exits_1_with_detail(server, capsys):
    server.reply("POST", "/api/incidents/INC-0042/resolve/", 403, {"detail": "requires T2"})
    assert cli.main(["--as", "t1a", "resolve", "INC-0042"]) == 1
    assert "HTTP 403: requires T2" in capsys.readouterr().err


def test_intake_uses_webhook_secret_not_a_user(server, monkeypatch, capsys):
    monkeypatch.setenv("WT_WEBHOOK_SECRET", "s3cret")
    server.reply("POST", "/api/intake/webhook", 200, {**INCIDENT, "created": False})
    assert cli.main(["intake", "cpu high", "--event-id", "e1"]) == 0
    _, _, headers, body = server.calls[0]
    assert headers["X-Watch-Webhook-Secret"] == "s3cret"
    assert "Authorization" not in headers
    assert body["source_event_id"] == "e1"
    assert "(existing, deduped)" in capsys.readouterr().out


def test_webhook_secret_needed_off_local(server):
    with pytest.raises(SystemExit, match="WT_WEBHOOK_SECRET"):
        cli.main(["intake", "x"])


def test_assess_and_signal(server, monkeypatch, capsys):
    monkeypatch.setenv("WT_WEBHOOK_SECRET", "s")
    server.reply("POST", "/api/assessments/intake", 201, ASSESSMENT)
    server.reply("POST", "/api/signals/intake", 200,
                 {"assessments": [{**ASSESSMENT, "created": False}]})
    cli.main(["assess", "latency", "--signal", "p99", "--customer", "acme"])
    cli.main(["signal", "latency", "--signal", "p99"])
    assert server.calls[0][3]["customer"] == "acme"
    out = capsys.readouterr().out
    assert "ASM-0007  investigating latency" in out
    assert "(existing)" in out


def test_show_incident_and_assessment(server, capsys):
    server.reply("GET", "/api/incidents/INC-0042/", body=INCIDENT)
    server.reply("GET", "/api/assessments/ASM-0007/", body=ASSESSMENT)
    server.reply("GET", "/api/assessments/ASM-0007/history/", body=[
        {"occurred_at": "2026-11-16T10:00:00Z", "kind": "note", "actor": "t1a", "body": "a\nb"}])
    cli.main(["--as", "t1a", "show", "INC-0042"])
    cli.main(["--as", "t1a", "show", "asm-0007"])
    out = capsys.readouterr().out
    assert "OPEN/T1 -> OPEN/T2  by 7  r" in out
    assert "note" in out and "t1a: a" in out
    cli.main(["--as", "t1a", "--json", "show", "ASM-0007"])
    assert json.loads(capsys.readouterr().out)["history"][0]["kind"] == "note"


def test_wait_until_and_timeout(server, capsys):
    server.reply("GET", "/api/incidents/INC-0042/", body=INCIDENT)
    assert cli.main(["--as", "t1a", "wait", "INC-0042", "--tier", "T1", "--status", "open"]) == 0
    code = cli.main(["--as", "t1a", "wait", "INC-0042", "--acked", "--timeout", "0.05",
                     "--interval", "0.01"])
    assert code == 2
    assert "timed out" in capsys.readouterr().err


def test_api_raw(server, capsys):
    server.reply("POST", "/api/incidents/INC-0042/ack/", body={"ok": True})
    body = '{"reason":"x"}'
    cli.main(["--as", "t1a", "api", "-X", "post", "incidents/INC-0042/ack/", "-d", body])
    assert server.calls[0][3] == {"reason": "x"}
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_key_set_and_users(server, capsys):
    cli.main(["key", "set", "t2a", "wm_2_x"])
    cli.main(["users"])
    assert capsys.readouterr().out.splitlines()[-2:] == ["t1a", "t2a"]
    assert stat.S_IMODE(config.keys_path().stat().st_mode) == 0o600


def test_server_names(monkeypatch):
    monkeypatch.delenv("WT_SERVER", raising=False)
    assert config.server_url(None) == "http://localhost:8010"
    assert config.server_url("https://x/") == "https://x"
    with pytest.raises(SystemExit):
        config.server_url("bogus")
    assert config.webhook_secret("http://localhost:8010") == config.LOCAL_WEBHOOK_SECRET
