"""
Local Lambda shim (spec §5/§6) — lets Step Functions Local invoke the real Python
escalation handlers against Postgres, without building Lambda images (which this
sandbox can't do). Point Step Functions Local at it with
LAMBDA_ENDPOINT=http://host.docker.internal:<port>.

    python manage.py run_lambda_shim            # default port 9050

Run it in a tmux window; it's the host-side stand-in for the deployed Lambdas.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from incidents.lambda_shim import build_server


class Command(BaseCommand):
    help = "Run the local Lambda Invoke shim for Step Functions Local."

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=getattr(settings, "LAMBDA_SHIM_PORT", 9050))
        parser.add_argument("--host", default="0.0.0.0")

    def handle(self, *args, **opts):
        server, handlers = build_server(opts["host"], opts["port"])
        self.stdout.write(
            self.style.SUCCESS(
                f"lambda shim on http://{opts['host']}:{opts['port']} "
                f"(functions: {', '.join(handlers)})"
            )
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
