from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("incidents.urls")),
    # Server-rendered incident-management UI (ADR-011): HTMX + Alpine + Tailwind.
    path("ui/", include("incidents.ui_urls")),
    # Session login/logout for the DRF browsable API + the UI (@login_required).
    path("api-auth/", include("rest_framework.urls")),
]
