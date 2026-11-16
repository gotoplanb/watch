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
            raw = self.rfile.read(length) if length else b""
            is_json = self.headers.get("Content-Type", "").startswith("application/json")
            body = json.loads(raw) if raw and is_json else (raw or None)
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
        {"id": 9, "occurred_at": "2026-11-16T10:00:00Z", "kind": "note", "actor": "t1a",
         "body": "a\nb"}])
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


# --- workspace commands ---------------------------------------------------------------------------

RESULT = {"assessment": "ASM-0007", "status": "promoted", "incident": "INC-0042",
          "events": [{"id": 5, "kind": "incident_promoted", "actor": "t1a", "actor_name": "t1a",
                      "body": "Promoted", "occurred_at": "2026-11-16T10:00:00Z"}]}
COMM = {"id": "c-1", "state": "review", "revision": 2, "subject": "Update", "body": "Hello",
        "recipients": ["ops@example.test"]}


def test_pairs_parse_json_lists_and_files():
    fields, files = cli.pairs(["a=1", "b=true", "c=hello world", "s=1", "s=2", "f=@x.png"])
    assert fields == {"a": 1, "b": True, "c": "hello world", "s": [1, 2]}
    assert files == {"f": ["x.png"]}
    with pytest.raises(SystemExit, match="key=value"):
        cli.pairs(["oops"])


def test_do_posts_fields_and_prints_new_events(server, capsys):
    server.reply("POST", "/api/assessments/ASM-0007/actions/promote/", body=RESULT)
    assert cli.main(["--as", "t1a", "do", "asm-0007", "promote", "reason=impact"]) == 0
    assert server.calls[0][3] == {"reason": "impact"}
    out = capsys.readouterr().out
    assert "#5" in out and "incident_promoted" in out
    assert "ASM-0007 promoted -> INC-0042" in out


def test_note_uploads_photos_as_multipart(server, tmp_path):
    photo = tmp_path / "shot.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\nexample")
    path = "/api/assessments/ASM-0007/actions/comment/"
    server.reply("POST", path, body={**RESULT, "events": []})
    assert cli.main(["--as", "t1a", "note", "ASM-0007", "look", "--photo", str(photo)]) == 0
    _, _, headers, body = server.calls[0]
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="photos"; filename="shot.png"' in body and b"look" in body


def test_new_assessment(server, capsys):
    server.reply("POST", "/api/assessments/", 201, ASSESSMENT)
    cli.main(["--as", "t1a", "new", "latency", "--signal", "p99", "--customer", "acme"])
    assert server.calls[0][3]["customer"] == "acme"
    assert "ASM-0007" in capsys.readouterr().out


def test_comm_show_and_send_with_current_version(server, capsys):
    server.reply("GET", "/api/communications/c-1/", body=COMM)
    server.reply("POST", "/api/communications/c-1/send/", body={**COMM, "state": "sent"})
    cli.main(["--as", "t1a", "comm", "c-1"])
    cli.main(["--as", "t1a", "comm", "c-1", "send"])
    assert server.calls[-1][3] == {"expected_version": 2}
    out = capsys.readouterr().out
    assert "Hello" in out and "sent" in out


def test_lists_and_rca(server, capsys):
    server.reply("GET", "/api/assessments/ASM-0007/communications/", body=[COMM])
    server.reply("GET", "/api/assessments/ASM-0007/engagements/", body=[])
    server.reply("GET", "/api/assessments/ASM-0007/rca/", body={"markdown": "# RCA — ASM-0007"})
    server.reply("GET", "/api/subject-areas/", body=[{"id": 1, "name": "Pay", "members": ["t3a"]}])
    for argv in (["comms", "ASM-0007"], ["sme", "ASM-0007"], ["rca", "ASM-0007"], ["areas"]):
        assert cli.main(["--as", "t1a", *argv]) == 0
    out = capsys.readouterr().out
    assert "c-1  review" in out and "No specialist engagements." in out
    assert "# RCA — ASM-0007" in out and "Pay  (t3a)" in out


def test_sme_respond_remediation_and_report(server, capsys):
    acked = {"id": "g-1", "state": "acknowledged"}
    server.reply("POST", "/api/engagements/g-1/respond/", body=acked)
    server.reply("POST", "/api/remediations/r-1/", body={"id": "r-1", "state": "done",
                                                         "title": "x"})
    server.reply("GET", "/api/reports/?days=7", body={"days": 7})
    config.set_key(server.url, "t3a", "wm_3_x")
    cli.main(["--as", "t3a", "sme-respond", "g-1", "ack"])
    cli.main(["--as", "t3a", "remediation", "r-1", "state=done"])
    cli.main(["--as", "t3a", "report", "--days", "7"])
    assert server.calls[0][3] == {"action": "ack", "body": ""}
    assert server.calls[1][3] == {"state": "done"}
    out = capsys.readouterr().out
    assert "acknowledged" in out and '"days": 7' in out


def test_error_lists_form_fields_and_html_titles():
    from wt.http import ApiError
    err = ApiError(400, {"detail": "Check the fields.",
                         "errors": {"verdict": [{"message": "Not a choice."}]}})
    assert "verdict: Not a choice." in str(err)
    assert str(ApiError(404, "<html><title>Page not found</title></html>")) == \
        "HTTP 404: Page not found"


def test_email_in_posts_the_raw_message(server, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WT_WEBHOOK_SECRET", "s")
    eml = tmp_path / "m.eml"
    eml.write_bytes(b"From: a@b.example\r\nSubject: hi\r\n\r\nbody")
    server.reply("POST", "/api/email/inbound", 201, {"outcome": "signal", "reason": "new signal",
                                                     "assessments": [ASSESSMENT]})
    assert cli.main(["email-in", str(eml)]) == 0
    _, _, headers, body = server.calls[0]
    assert headers["Content-Type"] == "message/rfc822"
    assert body == eml.read_bytes()
    out = capsys.readouterr().out
    assert "signal: new signal" in out and "ASM-0007" in out
