import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from incidents import automation, cadence, diagnostics
from incidents.models import Assessment, DiagnosticRun

logger = logging.getLogger(__name__)


def run_job(kind, pk):
    close_old_connections()
    try:
        if kind == "assessment":
            automation.run(pk)
        else:
            run = DiagnosticRun.objects.select_related("assessment", "rule").get(pk=pk)
            diagnostics.execute(run.assessment, run.rule, run_id=run.pk)
    finally:
        close_old_connections()


class Command(BaseCommand):
    help = "Sweep deadlines and queued diagnostics; --loop keeps working without a browser."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=float, default=1)

    def handle(self, *args, **options):
        running = True

        def stop(*_):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        jobs = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            while running:
                close_old_connections()
                try:
                    for key, future in list(jobs.items()):
                        if future.done():
                            del jobs[key]
                            future.result()
                    candidates = [("assessment", pk) for pk in Assessment.objects.filter(automation_state="queued").values_list("pk", flat=True)[:3]]
                    candidates += [("diagnostic", pk) for pk in DiagnosticRun.objects.filter(state="queued").values_list("pk", flat=True)[:3]]
                    for key in candidates:
                        if key not in jobs:
                            if options["loop"]:
                                jobs[key] = pool.submit(run_job, *key)
                            else:
                                run_job(*key)
                    result = cadence.tick()
                    if not options["loop"]:
                        self.stdout.write(str(result))
                        break
                except Exception:
                    logger.exception("Operations sweep failed; next sweep will retry durable work")
                    if not options["loop"]:
                        raise
                if options["interval"] < 0.2:
                    raise ValueError("Interval must be at least 0.2 seconds")
                time.sleep(options["interval"])
