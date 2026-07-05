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

# Security response headers (#37, surfaced by the #32 DAST scan). Safe defaults — never break
# the UI: DENY framing, no MIME sniffing, a trimmed referrer, a restrictive Permissions-Policy,
# and a CSP permissive enough for the /ui's CDN deps (Tailwind Play + Alpine/HTMX from unpkg —
# they need unsafe-eval + inline styles) while locking down everything else. Overridable via env.
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
PERMISSIONS_POLICY = _env(
    "PERMISSIONS_POLICY",
    "geolocation=(), microphone=(), camera=(), payment=(), usb=(), interest-cohort=()",
)
CONTENT_SECURITY_POLICY = _env(
    "CONTENT_SECURITY_POLICY",
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval' https://cdn.tailwindcss.com https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
)

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
    "incidents.session_tagging.SessionTaggingMiddleware",  # session.id + session.user on spans (ADR-022)
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",  # X-Frame-Options (#37)
    "config.security_headers.SecurityHeadersMiddleware",  # CSP + Permissions-Policy (#37)
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
                "incidents.session_tagging.session_id",  # session correlation id in /ui/ (ADR-022)
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
    # ADR-027: the only anonymous write path (the public status-page report form) is
    # bounded per-IP. Rate is env-tunable so a noisy source can be clamped without a deploy.
    "DEFAULT_THROTTLE_RATES": {
        "public_report": _env("PUBLIC_REPORT_THROTTLE", "10/min"),
    },
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

# --- Escalation paging (ADR-013): page the on-call via ntfy on a real tier entry ---
# The `paging_enabled` control itself is a rollout mode read via the flags seam (ADR-014), not here.
PAGING_ENV = _env("PAGING_ENV", "local")  # topic namespace: watch-<env>-user-<id> / -tier-<T>
NTFY_BASE_URL = _env("NTFY_BASE_URL", "https://ntfy.sh")
NTFY_TOKEN = _env("NTFY_TOKEN", "")  # empty for public ntfy.sh; access token / self-host for prod
# Secret salt mixed into paging topic names so they can't be derived from the (public) source
# (ADR-013). Empty → plain topics (local default). Prod: an SSM secret, like NTFY_TOKEN.
NTFY_TOPIC_SECRET = _env("NTFY_TOPIC_SECRET", "")

# --- Intake webhook auth (ADR-008): machine-to-machine shared secret ---
INTAKE_WEBHOOK_SECRET = _env("INTAKE_WEBHOOK_SECRET", "")

# CORS origin allowed to read the public /api/status posture (ADR-011). Default open
# for the local status-page SPA; set to the CloudFront/status domain in prod.
STATUS_PAGE_CORS_ORIGIN = _env("STATUS_PAGE_CORS_ORIGIN", "*")

# --- Status SSE feed (ADR-024) ---
# The SSE stream re-checks posture every POLL seconds and recycles after MAX_SECONDS (the
# EventSource auto-reconnects) so no connection is held indefinitely.
STATUS_STREAM_POLL_SECONDS = int(_env("STATUS_STREAM_POLL_SECONDS", "3") or "3")
STATUS_STREAM_MAX_SECONDS = int(_env("STATUS_STREAM_MAX_SECONDS", "300") or "300")

# Volatile payload fields stripped before hashing the dedupe key (ADR-009).
# Per-source config; this is the v1 default set.
INTAKE_VOLATILE_FIELDS = ["timestamp", "firedAt", "deliveryId", "messageId", "sequence"]

# --- Observability (OTel — §4.8) ---
OTEL_ENABLED = _bool("OTEL_ENABLED", False)
OTEL_SERVICE_NAME = _env("OTEL_SERVICE_NAME", "watch-backend")
OTEL_EXPORTER_OTLP_ENDPOINT = _env("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

# --- Session Check (ADR-022) ---
# Stable, non-rotated per-env secret keying HMAC(user id) -> session.user span attr. Rotating it
# orphans already-emitted spans. Empty => user-subject lookups disabled (session-id still works).
SESSION_USER_HMAC_KEY = _env("SESSION_USER_HMAC_KEY", "")
# M2M shared secret for the inbound Session Check webhook (like the intake secret, ADR-008).
CHECKS_WEBHOOK_SECRET = _env("CHECKS_WEBHOOK_SECRET", "")
# Run the check synchronously in-process (local/dev); the cloud path enqueues to SQS + a worker.
CHECKS_LOCAL_MODE = _bool("CHECKS_LOCAL_MODE", True)
# Trace backend the worker queries for error spans (ADR-026): none | tempo | grafana_cloud |
# datadog | sumologic. tempo/grafana_cloud are Watch's own telemetry; datadog/sumologic are
# query-only adapters onto EXISTING (work) telemetry — Watch-as-an-SRE-tool, no ingest here.
TRACE_STORE_PROVIDER = _env("TRACE_STORE_PROVIDER", "none")
TEMPO_QUERY_URL = _env("TEMPO_QUERY_URL", "http://localhost:3200")  # in-VPC Tempo query-frontend base
# Grafana Cloud Tempo (managed): HTTPS + basic auth (user = instance id, token = access-policy token).
GRAFANA_CLOUD_TEMPO_URL = _env("GRAFANA_CLOUD_TEMPO_URL", "")
GRAFANA_CLOUD_TEMPO_USER = _env("GRAFANA_CLOUD_TEMPO_USER", "")
GRAFANA_CLOUD_TEMPO_TOKEN = _env("GRAFANA_CLOUD_TEMPO_TOKEN", "")  # secret (SSM in prod)
# Datadog APM spans search (v2). Keys are secrets (SSM in prod); site e.g. datadoghq.com | us5... | eu.
DATADOG_SITE = _env("DATADOG_SITE", "datadoghq.com")
DATADOG_API_KEY = _env("DATADOG_API_KEY", "")
DATADOG_APP_KEY = _env("DATADOG_APP_KEY", "")
# Sumo Logic tracing (Search Job API). accessId/accessKey are secrets (SSM in prod); endpoint is
# region-specific, e.g. https://api.us2.sumologic.com.
SUMO_API_ENDPOINT = _env("SUMO_API_ENDPOINT", "")
SUMO_ACCESS_ID = _env("SUMO_ACCESS_ID", "")
SUMO_ACCESS_KEY = _env("SUMO_ACCESS_KEY", "")
# Default lookback when a check omits an explicit window (seconds).
CHECKS_DEFAULT_LOOKBACK_SECONDS = int(_env("CHECKS_DEFAULT_LOOKBACK_SECONDS", "3600") or "3600")
# Trace retention — a window fully older than this returns `aged_out` (never a false clean).
CHECKS_TRACE_RETENTION_SECONDS = int(_env("CHECKS_TRACE_RETENTION_SECONDS", "2592000") or "2592000")

# --- Outbound event webhooks (ADR-023) ---
# Deliver synchronously in-process (local/dev); the cloud path records `pending` and enqueues to SQS.
WEBHOOKS_LOCAL_MODE = _bool("WEBHOOKS_LOCAL_MODE", True)
# Signing secret for the loopback /api/webhook-echo receiver — a self-contained target for Watch's
# OWN outbound webhooks so the outbound path is verifiable locally + on staging (the E2E dogfood
# health check, ADR-022/023). Not for prod partner traffic. Point a subscription's secret at this.
WEBHOOK_ECHO_SECRET = _env("WEBHOOK_ECHO_SECRET", "")

# --- Async job queue + worker (ADR-025) ---
# Where the domain hands async work: local (no-op; work runs inline) | sqs (send to WATCH_QUEUE_URL).
QUEUE_PROVIDER = _env("QUEUE_PROVIDER", "local")
WATCH_QUEUE_URL = _env("WATCH_QUEUE_URL", "")
# run_sqs_worker long-poll + visibility knobs (seconds) and batch size.
WORKER_WAIT_SECONDS = int(_env("WORKER_WAIT_SECONDS", "20") or "20")  # SQS long-poll
WORKER_VISIBILITY_SECONDS = int(_env("WORKER_VISIBILITY_SECONDS", "60") or "60")
WORKER_BATCH_SIZE = int(_env("WORKER_BATCH_SIZE", "10") or "10")

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": _env("LOG_LEVEL", "INFO")},
}
