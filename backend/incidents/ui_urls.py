from django.urls import path

from . import ui_views

app_name = "ui"

urlpatterns = [
    path("incidents/", ui_views.incident_list, name="incident_list"),
    path("incidents/<uuid:pk>/", ui_views.incident_detail, name="incident_detail"),
    path("incidents/<uuid:pk>/note/", ui_views.add_note, name="add_note"),
    path("incidents/<uuid:pk>/annotate/", ui_views.annotate, name="annotate"),
    path("incidents/<uuid:pk>/rca.md", ui_views.rca, name="rca"),
    path("incidents/<uuid:pk>/ack/", ui_views.act, {"action": "ack"}, name="ack"),
    path("incidents/<uuid:pk>/escalate/", ui_views.act, {"action": "escalate"}, name="escalate"),
    path("incidents/<uuid:pk>/resolve/", ui_views.act, {"action": "resolve"}, name="resolve"),
    path("schedule/", ui_views.schedule, name="schedule"),
    path("schedule/shift/", ui_views.add_shift, name="add_shift"),
]
