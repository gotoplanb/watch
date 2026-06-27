"""
Local Lambda Invoke shim (spec §5/§6). Speaks the minimal AWS Lambda Invoke API so
Step Functions Local can invoke the real Python escalation handlers against Postgres
without building Lambda images. Used by the `run_lambda_shim` command and the e2e test.
"""
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from django.conf import settings


def load_handlers():
    lambdas_dir = Path(settings.BASE_DIR).parent / "escalation" / "lambdas"
    if str(lambdas_dir) not in sys.path:
        sys.path.insert(0, str(lambdas_dir))
    import commit
    import record_token

    return {"record_token": record_token.handler, "commit": commit.handler}


def _make_handler(handlers):
    class LambdaInvokeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _read_body(self):
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                chunks = []
                while True:
                    size_line = self.rfile.readline().split(b";")[0].strip()
                    size = int(size_line, 16) if size_line else 0
                    if size == 0:
                        self.rfile.readline()  # trailing CRLF
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()  # CRLF after each chunk
                return b"".join(chunks)
            return self.rfile.read(int(self.headers.get("Content-Length", 0)))

        def do_POST(self):
            parts = [p for p in self.path.split("/") if p]
            name = parts[parts.index("functions") + 1] if "functions" in parts else ""
            if ":" in name:
                name = name.split(":")[-1]
            handler = handlers.get(name)

            event = json.loads(self._read_body() or b"{}")

            if handler is None:
                return self._respond(404, {"errorMessage": f"unknown function {name}"})
            try:
                self._respond(200, handler(event, None))
            except Exception as exc:  # surface as a Lambda function error to SFN
                traceback.print_exc()
                self._respond(
                    200,
                    {"errorType": type(exc).__name__, "errorMessage": str(exc)},
                    function_error="Unhandled",
                )
            finally:
                # Each request runs in its own thread with its own DB connection; close
                # it so threads don't pin the (test) database.
                from django.db import connections

                connections.close_all()

        def _respond(self, status, body, function_error=None):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if function_error:
                self.send_header("X-Amz-Function-Error", function_error)
            self.end_headers()
            self.wfile.write(payload)

    return LambdaInvokeHandler


def build_server(host="0.0.0.0", port=None):
    port = port or getattr(settings, "LAMBDA_SHIM_PORT", 9050)
    handlers = load_handlers()
    return ThreadingHTTPServer((host, port), _make_handler(handlers)), handlers
