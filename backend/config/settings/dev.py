"""
Development settings — local machine and docker-compose.

Everything here trades a production guarantee for iteration speed. Each
override says what it is trading away so that nobody copies it into prod.py.
"""

from __future__ import annotations

from decouple import Csv, config

from .base import *  # noqa: F401,F403
from .base import CACHES, LOGGING, REST_FRAMEWORK, SIMPLE_JWT, STORAGES, BACKEND_DIR
from datetime import timedelta

DEBUG = True
SECRET_KEY = config("DJANGO_SECRET_KEY", default="dev-only-not-a-secret")
ALLOWED_HOSTS = config(
    "DJANGO_ALLOWED_HOSTS",
    default="localhost,127.0.0.1,0.0.0.0,api,.localhost",
    cast=Csv(),
)

INTERNAL_IPS = ["127.0.0.1"]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Even locally we connect as the non-superuser app role. If developers run as
# the owner/superuser, RLS is bypassed on their machine, every tenant test
# passes, and the isolation bug is discovered in production. docker-compose
# creates `erp_app` and `erp_migrator` for exactly this reason.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("POSTGRES_DB", default="erp"),
        "USER": config("POSTGRES_APP_USER", default="erp_app"),
        "PASSWORD": config("POSTGRES_APP_PASSWORD", default="erp_app"),
        "HOST": config("POSTGRES_HOST", default="localhost"),
        "PORT": config("POSTGRES_PORT", default="5432"),
        "CONN_MAX_AGE": 0,
        "ATOMIC_REQUESTS": True,
        "OPTIONS": {"application_name": "erp-api-dev"},
        "TEST": {"NAME": "test_erp"},
    }
}

# ---------------------------------------------------------------------------
# DRF
# ---------------------------------------------------------------------------
# Browsable API is available locally only. It is off in prod because it runs
# queries to build its HTML forms (a <select> of every account in the tenant).
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
    # Throttling off locally: nothing is more annoying than a 429 while
    # debugging a seed script. It is *on* in CI so the config is exercised.
    "DEFAULT_THROTTLE_CLASSES": (),
    # The *rates* map is kept even though the default classes are off. Any
    # view that names a scope explicitly (`throttle_scope = "login"`) still
    # instantiates a ScopedRateThrottle, and an empty map makes that raise
    # ImproperlyConfigured at request time — turning "throttling off" into
    # "endpoint returns 500 locally but works in CI".
    "DEFAULT_THROTTLE_RATES": {
        **REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
        "login": "1000/min",
        "report": "1000/hour",
        "reports": "1000/hour",
        "export": "1000/hour",
    },
}

# Longer access token so you are not re-authenticating every five minutes
# while stepping through a debugger. Never do this in prod: it widens the
# window in which a stolen bearer token is usable.
SIMPLE_JWT = {**SIMPLE_JWT, "ACCESS_TOKEN_LIFETIME": timedelta(hours=8)}

# ---------------------------------------------------------------------------
# Storage / email
# ---------------------------------------------------------------------------
STORAGES = {
    **STORAGES,
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
# eager=False by default: run `make worker` so that the queue routing, the
# acks_late semantics and the idempotency keys are actually exercised locally.
# Set CELERY_TASK_ALWAYS_EAGER=1 only for a quick one-off.
CELERY_TASK_ALWAYS_EAGER = config("CELERY_TASK_ALWAYS_EAGER", default=False, cast=bool)
CELERY_TASK_EAGER_PROPAGATES = True

# ---------------------------------------------------------------------------
# Cache — Redis optional locally
# ---------------------------------------------------------------------------
# Production uses Redis, and so should CI: LocMemCache gives every process its
# own dict, which hides exactly the key-collision and cross-tenant-namespacing
# bugs that `apps.core.cache.tenant_key_func` exists to prevent.
#
# But requiring Redis to *start the server* is a bad trade on a developer
# machine, especially on Windows where there is no supported native build. The
# cache here holds effective permissions and throttle counters — both are
# recomputable, and neither is correctness-critical for a single-process dev
# server. So Redis is opt-in locally and its absence degrades to LocMemCache
# instead of refusing to boot.
#
#   USE_REDIS=1  -> Redis, same as production (use this before shipping
#                   anything that touches caching)
#   unset        -> in-memory, no external service required
USE_REDIS = config("USE_REDIS", default=False, cast=bool)

if USE_REDIS:
    CACHES = {**CACHES}
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "erp-dev-default",
            "KEY_FUNCTION": "apps.core.cache.tenant_key_func",
            "KEY_PREFIX": "erp",
        },
        "shared": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "erp-dev-shared",
            "KEY_PREFIX": "erp-shared",
        },
        "sessions": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "erp-dev-sessions",
            "KEY_PREFIX": "erp-sess",
        },
    }

# Without a broker there is nowhere to queue a task, so run them inline. This
# keeps reminders, report generation and payroll callbacks working in a plain
# `runserver` — they execute synchronously inside the request instead of in a
# worker. `EAGER_PROPAGATES` means a task failure surfaces as a real traceback
# rather than being swallowed the way a remote worker would swallow it.
if not USE_REDIS:
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    **LOGGING,
    "root": {"handlers": ["console"], "level": config("LOG_LEVEL", default="DEBUG")},
}

CORS_ALLOW_ALL_ORIGINS = False  # keep the header list honest even locally
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default="http://localhost:3000,http://localhost:8081,http://localhost:19006",
    cast=Csv(),
)

# Expo/React Native dev client hits the API from a LAN IP.
CSRF_TRUSTED_ORIGINS = config(
    "CSRF_TRUSTED_ORIGINS",
    default="http://localhost:3000,http://localhost:8081",
    cast=Csv(),
)

SEED_DEMO_TENANTS = 2
FIXTURE_DIRS = [BACKEND_DIR / "fixtures"]
