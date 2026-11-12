"""Human actions store the acting user's pk as the Transition actor (it flows through SendTaskSuccess
and the commit Lambda, and survives a rename); every surface that shows it resolves it to a username."""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from incidents import mcp_server, services
from incidents.models import Incident, Tier, Transition


def _incident():
    return Incident.objects.create(source="test", payload={}, title="disk full",
                                   dedupe_key=f"al-{Incident.objects.count()}", current_tier=Tier.T1)


@pytest.mark.django_db
def test_actor_labels_resolve_pks_and_pass_everything_else_through():
    u = User.objects.create(username="t1a")
    labels = services.actor_labels([str(u.pk), "system:auto-escalation", "99999", None])
    assert labels[str(u.pk)] == "t1a"
    assert labels["system:auto-escalation"] == "system:auto-escalation"
    assert labels["99999"] == "99999"          # unknown pk: shown as-is, never an error
    assert None not in labels
    assert services.actor_label(u.pk) == "t1a"


@pytest.mark.django_db
def test_timeline_and_rca_show_the_username_not_the_pk():
    u = User.objects.create(username="t1a")
    incident = _incident()
    services.acknowledge(incident.id, actor=str(u.pk))
    items = [i for i in services.timeline(incident) if i["kind"] == "transition"]
    assert items[0]["obj"].actor_label == "t1a"
    assert "actor `t1a`" in services.rca_markdown(incident)


@pytest.mark.django_db
def test_detail_page_shows_the_username():
    u = User.objects.create(username="t1a")
    u.groups.create(name="T1")
    incident = _incident()
    services.acknowledge(incident.id, actor=str(u.pk))
    client = Client()
    client.force_login(u)
    body = client.get(f"/ui/incidents/{incident.id}/").content.decode()
    assert "· t1a ·" in body
    assert f"· {u.pk} ·" not in body


@pytest.mark.django_db
def test_mcp_transitions_carry_actor_name():
    u = User.objects.create(username="t1a")
    incident = _incident()
    services.acknowledge(incident.id, actor=str(u.pk))
    token = mcp_server._principal.set(u.pk)     # what the MCP auth gate does per request
    try:
        row = mcp_server.get_incident(incident.number)["transitions"][0]
    finally:
        mcp_server._principal.reset(token)
    assert row["actor"] == str(u.pk)
    assert row["actor_name"] == "t1a"
    assert Transition.objects.filter(incident=incident).count() == 1
