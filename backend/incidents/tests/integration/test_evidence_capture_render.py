"""A real headless-Chromium render (ADR-060): the screenshot is a JPEG, and the page can't reach an
internal address. Skips where Chromium isn't installed."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from incidents import evidence_capture

pytestmark = pytest.mark.integration


@pytest.fixture
def internal_server():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>internal</h1>")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", hits
    server.shutdown()


def _render(url):
    try:
        return evidence_capture.render(url)
    except Exception as exc:  # noqa: BLE001
        if "Executable doesn't exist" in str(exc):
            pytest.skip(f"Chromium unavailable: {exc}")
        raise


def test_render_returns_a_jpeg():
    image, final_url = _render("data:text/html,<h1>Square: degraded payments</h1>")
    assert image.startswith(b"\xff\xd8\xff")
    assert final_url.startswith("data:text/html")


def test_internal_addresses_are_never_contacted(internal_server, monkeypatch):
    base, hits = internal_server
    with pytest.raises(Exception, match="net::ERR_FAILED"):
        _render(f"{base}/latest/meta-data")
    assert hits == []
    # Control: without the guard the same navigation does reach the server, so the check above
    # is the guard's doing, not something else in Chromium.
    monkeypatch.setattr(evidence_capture, "_guard", lambda route: route.continue_())
    _render(f"{base}/latest/meta-data")
    assert hits == ["/latest/meta-data"]
