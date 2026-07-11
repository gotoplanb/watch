from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from incidents import oauth_views

urlpatterns = [
    # Simple root landing (not a 404) pointing at the working surfaces.
    path("", TemplateView.as_view(template_name="landing.html"), name="landing"),
    path("admin/", admin.site.urls),
    path("api/", include("incidents.urls")),
    # Server-rendered incident-management UI (ADR-011): HTMX + Alpine + Tailwind.
    path("ui/", include("incidents.ui_urls")),
    # Session login/logout for the DRF browsable API + the UI (@login_required).
    path("api-auth/", include("rest_framework.urls")),
    # OAuth 2.0 authorization server for the MCP resource (ADR-038): discovery + authorize + token.
    path(".well-known/oauth-authorization-server",
         oauth_views.authorization_server_discovery, name="oauth_as_metadata"),
    path(".well-known/oauth-protected-resource",
         oauth_views.protected_resource_discovery, name="oauth_pr_metadata"),
    path("oauth/authorize", oauth_views.authorize, name="oauth_authorize"),
    path("oauth/token", oauth_views.token, name="oauth_token"),
]
