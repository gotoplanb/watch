"""The little HTTP wt needs, on the standard library alone."""
import json
import mimetypes
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import __version__

USER_AGENT = f"wt/{__version__}"


class ApiError(Exception):
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {_describe(body)}")


def _describe(body) -> str:
    """The server's reason, readably: DRF's detail plus any form errors, or an HTML page's title."""
    if isinstance(body, dict):
        text = str(body.get("detail") or json.dumps(body)[:400])
        for field, errors in (body.get("errors") or {}).items():
            messages = "; ".join(e.get("message", str(e)) if isinstance(e, dict) else str(e)
                                 for e in errors)
            text += f"\n  {field}: {messages}"
        return text
    if isinstance(body, str):
        title = re.search(r"<title>(.*?)</title>", body, re.S)
        return (title.group(1).strip() if title else body[:400]).strip()
    return json.dumps(body)[:400]


def multipart(fields: dict, files: dict[str, list[str]]) -> tuple[bytes, str]:
    """Encode form fields and files (field name -> local paths) as multipart/form-data."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        for item in value if isinstance(value, list) else [value]:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                         f"{item}\r\n".encode())
    for name, paths in files.items():
        for path in paths:
            file = Path(path).expanduser()
            kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                    f'filename="{file.name}"\r\nContent-Type: {kind}\r\n\r\n')
            parts.append(head.encode() + file.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def request(method: str, url: str, *, body=None, files=None, raw=None, headers=None,
            timeout: float = 30):
    """Send JSON, multipart (files) or raw bytes (`raw=(bytes, content_type)`); return the decoded
    reply. Raises ApiError."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    data = None
    if raw is not None:
        data, headers["Content-Type"] = raw
    elif files:
        data, headers["Content-Type"] = multipart(body or {}, files)
    elif body is not None:
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
