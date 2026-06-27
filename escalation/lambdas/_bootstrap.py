"""
Make the Django ORM available inside the Lambda handlers so they can call the shared
`incidents.services` decision functions (ADR-001: ASL orchestrates, Python decides;
one decision implementation, two callers). In real AWS the handler package bundles
Django + deps; locally the run_lambda_shim command imports these in the Django process.
"""
import os
import sys
from pathlib import Path

_READY = False


def setup_django():
    global _READY
    if _READY:
        return
    backend = Path(__file__).resolve().parents[2] / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()  # idempotent: apps.populate() guards re-entry
    _READY = True
