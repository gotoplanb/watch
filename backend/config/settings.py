"""
Django settings for the Watch v1 slice.

Config posture (spec §4.3): plain config inline via env vars; in prod, secrets
arrive through the task definition `secrets` block (SSM SecureString / Secrets
Manager), never inline. This file reads os.environ only — it has no secret values.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _bool(key: str, default: bool = False) -> bool:
    return _env(key, "1" if default else "0") in ("1", "true", "True", "yes")


SECRET_KEY = _env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = _bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = [h for h in _env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

# Behind a TLS-terminating ALB (§4.3): trust X-Forwarded-Proto so request.is_secure() is
# correct, and trust the public HTTPS origin for CSRF (Django checks POST Origin against this).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [o for o in _env("CSRF_TRUSTED_ORIGINS", "").split(",") if o]
# Secure cookies only when fronted by HTTPS (env-gated so local http still works).
SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = _bool("CSRF_COOKIE_SECURE", False)

# HSTS (#30): tell browsers to stay on HTTPS. Env-gated — 0 disables (local http); the ALB
# terminates TLS so this emits on request.is_secure() via SECURE_PROXY_SSL_HEADER above.
# include_subdomains scopes only to watch.'s subdomains (not the apex); preload stays off
# (hard to reverse — opt in deliberately).
SECURE_HSTS_SECONDS = int(_env("SECURE_HSTS_SECONDS", "0") or "0")
SECURE_HSTS_INCLUDE_SUBDOMAINS = _bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
SECURE_HSTS_PRELOAD = _bool("SECURE_HSTS_PRELOAD", False)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "incidents",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- Data: RDS Postgres Multi-AZ in prod; container locally (§4.3) ---
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _env("POSTGRES_DB", "watch"),
        "USER": _env("POSTGRES_USER", "watch"),
        "PASSWORD": _env("POSTGRES_PASSWORD", "watch"),
        "HOST": _env("POSTGRES_HOST", "localhost"),
        "PORT": _env("POSTGRES_PORT", "5432"),
    }
}

# --- Sessions externalized to Valkey (ADR-008, §4.3): stateless tasks ---
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": _env("VALKEY_URL", "redis://localhost:6379/0"),
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

# @login_required (UI, ADR-011) redirects here; the DRF/Django auth login honors ?next.
LOGIN_URL = "/api-auth/login/"
LOGIN_REDIRECT_URL = "/ui/incidents/"

REST_FRAMEWORK = {
    # ADR-008: session auth (cookies in Valkey). OIDC/SSO is a clean seam — add a
    # DRF auth class here later; nothing else changes.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# --- Feature flags (ADR-003): thin seam, provider selected by env ---
FLAGS_PROVIDER = _env("FLAGS_PROVIDER", "memory")  # memory | appconfig
APPCONFIG_AGENT_URL = _env("APPCONFIG_AGENT_URL", "http://localhost:2772")
APPCONFIG_APPLICATION = _env("APPCONFIG_APPLICATION", "watch")
APPCONFIG_ENVIRONMENT = _env("APPCONFIG_ENVIRONMENT", "local")
APPCONFIG_PROFILE = _env("APPCONFIG_PROFILE", "flags")

# --- Escalation engine (ADR-001/007) ---
AWS_REGION = _env("AWS_REGION", "us-east-1")
ESCALATION_STATE_MACHINE_ARN = _env("ESCALATION_STATE_MACHINE_ARN", "")
ESCALATION_LOCAL_MODE = _bool("ESCALATION_LOCAL_MODE", True)
# Point boto3 at Step Functions Local (e.g. http://localhost:8083) for the real local
# engine; empty = real AWS. The local Lambda shim listens on LAMBDA_SHIM_PORT.
ESCALATION_ENDPOINT_URL = _env("ESCALATION_ENDPOINT_URL", "")
LAMBDA_SHIM_PORT = int(_env("LAMBDA_SHIM_PORT", "9050"))

# Tier SLAs (seconds) — timeout on each waitForTaskToken (ADR-007)
TIER_SLA_SECONDS = {
    "T1": int(_env("SLA_T1_SECONDS", "900")),
    "T2": int(_env("SLA_T2_SECONDS", "1800")),
    "T3": int(_env("SLA_T3_SECONDS", "3600")),
}

# --- Intake webhook auth (ADR-008): machine-to-machine shared secret ---
INTAKE_WEBHOOK_SECRET = _env("INTAKE_WEBHOOK_SECRET", "")

# CORS origin allowed to read the public /api/status posture (ADR-011). Default open
# for the local status-page SPA; set to the CloudFront/status domain in prod.
STATUS_PAGE_CORS_ORIGIN = _env("STATUS_PAGE_CORS_ORIGIN", "*")

# Volatile payload fields stripped before hashing the dedupe key (ADR-009).
# Per-source config; this is the v1 default set.
INTAKE_VOLATILE_FIELDS = ["timestamp", "firedAt", "deliveryId", "messageId", "sequence"]

# --- Observability (OTel — §4.8) ---
OTEL_ENABLED = _bool("OTEL_ENABLED", False)
OTEL_SERVICE_NAME = _env("OTEL_SERVICE_NAME", "watch-backend")
OTEL_EXPORTER_OTLP_ENDPOINT = _env("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": _env("LOG_LEVEL", "INFO")},
}
