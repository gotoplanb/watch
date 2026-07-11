"""
Run the Watch MCP server (ADR-038) — a separate process from the same image, the
ADR-025/032 same-image-different-command pattern:

    python manage.py mcp            # http://localhost:8011/mcp

Auth: `Authorization: Bearer <wm_… MCP key>` (from /ui/settings) or an OAuth access token
(claude.ai connectors — the AS lives on the Django app). Claude Code:

    claude mcp add --transport http watch http://localhost:8011/mcp \
      --header "Authorization: Bearer wm_…"
"""
import uvicorn
from django.core.management.base import BaseCommand

from incidents.mcp_server import build_mcp_app


class Command(BaseCommand):
    help = "Serve the Watch MCP server (FastMCP, streamable HTTP) on the given port."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=8011)

    def handle(self, *args, **opts):
        self.stdout.write(f"Watch MCP: http://{opts['host']}:{opts['port']}/mcp")
        uvicorn.run(build_mcp_app(), host=opts["host"], port=opts["port"], log_level="info")
