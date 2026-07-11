"""
Mint an OAuth client pair for an MCP connector (ADR-038) — manual registration, conduct-style
(no dynamic client registration). The secret is printed ONCE and stored only as a hash.

    python manage.py create_oauth_client --name "claude.ai" \
      --redirect-uri https://claude.ai/api/mcp/auth_callback
"""
from django.core.management.base import BaseCommand

from incidents import oauth


class Command(BaseCommand):
    help = "Create an OAuth client (prints client_id + secret once)."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--redirect-uri", action="append", required=True, dest="redirect_uris",
                            help="Exact allowed redirect URI (repeatable).")

    def handle(self, *args, **opts):
        client, raw_secret = oauth.create_client(opts["name"], opts["redirect_uris"])
        self.stdout.write(self.style.SUCCESS(f"client_id:     {client.client_id}"))
        self.stdout.write(self.style.SUCCESS(f"client_secret: {raw_secret}"))
        self.stdout.write("Store the secret now — only its hash is kept (rotating = new client).")
