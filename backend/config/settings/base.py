"""
Base Django settings — shared by every environment.

Read this file as a list of *decisions*, not a list of knobs. Each block
states the failure it prevents; if you change one, you are re-opening that
failure. Environment-specific overrides live in ``dev.py`` and ``prod.py``.

Layout
------
``config/settings/base.py``  -> everything common
``config/settings/dev.py``   -> local developer machine, docker-compose
``config/settings/prod.py``  -> production/staging, all secrets from env

Selected via ``DJANGO_SETTINGS_MODULE``; ``manage.py`` defaults to ``dev``.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from decouple import Csv, config

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# base.py -> settings/ -> config/ -> backend/
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = BACKEND_DIR.parent

# ``apps.accounting`` rather than ``backend.apps.accounting``: the import path
# in every module of this codebase is ``apps.<name>``, so ``backend/`` must be
# on sys.path. Keeping it implicit here (instead of relying on the cwd) means
# Celery workers, management commands and pytest all resolve imports the same
# way.
import sys  # noqa: E402  (deliberately after BACKEND_DIR is known)

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

SECRET_KEY = config("DJANGO_SECRET_KEY", default="insecure-dev-key-override-me")
DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Django 5 default. Explicit because changing it later silently changes the
# column type of every new model's implicit PK — but note that in this project
# *every* business model uses an explicit UUID PK (see apps.core.models).
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The whole authorisation model (TenantMembership, Role, ScopeRule) hangs off
# this. It cannot be changed after the first migration without a table rebuild.
AUTH_USER_MODEL = "iam.User"


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_spectacular",
    "corsheaders",
    "django_celery_beat",
]

# Order matters only for template/static resolution and for the app registry's
# migration graph; it is listed dependency-first for readability.
LOCAL_APPS = [
    "apps.core",       # abstract bases, money fields, tenancy context
    "apps.tenancy",    # Tenant, domains, subscriptions, RLS migration
    "apps.iam",        # users, memberships, RBAC + ABAC
    "apps.accounting",  # chart of accounts, fiscal calendar, general ledger
    "apps.sales",      # customers, invoices, credit notes
    "apps.payments",   # payments, applications, refunds, gateway webhooks
    "apps.expenses",   # employee expenses, vendor bills
    "apps.inventory",  # items, stock levels, movements
    "apps.banking",    # bank accounts, statements, reconciliation
    "apps.projects",   # projects, timesheets, WIP
    "apps.hr",         # employees, departments, attendance, leave
    "apps.payroll",    # payroll runs, payslips, statutory contributions
    "apps.reporting",  # trial balance, P&L, balance sheet, AR aging
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
# ORDER IS A SECURITY CONTROL, NOT A STYLE CHOICE.
#
# 1. RequestIDMiddleware first so that *every* log line, including one emitted
#    by a 500 in a later middleware, carries a correlation id.
# 2. Security/CORS/session/auth as Django ships them.
# 3. TenantMiddleware AFTER AuthenticationMiddleware — it resolves the tenant
#    from the authenticated principal's membership (and the host header), so
#    it needs request.user to already exist. Putting it earlier means it must
#    trust an unauthenticated header, which is a trivial cross-tenant read.
# 4. TenantMiddleware BEFORE any view runs, because it is what sets the
#    ``app.current_tenant`` PostgreSQL session variable that every RLS policy
#    reads. A view that executes before it runs against a session with no
#    tenant bound: the RLS policy then matches nothing and the request fails
#    closed (correct), but the ORM manager would also return .none() and the
#    bug would look like "data disappeared" instead of "middleware misordered".
# 5. AuditContextMiddleware last so it can see the resolved tenant + user.
MIDDLEWARE = [
    "apps.core.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # ---- tenant boundary established here ----
    "apps.tenancy.middleware.TenantMiddleware",
    "apps.tenancy.middleware.TenantSubscriptionGateMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "apps.core.middleware.AuditContextMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BACKEND_DIR / "templates"],
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


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# THE SINGLE MOST IMPORTANT SETTING IN THIS FILE.
#
# The application connects as a NON-SUPERUSER, NON-OWNER role (``erp_app``).
#
# Why: PostgreSQL Row-Level Security is *bypassed* by superusers, by roles
# with BYPASSRLS, and by the table owner, unless the table is declared
# ``FORCE ROW LEVEL SECURITY``. Teams routinely deploy with the app connecting
# as the migration/owner role, see their tests pass (because tests seed one
# tenant), and ship a system with no tenant isolation at all. That is the #1
# way a team silently disables its own isolation.
#
# We defend twice:
#   * the app role is not the owner and has no BYPASSRLS, and
#   * every tenant table is ALTER TABLE ... FORCE ROW LEVEL SECURITY
#     (see apps/tenancy/migrations/0002_row_level_security.py)
#
# Migrations are run by a *different* role (``erp_migrator``, the owner) via
# the DATABASE_MIGRATION_URL used by `make migrate` in CI/CD.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("POSTGRES_DB", default="erp"),
        "USER": config("POSTGRES_APP_USER", default="erp_app"),
        "PASSWORD": config("POSTGRES_APP_PASSWORD", default="erp_app"),
        "HOST": config("POSTGRES_HOST", default="localhost"),
        "PORT": config("POSTGRES_PORT", default="5432"),
        # ---------------------------------------------------------------
        # CONN_MAX_AGE and pgbouncer
        # ---------------------------------------------------------------
        # Django persistent connections are safe *only* when Django owns the
        # socket end to end (direct connection, or pgbouncer in SESSION mode).
        #
        # With pgbouncer in TRANSACTION pooling mode the server connection is
        # handed back to the pool at COMMIT, so:
        #   * CONN_MAX_AGE must be 0 (Django's idea of "my connection" is a
        #     fiction; keeping it alive pins a pooler client slot forever), and
        #   * every tenant binding must use SET LOCAL / set_config(..., true)
        #     inside a transaction, never a plain SET.
        # ``apps.core.tenancy_context.bind_database_session`` already uses
        # ``set_config(key, value, /* is_local */ true)``. A plain SET would
        # leak ``app.current_tenant`` to whichever request receives that
        # server connection next — a cross-tenant data leak that appears only
        # under load and is essentially impossible to reproduce locally.
        "CONN_MAX_AGE": config("DB_CONN_MAX_AGE", default=0, cast=int),
        "CONN_HEALTH_CHECKS": True,
        "ATOMIC_REQUESTS": True,  # see note below
        "OPTIONS": {
            "application_name": config("DB_APPLICATION_NAME", default="erp-api"),
            # Fail fast instead of holding a pooler slot behind a lock.
            "options": "-c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000",
        },
        "TEST": {"NAME": "test_erp"},
    }
}

# ATOMIC_REQUESTS=True is deliberate. Two reasons specific to this system:
#   1. ``SET LOCAL app.current_tenant`` only survives inside a transaction, so
#      the tenant binding and the request must share one.
#   2. A partially-applied financial write (invoice saved, journal entry not)
#      is worse than a 500. All-or-nothing is the only acceptable semantics.
# Long-running report endpoints opt out with @transaction.non_atomic_requests.

DATABASE_ROUTERS: list[str] = []

# Read replica for reporting, wired in prod.py when REPLICA_HOST is present.
REPLICA_ALIAS = "replica"


# ---------------------------------------------------------------------------
# Decimal / money handling
# ---------------------------------------------------------------------------
# Amounts are numeric(19,6) in PostgreSQL and Decimal in Python end to end.
# These settings make sure they survive the *edges* (JSON parse, JSON render).
#
# Failure prevented: a float anywhere in the chain turns 0.1 + 0.2 into
# 0.30000000000000004, the entry fails ck_entry_balanced, and the posting
# transaction rolls back — or worse, it rounds into balance and the customer's
# trial balance is off by cents that nobody can find.
MONEY_DECIMAL_PLACES = 6
MONEY_MAX_DIGITS = 19
#: DRF must emit decimals as JSON *strings*. If this is False, DRF renders a
#: JSON number, JavaScript parses it into a float64, and the client's totals
#: row disagrees with the ledger. See docs/07-frontend-architecture.md.
COERCE_DECIMAL_TO_STRING = True
USE_THOUSAND_SEPARATOR = False  # formatting is the client's job, per locale


# ---------------------------------------------------------------------------
# Password / auth
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Argon2 first: bcrypt silently truncates at 72 bytes and PBKDF2 needs an
# ever-increasing iteration count to stay honest.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

AUTHENTICATION_BACKENDS = ["apps.iam.backends.TenantAwareModelBackend"]


# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "apps.iam.authentication.ApiKeyAuthentication",
    ),
    # Deny by default. An endpoint that forgets its permission_classes must
    # 403, not leak. The tenant/ABAC narrowing happens inside the permission.
    "DEFAULT_PERMISSION_CLASSES": ("apps.iam.permissions.HasTenantPermission",),
    # CURSOR pagination, not PageNumber/LimitOffset. Ledger and journal lists
    # are append-heavy: with OFFSET pagination a row inserted while the user
    # pages shifts everything and rows are skipped or duplicated. Cursor
    # pagination is also O(1) instead of O(offset) on a 50M-row ledger.
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.TenantCursorPagination",
    "PAGE_SIZE": 50,
    # JSON only. BrowsableAPIRenderer executes queries to build its HTML forms
    # (a dropdown of every account in the tenant) and has leaked data through
    # its form rendering before. Use /api/schema/docs/ instead.
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "DEFAULT_PARSER_CLASSES": (
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ),
    # Single translator from Python exception -> RFC7807-ish JSON body, so a
    # DB constraint violation ("ck_entry_balanced") becomes a documented
    # error code instead of a 500 with a stack trace.
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "apps.core.throttling.TenantScopedUserRateThrottle",
        "apps.core.throttling.TenantScopedAnonRateThrottle",
    ),
    # Rates are per-(tenant, user): a single tenant hammering exports must not
    # exhaust another tenant's budget.
    "DEFAULT_THROTTLE_RATES": {
        "anon": "30/min",
        "user": "1000/hour",
        "login": "10/min",       # credential stuffing
        "report": "60/hour",     # heavy aggregate queries
        "reports": "60/hour",    # alias: BurstThrottle uses the plural
        "export": "20/hour",     # full-ledger CSV dumps
        "webhook": "600/min",    # gateway callbacks; high but bounded
    },
    "COERCE_DECIMAL_TO_STRING": COERCE_DECIMAL_TO_STRING,
    "DATETIME_FORMAT": "iso-8601",
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "NON_FIELD_ERRORS_KEY": "detail",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Accounting & HR ERP API",
    "DESCRIPTION": (
        "Multi-tenant accounting and HR platform. All monetary values are "
        "JSON strings holding decimal numbers — never parse them as floats."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v[0-9]+",
    "COMPONENT_SPLIT_REQUEST": True,
    # The web/mobile clients generate their TypeScript types and Zod schemas
    # from this document (see docs/07-frontend-architecture.md), so the schema
    # is a build artefact, not documentation. It is checked in CI.
    "ENUM_NAME_OVERRIDES": {
        "JournalEntryStatusEnum": "apps.accounting.models.JournalEntry.Status",
    },
}


# ---------------------------------------------------------------------------
# SimpleJWT
# ---------------------------------------------------------------------------
# Short access token + rotating refresh + blacklist.
#
# Failure prevented: a stolen access token is a bearer credential that cannot
# be revoked. Five minutes bounds the damage. Rotation + blacklist turns
# refresh-token theft into a *detectable* event: when the attacker uses the
# stolen refresh token, the legitimate client's next refresh presents an
# already-rotated token, which is blacklisted, and the whole family is killed.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=5),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": config("JWT_SIGNING_KEY", default=SECRET_KEY),
    "AUDIENCE": config("JWT_AUDIENCE", default="erp-api"),
    "ISSUER": config("JWT_ISSUER", default="erp-auth"),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_TYPE_CLAIM": "token_type",
    "JTI_CLAIM": "jti",
    # Custom pair serialiser adds `tenant_id`, `membership_id` and the
    # permission set. The tenant claim is a *hint*: TenantMiddleware still
    # re-reads TenantMembership, because a membership deactivated one minute
    # ago must not survive in a token for another four.
    "TOKEN_OBTAIN_SERIALIZER": "apps.iam.serializers.TenantTokenObtainPairSerializer",
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "SLIDING_TOKEN_LIFETIME": timedelta(minutes=5),
}

#: Name of the httpOnly cookie the web client stores its refresh token in.
#: Mobile uses expo-secure-store and the Authorization header instead.
JWT_REFRESH_COOKIE_NAME = "erp_refresh"
JWT_REFRESH_COOKIE_PATH = "/api/v1/auth/"


# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")

CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True

# ---------------------------------------------------------------------------
# acks_late: the trade we are making, spelled out
# ---------------------------------------------------------------------------
# task_acks_late=True means the message is acknowledged AFTER the task body
# finishes, so a worker killed mid-task (OOM, spot-instance reclaim, deploy)
# does not silently lose the job — the broker redelivers it.
#
# The cost: AT-LEAST-ONCE delivery. The task WILL run twice sometimes. With a
# non-idempotent task this is catastrophic in an accounting system: a payroll
# run posted twice, an invoice emailed twice, a Stripe charge captured twice.
#
# THIS IS ONLY SAFE BECAUSE EVERY FINANCIAL TASK IS IDEMPOTENT:
#   * JournalEntry.idempotency_key has a per-tenant unique constraint, so a
#     replayed posting raises IntegrityError and is swallowed as a no-op
#     (apps.accounting.services.posting.post_entry).
#   * PaymentWebhookEvent stores the gateway event id uniquely.
#   * PayrollRun transitions are guarded by an explicit state machine.
# If you add a task that moves money and has no idempotency key, you must
# either add one or route it to a queue with acks_late disabled. Do not
# quietly rely on "it probably won't happen".
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Fair dispatch. The default (4) lets one worker reserve four payroll runs and
# sit on three of them while a second idle worker starves.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200      # bounds slow memory growth
CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True

CELERY_TASK_TIME_LIMIT = 15 * 60             # hard kill
CELERY_TASK_SOFT_TIME_LIMIT = 13 * 60        # raises SoftTimeLimitExceeded
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_DEFAULT_RETRY_DELAY = 30
CELERY_TASK_MAX_RETRIES = 5
CELERY_BROKER_TRANSPORT_OPTIONS = {
    # Must exceed the longest task, or Redis redelivers a task that is still
    # running and two workers process the same payroll run concurrently.
    "visibility_timeout": 3600,
    "max_retries": 3,
}
CELERY_RESULT_EXPIRES = 60 * 60 * 24
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

#: Per-queue worker settings applied by the deployment (see docker-compose /
#: Makefile). The payroll queue runs with --prefetch-multiplier=1 --concurrency=2
#: because a payroll run holds row locks on every payslip in the run.
CELERY_QUEUE_CONCURRENCY = {
    "default": 8,
    "payments": 4,
    "payroll": 2,
    "reports": 2,
    "notifications": 8,
}


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
# EVERY CACHE KEY IS TENANT-NAMESPACED BY CONSTRUCTION.
#
# ``apps.core.cache.tenant_key_func`` prefixes the active tenant id from the
# ContextVar onto the key, and RAISES if no tenant is bound while writing to
# the ``default`` cache. Without it, `cache.set("ar_aging", ...)` in tenant A
# is served to tenant B on the next request — a silent, total confidentiality
# failure that no test with a single tenant will ever catch.
#
# The ``shared`` cache is the deliberate exception (currency lists, country
# codes, the permission catalogue) and uses the plain key function.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": config("REDIS_CACHE_URL", default="redis://localhost:6379/2"),
        "KEY_FUNCTION": "apps.core.cache.tenant_key_func",
        "KEY_PREFIX": "erp",
        "TIMEOUT": 300,
        # Django's RedisCache forwards unknown OPTIONS straight to
        # ConnectionPool.from_url(), so pool settings go here flat.
        # Nesting them under a "pool_class_kwargs" key reaches the
        # Connection constructor instead and raises TypeError on connect.
        "OPTIONS": {"max_connections": 50},
    },
    "shared": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": config("REDIS_SHARED_CACHE_URL", default="redis://localhost:6379/3"),
        "KEY_PREFIX": "erp-shared",
        "TIMEOUT": 60 * 60 * 24,
    },
}

SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_COOKIE_NAME = "erp_session"

#: Distributed lock namespace (payroll run, period close, sequence allocation).
LOCK_REDIS_URL = config("REDIS_LOCK_URL", default="redis://localhost:6379/4")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
# Django 5 STORAGES dict. Attachments (invoice PDFs, expense receipts, payslip
# PDFs) are PRIVATE: served through signed, short-lived URLs generated by
# apps.core.storage, never by a public bucket. A payslip in a public bucket is
# a data-protection incident with an object key an attacker can enumerate.
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": config("S3_BUCKET", default="erp-media-dev"),
            "region_name": config("S3_REGION", default="eu-central-1"),
            "endpoint_url": config("S3_ENDPOINT_URL", default=None),
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": 900,  # 15 minutes
            "file_overwrite": False,    # never clobber an existing receipt
            "signature_version": "s3v4",
        },
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}

STATIC_URL = "/static/"
STATIC_ROOT = BACKEND_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BACKEND_DIR / "media"

#: Uploads are capped and content-type sniffed; a 2 GB "receipt" is an easy DoS.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000
ALLOWED_UPLOAD_CONTENT_TYPES = [
    "application/pdf", "image/png", "image/jpeg", "image/webp",
    "text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]


# ---------------------------------------------------------------------------
# Internationalisation — English + Arabic, LTR + RTL
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("ar", "العربية"),
]
#: Locales whose UI must be mirrored. The API returns this in /api/v1/i18n/
#: so the web and mobile clients set `dir="rtl"` from one source of truth
#: rather than each hard-coding a list.
RTL_LANGUAGES = ["ar", "he", "fa", "ur"]

LOCALE_PATHS = [BACKEND_DIR / "locale"]
USE_I18N = True
USE_L10N = True
USE_TZ = True
TIME_ZONE = "UTC"  # storage TZ; display TZ comes from Tenant.timezone

#: Arabic-Indic digits are a *presentation* choice. Numbers are stored and
#: transmitted in ASCII digits; converting at the API boundary would make
#: amounts unparseable by Decimal on the client.
USE_ARABIC_NUMERALS_IN_PDF = config("USE_ARABIC_NUMERALS_IN_PDF", default=False, cast=bool)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Structured JSON logs with a correlation filter that stamps every record with
# request_id / tenant_id / user_id pulled from the ContextVars.
#
# Failure prevented: "a customer says their invoice total was wrong at 14:02".
# Without tenant+request correlation you are grepping 40 GB of logs by
# timestamp. With it, one query returns the request, its SQL, and the Celery
# tasks it spawned (the request id is propagated into task headers).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation": {"()": "apps.core.logging.CorrelationFilter"},
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
    },
    "formatters": {
        "json": {
            "()": "structlog.stdlib.ProcessorFormatter",
            "processor": "structlog.processors.JSONRenderer",
        },
        "console": {
            "format": "%(asctime)s %(levelname)-8s [%(request_id)s|%(tenant_id)s] "
                      "%(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["correlation"],
            "formatter": "console",
        },
        "json": {
            "class": "logging.StreamHandler",
            "filters": ["correlation"],
            "formatter": "json",
        },
    },
    "root": {"handlers": ["console"], "level": config("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        # SQL logging is opt-in; at DEBUG it prints every parameter, which in
        # this system includes salaries and bank details.
        "django.db.backends": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "celery": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # The audit logger is never silenced and never sampled.
        "erp.audit": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "erp.ledger": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "erp.security": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}


# ---------------------------------------------------------------------------
# CORS / CSRF
# ---------------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="http://localhost:3000", cast=Csv())
CORS_ALLOW_CREDENTIALS = True  # the web client's refresh cookie is httpOnly
CORS_ALLOW_HEADERS = [
    "accept", "authorization", "content-type", "origin", "user-agent",
    "x-csrftoken", "x-requested-with",
    "x-tenant-id",        # explicit tenant selection for multi-tenant users
    "idempotency-key",    # client-generated UUID; see the mobile outbox
    "x-request-id",
    "accept-language",
]
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="http://localhost:3000", cast=Csv())
CSRF_COOKIE_NAME = "erp_csrftoken"


# ---------------------------------------------------------------------------
# Domain / integration settings
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY", default="")
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET", default="")

DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="no-reply@example.com")
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

#: How long an Idempotency-Key is honoured. Must exceed the mobile outbox's
#: maximum offline window, or a replay after a long flight creates a duplicate.
IDEMPOTENCY_KEY_TTL_SECONDS = 60 * 60 * 24 * 7

#: Hard ceiling on report row counts; beyond this the API returns a job id and
#: the result is delivered as a file.
REPORT_SYNC_ROW_LIMIT = 20_000

APPEND_SLASH = True
os.environ.setdefault("PGTZ", "UTC")
