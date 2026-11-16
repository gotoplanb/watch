"""Where wt keeps what it knows: servers by name, and each server's per-user keys.

Keys live in ~/.config/wt/keys.json (mode 600) as {"servers": {url: {username: key}}}.
`make cli-keys` writes the local server's; `wt key set` adds one for any other server.
"""
import json
import os
from pathlib import Path

SERVERS = {"local": "http://localhost:8010"}
# The `make dev` intake secret (Makefile HOSTENV). Only ever used against the local server.
LOCAL_WEBHOOK_SECRET = "dev-webhook-secret"


def config_dir() -> Path:
    return Path(os.environ.get("WT_CONFIG_DIR") or Path.home() / ".config" / "wt")


def keys_path() -> Path:
    return config_dir() / "keys.json"


def server_url(name: str | None) -> str:
    name = name or os.environ.get("WT_SERVER") or "local"
    if name in SERVERS:
        return SERVERS[name]
    if name.startswith(("http://", "https://")):
        return name.rstrip("/")
    raise SystemExit(f"wt: unknown server {name!r}; use {', '.join(SERVERS)} or a URL")


def load() -> dict:
    try:
        return json.loads(keys_path().read_text())
    except FileNotFoundError:
        return {}


def users(server: str) -> dict[str, str]:
    return load().get("servers", {}).get(server, {})


def key_for(server: str, username: str | None) -> str:
    known = users(server)
    if not username:
        names = ", ".join(sorted(known)) or "none yet; run `make cli-keys`"
        raise SystemExit(f"wt: say who to act as with --as <user> or WT_AS (known: {names})")
    if username not in known:
        hint = "run `make cli-keys`" if server == SERVERS["local"] else "use `wt key set`"
        raise SystemExit(f"wt: no key for {username!r} on {server}; {hint}")
    return known[username]


def set_key(server: str, username: str, key: str) -> None:
    data = load()
    data.setdefault("servers", {}).setdefault(server, {})[username] = key
    path = keys_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def webhook_secret(server: str) -> str:
    secret = os.environ.get("WT_WEBHOOK_SECRET")
    if secret:
        return secret
    if server == SERVERS["local"]:
        return LOCAL_WEBHOOK_SECRET
    raise SystemExit("wt: set WT_WEBHOOK_SECRET for a non-local server")
