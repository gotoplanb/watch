"""
Make the Django ORM available inside the Lambda handlers so they can call the shared
`incidents.services` decision functions (ADR-001: ASL orchestrates, Python decides;
one decision implementation, two callers). In real AWS the handler package bundles
Django + deps; locally the run_lambda_shim command imports these in the Django process.

In AWS, Lambda has no ECS-style `secrets` block, so secrets are fetched here before
`django.setup()`: POSTGRES_PASSWORD from the RDS-managed Secrets Manager secret
(DB_MASTER_SECRET_ARN) and DJANGO_SECRET_KEY from SSM (DJANGO_SECRET_KEY_ARN). Both are
no-ops locally (ARNs unset → settings reads the env defaults), so this stays hermetic.
"""
import json
import os
import sys
from pathlib import Path

_READY = False


def _load_secrets():
    """Populate POSTGRES_PASSWORD / DJANGO_SECRET_KEY from AWS when ARNs are provided."""
    secret_arn = os.environ.get("DB_MASTER_SECRET_ARN")
    if secret_arn and not os.environ.get("POSTGRES_PASSWORD"):
        import boto3

        raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]
        cred = json.loads(raw)
        os.environ["POSTGRES_PASSWORD"] = cred["password"]
        os.environ.setdefault("POSTGRES_USER", cred.get("username", "watch"))

    key_arn = os.environ.get("DJANGO_SECRET_KEY_ARN")
    if key_arn and not os.environ.get("DJANGO_SECRET_KEY"):
        import boto3

        param = boto3.client("ssm").get_parameter(Name=key_arn, WithDecryption=True)
        os.environ["DJANGO_SECRET_KEY"] = param["Parameter"]["Value"]


def setup_django():
    global _READY
    if _READY:
        return
    backend = Path(__file__).resolve().parents[2] / "backend"
    if str(backend) not in sys.path and backend.exists():
        sys.path.insert(0, str(backend))
    _load_secrets()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()  # idempotent: apps.populate() guards re-entry
    _READY = True
