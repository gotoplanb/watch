"""Security response headers (platform#37) — CSP, Permissions-Policy, X-Frame-Options,
nosniff, referrer-policy present on responses (surfaced by the #32 DAST scan)."""

from django.test import TestCase


class SecurityHeadersTests(TestCase):
    def test_headers_present_on_landing(self):
        resp = self.client.get("/")
        self.assertEqual(resp["X-Frame-Options"], "DENY")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertIn("default-src 'self'", resp["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", resp["Content-Security-Policy"])
        self.assertIn("camera=()", resp["Permissions-Policy"])

    def test_csp_allows_ui_cdns(self):
        # The /ui depends on Tailwind Play + Alpine/HTMX (unpkg) — the CSP must permit them
        # (and unsafe-eval) or the UI breaks.
        csp = self.client.get("/")["Content-Security-Policy"]
        self.assertIn("https://cdn.tailwindcss.com", csp)
        self.assertIn("https://unpkg.com", csp)
        self.assertIn("'unsafe-eval'", csp)
