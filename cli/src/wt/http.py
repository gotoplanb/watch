"""The little HTTP wt needs, on the standard library alone."""
import json
import urllib.error
import urllib.request

from . import __version__

USER_AGENT = f"wt/{__version__}"


class ApiError(Exception):
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        detail = body.get("detail") if isinstance(body, dict) else None
        super().__init__(f"HTTP {status}: {detail or json.dumps(body)[:400]}")


def request(method: str, url: str, *, body=None, headers=None, timeout: float = 30):
    """Send JSON, return the decoded JSON reply (or text). Raises ApiError on 4xx/5xx."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _decode(resp.read())
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, _decode(exc.read())) from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"wt: can't reach {url}: {exc.reason} (is `make dev` up?)") from None


def _decode(raw: bytes):
    text = raw.decode(errors="replace")
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return text
