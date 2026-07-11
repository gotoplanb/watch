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
    assert "rounded-full" in html and "bg-rose-500/15" in html  # squall -> crit badge (Tailwind, ADR-039)
    assert "SPECIAL" in html and "ROUTINE" in html             # digest badges
    assert "System Health" in html and "INCIDENT digest" in html


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
