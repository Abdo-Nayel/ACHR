"""
Celery application, queue routing and the beat schedule.

Queue topology
--------------
Five queues, each with its own worker deployment, because they fail
differently and must not share a failure domain:

``default``        light CRUD side effects, cache warms, audit fan-out.
``payments``       gateway calls and webhook processing. Isolated because a
                   Stripe outage must not stall payroll.
``payroll``        long, lock-heavy, and legally significant. Runs with
                   ``--concurrency=2 --prefetch-multiplier=1`` so a worker
                   never reserves a run it cannot start; a reserved-but-idle
                   payroll run looks like a hang to the customer and holds
                   row locks on every payslip.
``reports``        CPU/IO heavy aggregates. Isolated so a 40-second trial
                   balance cannot starve invoice emails.
``notifications``  email/SMS/push. Highest volume, lowest value; it is the
                   queue we are happy to shed.

Idempotency
-----------
``task_acks_late`` + ``task_reject_on_worker_lost`` (see settings/base.py)
give at-least-once delivery. Every task below is therefore either pure
(read + report), or guarded by a uniqueness key:
``JournalEntry.idempotency_key``, ``PaymentWebhookEvent.gateway_event_id``,
``PayrollRun.status`` transitions. Adding a money-moving task without such a
guard means duplicate postings the first time a worker is OOM-killed.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    before_task_publish,
    setup_logging,
    task_postrun,
    task_prerun,
)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("erp")

# All CELERY_* Django settings become Celery config. One config source, so a
# broker URL cannot drift between the web process and the workers.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Finds apps/<app>/tasks.py in every INSTALLED_APPS entry.
app.autodiscover_tasks()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
# Explicit prefix routing beats per-task decorators: a new task in
# apps.payroll.tasks lands on the payroll queue automatically, and nobody has
# to remember the `queue=` kwarg.
app.conf.task_routes = {
    "apps.payments.tasks.*": {"queue": "payments"},
    "apps.banking.tasks.*": {"queue": "payments"},
    "apps.payroll.tasks.*": {"queue": "payroll"},
    "apps.hr.tasks.accrue_leave*": {"queue": "payroll"},
    "apps.reporting.tasks.*": {"queue": "reports"},
    "apps.notifications.tasks.*": {"queue": "notifications"},
    "apps.sales.tasks.send_payment_reminders": {"queue": "notifications"},
    "*": {"queue": "default"},
}

app.conf.task_queues_declared = [
    "default", "payments", "payroll", "reports", "notifications",
]

# Retry policy for transient broker/gateway failures. Financial tasks set
# their own `autoretry_for` with a narrower exception list — blanket retrying
# a ValidationError just burns the queue.
app.conf.task_annotations = {
    "apps.payments.tasks.*": {"rate_limit": "60/m"},
    "apps.notifications.tasks.*": {"rate_limit": "300/m"},
    "apps.reporting.tasks.*": {"time_limit": 30 * 60, "soft_time_limit": 28 * 60},
}


# ---------------------------------------------------------------------------
# Beat schedule
# ---------------------------------------------------------------------------
# Every periodic task fans out per tenant *inside* the task (it iterates
# active tenants and re-binds ``app.current_tenant`` for each), rather than
# beat holding one schedule per tenant. With 5,000 tenants the alternative is
# 35,000 beat entries and a scheduler that takes minutes to tick.
#
# All times are UTC (CELERY_TIMEZONE). Nightly jobs are staggered so the
# integrity checks do not collide with the drift recompute on the same
# database.
app.conf.beat_schedule = {
    # Moves invoices past due date into OVERDUE and refreshes AR aging
    # buckets. Hourly, not daily: a customer looking at the dashboard at
    # 09:00 in UTC+4 should not see yesterday's aging.
    "refresh-overdue-invoices": {
        "task": "apps.sales.tasks.refresh_overdue_invoices",
        "schedule": crontab(minute=5),  # every hour at :05
        "options": {"queue": "default", "expires": 55 * 60},
    },
    # Dunning. Once a day, early, so reminders land in the customer's morning.
    # `expires` matters: if the worker fleet was down for six hours we do NOT
    # want six days of reminders delivered at once.
    "send-payment-reminders": {
        "task": "apps.sales.tasks.send_payment_reminders",
        "schedule": crontab(hour=6, minute=0),
        "options": {"queue": "notifications", "expires": 6 * 60 * 60},
    },
    # Monthly leave accrual on the 1st. Idempotent per (employee, period):
    # LeaveBalance rows carry a unique (tenant, employee, leave_type, period)
    # key, so a re-run credits nothing twice.
    "accrue-leave": {
        "task": "apps.hr.tasks.accrue_leave",
        "schedule": crontab(day_of_month=1, hour=1, minute=0),
        "options": {"queue": "payroll", "expires": 12 * 60 * 60},
    },
    # Drift check: recompute StockLevel from the StockMovement ledger and
    # report any row that disagrees. The cached level is a denormalisation;
    # this is what proves the denormalisation is still true. It reports and
    # alerts rather than silently "fixing", because a silent fix destroys the
    # evidence of the bug that caused the drift.
    "recompute-stock-levels": {
        "task": "apps.inventory.tasks.recompute_stock_levels",
        "schedule": crontab(hour=2, minute=0),
        "options": {"queue": "reports", "expires": 4 * 60 * 60},
    },
    # The nightly proof that the books are sound: for every tenant and every
    # open period, SUM(debit) == SUM(credit) across all posted lines, and each
    # entry's materialised totals match its lines. The DB trigger makes an
    # unbalanced entry impossible; this catches the case where someone
    # disabled the trigger, restored a bad backup, or ran a data migration.
    "assert-ledger-balanced": {
        "task": "apps.reporting.tasks.nightly_ledger_integrity_check",
        "schedule": crontab(hour=3, minute=0),
        "options": {"queue": "reports", "expires": 4 * 60 * 60},
    },
    # Weekly digest of expiring documents (employee contracts, residency and
    # work permits, vehicle licences, tax certificates). Monday 07:00 so it is
    # actionable during the working week.
    "expire-documents-report": {
        "task": "apps.hr.tasks.expire_documents_report",
        "schedule": crontab(day_of_week=1, hour=7, minute=0),
        "options": {"queue": "notifications", "expires": 24 * 60 * 60},
    },
    # Gateway webhooks that failed processing are parked, not dropped. This
    # replays them with exponential backoff. Safe because every webhook is
    # deduplicated on the gateway event id.
    "retry-failed-webhooks": {
        "task": "apps.payments.tasks.retry_failed_webhooks",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "payments", "expires": 4 * 60},
    },
}


# ---------------------------------------------------------------------------
# Tenant + correlation propagation
# ---------------------------------------------------------------------------

@before_task_publish.connect
def _propagate_tenant_header(headers=None, **extra):
    """Producer side: stamp the caller's tenant/request onto the message.

    The worker's ``task_prerun`` hook reads ``tenant_id`` from the message
    headers — but nothing was ever *writing* it, so the propagation was a
    one-ended pipe. This closes it: whatever tenant the enqueuing code is bound
    to travels with the job. Enqueue from inside ``tenant_context`` (or a
    request) and the worker knows which company the work is for.
    """
    if headers is None:
        return
    from apps.core.tenancy_context import (  # noqa: PLC0415
        get_current_tenant_id,
        get_current_user_id,
    )

    tenant_id = get_current_tenant_id()
    if tenant_id is not None and "tenant_id" not in headers:
        headers["tenant_id"] = str(tenant_id)
    user_id = get_current_user_id()
    if user_id is not None and "user_id" not in headers:
        headers["user_id"] = str(user_id)


@task_prerun.connect
def _bind_task_context(sender=None, task_id=None, task=None, args=None,
                       kwargs=None, **extra):
    """Worker side: re-establish the ORM tenant ContextVar from the headers.

    This sets the ``ContextVar`` the ORM manager reads. It deliberately does
    **not** bind the PostgreSQL session here: ``SET LOCAL`` lives only inside a
    transaction, and this hook returns before the task body runs, so a binding
    made here would be gone by the first query. The durable both-layer binding
    belongs in the task body — every task wraps its work in
    ``tenant_context(...)`` (which now binds the DB session too), so RLS sees
    the tenant. This ContextVar is the fallback that keeps ORM reads scoped
    even before that wrapper is entered.
    """
    from apps.core.tenancy_context import (  # noqa: PLC0415
        _current_tenant_id,
        _current_user_id,
    )

    headers = getattr(getattr(task, "request", None), "headers", None) or {}
    import uuid  # noqa: PLC0415

    tenant_id = headers.get("tenant_id")
    if tenant_id:
        _current_tenant_id.set(uuid.UUID(str(tenant_id)))
    user_id = headers.get("user_id")
    if user_id:
        _current_user_id.set(uuid.UUID(str(user_id)))


@task_postrun.connect
def _clear_task_context(sender=None, task_id=None, task=None, args=None,
                        kwargs=None, retval=None, state=None, **extra):
    """Workers are long-lived; a leftover tenant would be inherited by the
    next task on that worker and write rows into the wrong company."""
    from apps.core.tenancy_context import (  # noqa: PLC0415
        _current_tenant_id,
        _current_user_id,
    )

    _current_tenant_id.set(None)
    _current_user_id.set(None)


@setup_logging.connect
def _configure_logging(**kwargs):
    """Use Django's LOGGING (JSON + correlation filter) instead of Celery's
    own basicConfig, so worker logs are searchable by tenant like web logs."""
    from logging.config import dictConfig  # noqa: PLC0415

    from django.conf import settings  # noqa: PLC0415

    dictConfig(settings.LOGGING)


@app.task(bind=True, name="config.debug_task")
def debug_task(self):  # pragma: no cover - operational smoke test
    return {"request_id": self.request.id, "queue": self.request.delivery_info}
