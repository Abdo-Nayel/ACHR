"""
Production / staging settings.

Rule for this file: **nothing has a usable default**. Every secret is read
from the environment with no fallback, so a missing variable is a loud
start-up crash instead of a service running on a well-known dev key.
"""

from __future__ import annotations

import logging

import sentry_sdk
from decouple import Csv, config
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.redis import RedisIntegration

from .base import *  # noqa: F401,F403
from .base import DATABASES, LOGGING, STORAGES

DEBUG = False

# No default: refusing to boot beats booting with a known key.
SECRET_KEY = config("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", cast=Csv())

# Wildcard hosts are how DNS-rebinding and Host-header poisoning (password
# reset links pointing at an attacker's domain) get in. The tenant resolver
# already matches on TenantDomain rows, so the list is short and explicit.
if "*" in ALLOWED_HOSTS:
    raise RuntimeError("ALLOWED_HOSTS must not contain '*' in production.")


# ---------------------------------------------------------------------------
# Transport security
# ---------------------------------------------------------------------------
# The load balancer terminates TLS and forwards this header. Django must be
# told, or is_secure() is False, secure cookies are never set, and every
# redirect loops.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True

# HSTS: after the first visit the browser refuses plain http for this domain,
# closing the sslstrip window on the *next* request. Roll it out with a short
# max-age first — preload is effectively irreversible for two years.
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=False, cast=bool)

SECURE_CONTENT_TYPE_NOSNIFF = True   # a user-uploaded "receipt.pdf" that is
                                     # actually HTML must not be sniffed and
                                     # executed in our origin
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"             # no clickjacking of the "post payroll" button

# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = False   # the SPA must read it to echo X-CSRFToken
CSRF_COOKIE_SAMESITE = "Lax"

# The refresh token cookie: httpOnly so XSS cannot exfiltrate it, Strict so it
# is not attached to cross-site navigations, and path-scoped to the refresh
# endpoint so it is not sent with every API call.
JWT_REFRESH_COOKIE_SECURE = True
JWT_REFRESH_COOKIE_HTTPONLY = True
JWT_REFRESH_COOKIE_SAMESITE = "Strict"

CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", cast=Csv())
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", cast=Csv())
CORS_ALLOW_ALL_ORIGINS = False


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        **DATABASES["default"],
        "NAME": config("POSTGRES_DB"),
        "USER": config("POSTGRES_APP_USER"),          # NOT the owner, NOT superuser
        "PASSWORD": config("POSTGRES_APP_PASSWORD"),
        "HOST": config("POSTGRES_HOST"),
        "PORT": config("POSTGRES_PORT", default="5432"),
        # 0 when behind pgbouncer in transaction mode (the default topology);
        # raise only if you have verified session pooling end to end.
        "CONN_MAX_AGE": config("DB_CONN_MAX_AGE", default=0, cast=int),
        "OPTIONS": {
            "sslmode": config("POSTGRES_SSLMODE", default="require"),
            "application_name": "erp-api",
            "options": (
                "-c statement_timeout=30000 "
                "-c idle_in_transaction_session_timeout=60000"
            ),
        },
    }
}

# Reporting replica. Read-only aggregates (trial balance over 50M lines) must
# not compete with the transactional pool for buffers.
if config("POSTGRES_REPLICA_HOST", default=""):
    DATABASES["replica"] = {
        **DATABASES["default"],
        "HOST": config("POSTGRES_REPLICA_HOST"),
        "OPTIONS": {**DATABASES["default"]["OPTIONS"], "application_name": "erp-reports"},
        # ATOMIC_REQUESTS on a replica would try to open a write transaction.
        "ATOMIC_REQUESTS": False,
    }
    # No automatic router: routing *all* reads to the replica breaks
    # read-your-writes (a read straight after a write would hit stale replica
    # data). Heavy report aggregates opt in explicitly with
    # ``.using("replica")`` instead, which is the only place the lag is
    # acceptable. (A blanket ``DATABASE_ROUTERS`` here previously named a router
    # module that did not exist — a boot crash the moment a replica was set.)


# ---------------------------------------------------------------------------
# Storage / email
# ---------------------------------------------------------------------------
STORAGES = {
    **STORAGES,
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": config("S3_BUCKET"),
            "region_name": config("S3_REGION"),
            "default_acl": None,        # bucket is private; no public objects
            "querystring_auth": True,   # every download is a signed, expiring URL
            "querystring_expire": 900,
            "file_overwrite": False,
            "signature_version": "s3v4",
            "object_parameters": {"ServerSideEncryption": "AES256"},
        },
    },
}

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL")

CELERY_BROKER_URL = config("CELERY_BROKER_URL")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND")

STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET")


# ---------------------------------------------------------------------------
# Logging — JSON to stdout, collected by the platform
# ---------------------------------------------------------------------------
LOGGING = {
    **LOGGING,
    "root": {"handlers": ["json"], "level": config("LOG_LEVEL", default="INFO")},
    "loggers": {
        name: {**cfg, "handlers": ["json"]}
        for name, cfg in LOGGING["loggers"].items()
    },
}


# ---------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------
SENTRY_DSN = config("SENTRY_DSN", default="")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=config("SENTRY_ENVIRONMENT", default="production"),
        release=config("GIT_SHA", default="unknown"),
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(monitor_beat_tasks=True),
            RedisIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=config("SENTRY_TRACES_SAMPLE_RATE", default=0.05, cast=float),
        profiles_sample_rate=config("SENTRY_PROFILES_SAMPLE_RATE", default=0.0, cast=float),
        # This is an accounting system: request bodies contain salaries, bank
        # accounts and tax IDs. Never ship PII to a third party by default.
        send_default_pii=False,
        max_request_body_size="never",
        before_send=lambda event, hint: __import__(
            "apps.core.observability", fromlist=["scrub_event"]
        ).scrub_event(event, hint),
    )


# ---------------------------------------------------------------------------
# Operational guards
# ---------------------------------------------------------------------------
# Isolation is asserted by real Django deploy checks now — see
# apps/tenancy/checks.py and apps/accounting/checks.py, run by
# ``manage.py check --deploy`` (and ``manage.py check_rls``) in CI and at deploy.
# The former ``STARTUP_CHECKS`` list here named functions that did not exist and
# was read by nothing; the check framework replaces it.

ADMINS = [(n, n) for n in config("ADMIN_EMAILS", default="", cast=Csv())]
SERVER_EMAIL = DEFAULT_FROM_EMAIL
