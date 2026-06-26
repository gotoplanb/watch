from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("incidents.urls")),
    # Session login/logout for the DRF browsable API — lets you exercise the
    # ack/escalate/resolve endpoints by hand as a logged-in tier user (manual use).
    path("api-auth/", include("rest_framework.urls")),
]
