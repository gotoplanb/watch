"""Notification seam (ADR-013): ntfy provider header protocol + best-effort (never raises)."""
from incidents import notify


class Resp:
    def raise_for_status(self):
        pass


def test_ntfy_posts_with_headers_no_token(settings, monkeypatch):
    settings.NTFY_BASE_URL = "https://ntfy.sh/"
    settings.NTFY_TOKEN = ""
    cap = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        cap.update(url=url, data=data, headers=headers)
        return Resp()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    ok, err = notify.send("watch-local-tier-T2", "the title", "hello", priority="high", tags=["x", "y"])
    assert ok and err == ""
    assert cap["url"] == "https://ntfy.sh/watch-local-tier-T2"
    assert cap["data"] == b"hello"
    assert cap["headers"]["Title"] == "the title" and cap["headers"]["Priority"] == "high"
    assert cap["headers"]["Tags"] == "x,y"
    assert "Authorization" not in cap["headers"]


def test_ntfy_adds_bearer_token(settings, monkeypatch):
    settings.NTFY_BASE_URL = "https://ntfy.sh"
    settings.NTFY_TOKEN = "tk_secret"
    cap = {}
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, data=None, headers=None, timeout=None: (cap.update(headers=headers), Resp())[1])
    notify.send("t", "ti", "m")
    assert cap["headers"]["Authorization"] == "Bearer tk_secret"


def test_notify_best_effort_never_raises(monkeypatch):
    def boom(*a, **k):
        raise notify.requests.RequestException("ntfy down")
    monkeypatch.setattr(notify.requests, "post", boom)
    ok, err = notify.send("t", "ti", "m")
    assert not ok and "ntfy down" in err


def test_notify_provider_override():
    calls = []

    class Fake:
        def send(self, topic, title, message, priority="default", tags=None):
            calls.append(topic)

    notify.set_provider_for_tests(Fake())
    try:
        ok, err = notify.send("mytopic", "t", "m")
        assert ok and calls == ["mytopic"]
    finally:
        notify.set_provider_for_tests(None)
