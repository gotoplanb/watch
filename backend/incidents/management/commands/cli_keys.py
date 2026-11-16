"""Write each active user's `wm_` key for the `wt` CLI (ADR-056). Local bootstrap only: it reads
the database and API_KEY_SECRET directly, which is why `wt` itself never does.

    manage.py cli_keys --write ~/.config/wt/keys.json --server http://localhost:8010
"""
import json
import os
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from incidents import apikeys


def keys_by_user() -> dict[str, str]:
    return {u.username: apikeys.mcp_key_for(u)
            for u in User.objects.filter(is_active=True).order_by("username")}


def merge_into(path: Path, server: str, keys: dict[str, str]) -> None:
    """Replace this server's keys in the file, keep other servers', and leave it mode 600."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    data.setdefault("servers", {})[server] = keys
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


class Command(BaseCommand):
    help = "Write every active user's wm_ key for the wt CLI (local development)."

    def add_arguments(self, parser):
        parser.add_argument("--write", required=True, help="keys.json path")
        parser.add_argument("--server", default="http://localhost:8010")

    def handle(self, *args, write, server, **options):
        keys = keys_by_user()
        if not any(keys.values()):
            raise CommandError("API_KEY_SECRET is not set, so there are no keys to write.")
        path = Path(write).expanduser()
        merge_into(path, server.rstrip("/"), keys)
        self.stdout.write(f"Wrote {len(keys)} keys for {server} to {path}")
