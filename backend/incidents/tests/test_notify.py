"""Notification seam (ADR-013): ntfy provider header protocol + best-effort (never raises)."""
import pytest

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
    assert ok
    assert err == ""
    assert cap["url"] == "https://ntfy.sh/watch-local-tier-T2"
    assert cap["data"] == b"hello"
    assert cap["headers"]["Title"] == "the title"
    assert cap["headers"]["Priority"] == "high"
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
    assert not ok
    assert "ntfy down" in err


def test_notify_provider_override():
    calls = []

    class Fake:
        def send(self, page):
            calls.append(page.topic)

    notify.set_provider_for_tests(Fake())
    try:
        ok, err = notify.send("mytopic", "t", "m")
        assert ok
        assert calls == ["mytopic"]
    finally:
        notify.set_provider_for_tests(None)


# --- Grafana IRM formatted webhook (ADR-066) -----------------------------------------------------

ALERT = {"uid": "watch-prod-abc-T2", "tier": "T2", "incident": "INC-0042", "assignee": "t2a",
         "link": "https://watch.example/ui/incidents/abc/"}


@pytest.fixture
def irm_settings(settings):
    settings.PAGING_PROVIDER = "grafana_irm"
    settings.GRAFANA_IRM_WEBHOOK_URL = "https://irm.example/integrations/v1/formatted_webhook/tok/"
    settings.PAGING_ENV = "prod"
    return settings


def _capture(monkeypatch):
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append({"url": url, "json": json})
        return Resp()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    return posts


def test_irm_page_is_a_formatted_webhook_alert(irm_settings, monkeypatch):
    posts = _capture(monkeypatch)
    assert notify.provider_name() == "grafana_irm"
    assert notify.send("ignored", "[T2] disk full", "body", alert=ALERT) == (True, "")
    [post] = posts
    assert post["url"] == irm_settings.GRAFANA_IRM_WEBHOOK_URL
    body = post["json"]
    assert (body["alert_uid"], body["state"], body["title"]) == ("watch-prod-abc-T2", "alerting", "[T2] disk full")
    assert (body["tier"], body["incident"], body["assignee"], body["environment"]) == ("T2", "INC-0042", "t2a", "prod")
    assert body["link_to_upstream_details"] == ALERT["link"]


def test_irm_clear_resolves_the_same_alert(irm_settings, monkeypatch):
    posts = _capture(monkeypatch)
    assert notify.clear(ALERT, "[T2] disk full") == (True, "")
    assert (posts[0]["json"]["alert_uid"], posts[0]["json"]["state"]) == ("watch-prod-abc-T2", "ok")


def test_irm_without_an_alert_or_url_fails_softly(irm_settings, monkeypatch):
    _capture(monkeypatch)
    ok, err = notify.send("t", "ti", "m")
    assert not ok
    assert "needs an alert" in err
    irm_settings.GRAFANA_IRM_WEBHOOK_URL = ""
    ok, err = notify.clear(ALERT, "ti")
    assert not ok
    assert "GRAFANA_IRM_WEBHOOK_URL" in err


def test_unknown_provider_fails_softly(settings):
    settings.PAGING_PROVIDER = "pagerduty"
    ok, err = notify.send("t", "ti", "m")
    assert not ok
    assert "unknown PAGING_PROVIDER" in err
    assert notify.provider_name() == "custom"


def test_ntfy_clear_is_a_no_op(settings):
    settings.PAGING_PROVIDER = "ntfy"
    assert notify.clear(ALERT, "ti") == (True, "")
