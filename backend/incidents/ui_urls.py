from django.urls import path

from . import ui_views

app_name = "ui"

urlpatterns = [
    path("incidents/", ui_views.incident_list, name="incident_list"),
    path("incidents/<uuid:pk>/", ui_views.incident_detail, name="incident_detail"),
    path("incidents/<uuid:pk>/comment/", ui_views.add_comment, name="add_comment"),
    path("incidents/<uuid:pk>/ack/", ui_views.act, {"action": "ack"}, name="ack"),
    path("incidents/<uuid:pk>/escalate/", ui_views.act, {"action": "escalate"}, name="escalate"),
    path("incidents/<uuid:pk>/resolve/", ui_views.act, {"action": "resolve"}, name="resolve"),
]
