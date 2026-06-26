"""
Integration test settings (spec §6): real Postgres, real Valkey, the AppConfig Agent
container — the dependencies an integration run exercises. Step Functions Local is
driven directly via boto3 in the test, not through Django.

Run via:  DJANGO_SETTINGS_MODULE=config.settings_integration pytest -m integration
(the Makefile `integration` target sets this up against the compose stack).
"""
from .settings import *  # noqa: F401,F403

# Base settings already read POSTGRES_* / VALKEY_URL from env (localhost defaults),
# which point at the compose-exposed ports. We only force the real flag provider.
FLAGS_PROVIDER = "appconfig"
ESCALATION_LOCAL_MODE = True
