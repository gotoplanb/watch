"""
Extra security response headers (platform#37): Content-Security-Policy + Permissions-Policy,
which Django's SecurityMiddleware doesn't emit. Surfaced by the #32 DAST scan. Values are
settings so they can be tuned without code changes; applied to every response.
"""

from django.conf import settings
from django.utils.deprecation import MiddlewareMixin


class SecurityHeadersMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        csp = getattr(settings, "CONTENT_SECURITY_POLICY", "")
        if csp and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = csp
        pp = getattr(settings, "PERMISSIONS_POLICY", "")
        if pp and "Permissions-Policy" not in response:
            response["Permissions-Policy"] = pp
        return response
