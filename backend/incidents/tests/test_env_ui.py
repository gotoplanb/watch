"""Env dashboard + schema-less status renderer (ADR-028): session-auth display, env switcher, digest
badges, and the generic JSON renderer proven against several unrelated shapes ('no rigid schema')."""
import pytest
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import Client

from incidents.models import Digest, EnvStatus


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
def test_env_dashboard_requires_login(client):
    r = client.get("/ui/environments/")
    assert r.status_code == 302 and "/api-auth/login/" in r["Location"]


@pytest.mark.django_db
def test_dashboard_renders_status_and_digests(client):
    EnvStatus.objects.create(environment="prod", payload={
        "worst_state": "squall",
        "services": [{"display_name": "Gibraltar", "state": "squall", "message": "5xx from Expedia"}],
    })
    Digest.objects.create(environment="prod", content="## System Health\nall good", title="hourly", special=False)
    Digest.objects.create(environment="prod", content="INCIDENT digest", special=True)
    client.force_login(User.objects.create(username="ops"))
    html = client.get("/ui/environments/?env=prod").content.decode()
    assert "Gibraltar" in html and "5xx from Expedia" in html
    assert "bg-orange-500" in html                             # squall dot (Tailwind, ADR-039)
    # ONE digest at a time now (ADR-043), newest first, with its badge — the rest are behind the pager
    assert "SPECIAL" in html and "INCIDENT digest" in html
    assert "ROUTINE" not in html and "System Health" not in html
    older = client.get("/ui/environments/?env=prod&d=1").content.decode()
    assert "ROUTINE" in older and "System Health" in older


@pytest.mark.django_db
def test_env_switcher_lists_all_envs(client):
    EnvStatus.objects.create(environment="prod", payload={"a": 1})
    Digest.objects.create(environment="nonprod", content="x")
    client.force_login(User.objects.create(username="ops2"))
    html = client.get("/ui/environments/").content.decode()
    assert "?env=prod" in html and "?env=nonprod" in html


@pytest.mark.django_db
def test_dashboard_empty_state_defaults_to_prod_nonprod(client):
    client.force_login(User.objects.create(username="ops3"))
    html = client.get("/ui/environments/").content.decode()
    assert "No status posted" in html and "?env=prod" in html and "?env=nonprod" in html


# --- the schema-less renderer, against unrelated shapes (ADR-028 guardrail) ------------------------
def _render(node):
    return render_to_string("incidents/_json_node.html", {"node": node, "label": "", "depth": 0})


def test_renderer_services_shape():
    out = _render({"services": [{"display_name": "Gib", "state": "calm", "url": "https://x/y"}]})
    assert "Gib" in out and "bg-emerald-500/15" in out and 'href="https://x/y"' in out


def test_renderer_totally_different_shape():
    out = _render({"teams": {"payments": {"health": "green"}}, "score": 97, "note": "ok"})
    assert "Payments" in out and "bg-emerald-500/15" in out and "97" in out  # no services-shaped assumptions


def test_renderer_scalars_bool_and_none():
    assert "yes" in _render({"enabled": True})
    assert "—" in _render({"missing": None})


def test_renderer_depth_cap_falls_back_to_raw():
    node = cur = {}
    for _ in range(12):
        cur["k"] = {}
        cur = cur["k"]
    cur["k"] = "deep"
    out = _render(node)
    assert "<pre" in out and "deep" in out  # past the depth cap -> raw JSON, never infinite recursion


# --- the view model behind the status page (ADR-043) -----------------------------------------------
from datetime import timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402

from incidents import envview  # noqa: E402


def _status(payload, minutes_ago=0):
    """An unsaved EnvStatus — summarize() only reads .payload and .created_at."""
    return EnvStatus(payload=payload, created_at=timezone.now() - timedelta(minutes=minutes_ago))


def test_service_shape_rows_are_sorted_worst_first():
    """The whole point of the list: the worst thing is where the eye already is."""
    view = envview.summarize(_status({
        "services": [
            {"display_name": "Search", "state": "serene", "message": "search: 5 errs"},
            {"display_name": "Payments", "state": "storm", "message": "payments: 344 errs"},
            {"display_name": "Platform", "state": "unsettled", "message": "platform: 60 errs"},
        ],
    }))
    assert [r["name"] for r in view["rows"]] == ["Payments", "Platform", "Search"]
    assert view["rows"][0]["dot"] == "bg-red-500"       # storm
    assert view["rows"][2]["dot"] == "bg-emerald-500"   # serene
    assert view["rows"][0]["message"] == "payments: 344 errs"


def test_declared_verdict_wins_over_the_rollup():
    """'Calm despite one degraded canary' is a judgment a sender is entitled to make."""
    view = envview.summarize(_status({
        "worst_state": "calm",
        "services": [{"display_name": "Canary", "state": "unsettled", "message": ""}],
    }))
    assert view["verdict"] == "calm" and view["verdict_dot"] == "bg-sky-500"


def test_verdict_rolls_up_from_the_worst_row_when_undeclared():
    view = envview.summarize(_status({
        "services": [
            {"display_name": "A", "state": "serene", "message": ""},
            {"display_name": "B", "state": "squall", "message": ""},
        ],
    }))
    assert view["verdict"] == "squall"


def test_arbitrary_shape_still_gets_rows(settings):
    """ADR-028's contract: the store never demanded a schema, so the screen can't either."""
    view = envview.summarize(_status({
        "regions": {"eu_west": {"health": "degraded", "latency_p99_ms": 880}},
        "deploys_today": 7,
    }))
    assert [r["name"] for r in view["rows"]] == ["regions"]  # a row per top-level object
    view2 = envview.summarize(_status({"eu_west": {"health": "degraded", "latency_p99_ms": 880}}))
    row = view2["rows"][0]
    assert row["name"] == "eu west" and row["dot"] == "bg-amber-400"
    assert row["message"] == "latency p99 ms 880"          # scalars summarized, state shown as a dot


def test_stale_status_is_unknown_not_reassuring_green():
    """A dead reporter must never render as healthy — the one lie a status page cannot tell."""
    view = envview.summarize(_status({"services": [{"display_name": "A", "state": "serene"}]},
                                     minutes_ago=90))
    assert view["stale"] is True and view["verdict"] == "unknown"


def test_a_snapshot_you_navigated_to_is_history_not_staleness():
    view = envview.summarize(_status({"worst_state": "storm"}, minutes_ago=90), historical=True)
    assert view["stale"] is False and view["historical"] is True and view["verdict"] == "storm"


def test_summarize_handles_no_status_at_all():
    view = envview.summarize(None)
    assert view["present"] is False and view["rows"] == []


def test_manual_status_is_badged_manual():
    assert envview.summarize(_status({"type": "manual"}))["scheduled"] is False
    assert envview.summarize(_status({}))["scheduled"] is True


# --- paging both histories (ADR-043) ---------------------------------------------------------------

@pytest.mark.django_db
def test_status_and_digest_histories_page_independently(client):
    for i in range(3):
        EnvStatus.objects.create(environment="prod", payload={"worst_state": f"s{i}", "n": i})
    for i in range(3):
        Digest.objects.create(environment="prod", content=f"digest {i}")
    client.force_login(User.objects.create(username="ops4"))

    html = client.get("/ui/environments/?env=prod").content.decode()
    assert "1 of 3" in html and "digest 2" in html          # newest of each by default

    # page the digest back one; the status snapshot must NOT move with it
    html = client.get("/ui/environments/?env=prod&d=1").content.decode()
    assert "digest 1" in html and "digest 2" not in html    # the digest moved…
    assert "1 of 3" in html and "2 of 3" in html            # …the snapshot stayed at 1 of 3

    # an out-of-range index clamps instead of exploding
    assert client.get("/ui/environments/?env=prod&s=99&d=99").status_code == 200
    assert client.get("/ui/environments/?env=prod&s=abc").status_code == 200


@pytest.mark.django_db
def test_pager_links_preserve_the_other_params(client):
    for i in range(2):
        EnvStatus.objects.create(environment="prod", payload={"n": i})
        Digest.objects.create(environment="prod", content=f"d{i}")
    client.force_login(User.objects.create(username="ops5"))
    html = client.get("/ui/environments/?env=prod&d=1").content.decode()
    # the status pager's Older link keeps env AND the digest index — paging one must not reset the other
    assert "env=prod" in html and "d=1" in html and "s=1" in html


@pytest.mark.django_db
def test_special_only_filter(client):
    Digest.objects.create(environment="prod", content="routine one", special=False)
    Digest.objects.create(environment="prod", content="the special one", special=True)
    client.force_login(User.objects.create(username="ops6"))
    html = client.get("/ui/environments/?env=prod&special=1").content.decode()
    assert "the special one" in html and "routine one" not in html


@pytest.mark.django_db
def test_historical_snapshot_says_so(client):
    old = EnvStatus.objects.create(environment="prod", payload={"worst_state": "storm"})
    EnvStatus.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=3))
    EnvStatus.objects.create(environment="prod", payload={"worst_state": "serene"})
    client.force_login(User.objects.create(username="ops7"))
    html = client.get("/ui/environments/?env=prod&s=1").content.decode()
    assert "Looking at the past" in html and "stale" not in html.lower()
