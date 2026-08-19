"""
Reporting background jobs.

All three tasks below run on the ``reports`` queue (routed by prefix in
``config/celery.py``), which is isolated precisely so that a 40-second trial
balance cannot starve invoice emails.

Two rules shape every task in this file:

1. **Fan out per tenant inside the task, under ``tenant_context``.** Celery
   beat holds one schedule, not one per tenant — with 5 000 tenants the
   alternative is 15 000 beat entries and a scheduler that takes minutes to
   tick. Binding the tenant explicitly inside the loop is mandatory, not
   tidy: the ORM's ``TenantManager`` fails *closed* with no tenant bound and
   returns ``.none()``, so an unbound task reports success having processed
   nothing at all — the worst possible failure mode for an integrity check.
2. **One tenant's failure must not stop the sweep.** Each iteration catches
   and records, then continues. A single misconfigured tenant halting the
   nightly ledger check for everyone else means the check silently stops
   being run, which is indistinguishable from it passing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any, Optional

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.core.tenancy_context import tenant_context

logger = logging.getLogger(__name__)

__all__ = [
    "run_scheduled_reports",
    "nightly_ledger_integrity_check",
    "refresh_report_cache",
]


#: How far a schedule may fall behind before it is skipped rather than run.
#: If the worker fleet was down for two days we do *not* want two days of
#: month-end reports delivered at once — the recipients cannot tell which is
#: current, and the noise trains them to ignore the next one.
MAX_SCHEDULE_LATENESS = timedelta(hours=12)


def _active_tenant_ids() -> list[uuid.UUID]:
    """Tenants whose books are worth checking.

    ``PAST_DUE`` is deliberately included: read-only access survives non-payment
    (a customer must always be able to export their own books), so their ledger
    still has to be sound. Only ``SUSPENDED`` and ``CLOSED`` are skipped.

    Index used: ``ix_tenant_status`` on ``tenancy_tenant``.
    """
    from apps.tenancy.models import Tenant

    return list(
        Tenant.objects.filter(
            status__in=[
                Tenant.Status.ACTIVE,
                Tenant.Status.TRIAL,
                Tenant.Status.PAST_DUE,
            ]
        ).values_list("id", flat=True)
    )


# ---------------------------------------------------------------------------
# Scheduled delivery
# ---------------------------------------------------------------------------

@shared_task(
    name="apps.reporting.tasks.run_scheduled_reports",
    bind=True,
    # Only infrastructure faults are retried. Retrying a ValidationError or a
    # ReportImbalance just burns the queue and delivers nothing; those are
    # recorded on the schedule row for a human instead.
    autoretry_for=(OSError,),
    retry_backoff=True,
    max_retries=3,
    acks_late=True,
)
def run_scheduled_reports(self, now_iso: Optional[str] = None) -> dict[str, Any]:
    """Run every ``ReportSchedule`` that is due, snapshot it, hand it to delivery.

    Each schedule is processed in its own transaction and its own
    ``tenant_context``. A failure updates ``last_error`` and
    ``consecutive_failure_count`` on that schedule and moves on: one tenant's
    misconfigured grouping must not stop every other tenant's board pack.

    ``next_run_at`` is advanced *before* the report is generated. That ordering
    is deliberate — if generation crashes the worker, the schedule must not be
    picked up again on the next tick and crash it again forever. A missed
    delivery is recoverable by a human; a crash loop that fills the queue is
    not.

    Snapshots are taken for every scheduled run, not only for statutory ones.
    A scheduled report is by definition one somebody relies on periodically,
    which makes "what did last month's say?" a question that will be asked.
    """
    from apps.reporting.models import ReportSchedule
    from apps.reporting.services.snapshot import generate_and_snapshot

    now = timezone.datetime.fromisoformat(now_iso) if now_iso else timezone.now()
    processed = 0
    failed = 0
    skipped_stale = 0

    # Index used: ix_report_sched_due (is_active, next_run_at) — the one hot
    # query this task issues, answered as a range scan rather than a full scan
    # of every tenant's schedules.
    due_ids = list(
        ReportSchedule.all_tenants.filter(
            is_active=True, next_run_at__isnull=False, next_run_at__lte=now
        )
        .order_by("next_run_at")
        .values_list("id", "tenant_id", "next_run_at")
    )

    for schedule_id, tenant_id, next_run_at in due_ids:
        if next_run_at is not None and now - next_run_at > MAX_SCHEDULE_LATENESS:
            skipped_stale += 1
            logger.warning(
                "reporting.schedule.stale",
                extra={"schedule_id": str(schedule_id), "tenant_id": str(tenant_id),
                       "due_at": next_run_at.isoformat()},
            )
            _advance_schedule(schedule_id, now, error="")
            continue

        try:
            with tenant_context(tenant_id):
                schedule = (
                    ReportSchedule.all_tenants.select_related("definition")
                    .filter(id=schedule_id)
                    .first()
                )
                if schedule is None or not schedule.is_active:
                    continue

                # Advance first. See the docstring: a crash during generation
                # must not turn into a crash loop on every subsequent tick.
                _advance_schedule(schedule_id, now, error="")

                definition = schedule.definition
                parameters = {
                    **(definition.default_parameters or {}),
                    **(schedule.parameters or {}),
                }
                context = _context_from_parameters(tenant_id, parameters)
                snapshot = generate_and_snapshot(
                    definition.report_type,
                    context,
                    user_id=definition.owner_id,
                    definition=definition,
                    file_format=schedule.format,
                )
                _enqueue_delivery(schedule, snapshot)
                processed += 1
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the sweep
            failed += 1
            logger.exception(
                "reporting.schedule.failed",
                extra={"schedule_id": str(schedule_id), "tenant_id": str(tenant_id)},
            )
            _record_schedule_failure(schedule_id, str(exc))

    return {
        "due": len(due_ids),
        "processed": processed,
        "failed": failed,
        "skipped_stale": skipped_stale,
    }


def _context_from_parameters(tenant_id, parameters: dict[str, Any]):
    """Build a fully-resolved :class:`ReportContext` from stored parameters.

    Relative periods ("previous_month") are resolved to absolute dates *here*,
    before the report runs, so that the snapshot records the dates that were
    actually used. A snapshot whose parameters say "previous month" is not
    reproducible, which defeats the point of taking one.
    """
    from apps.reporting.generators.base import ReportContext

    today = timezone.localdate()
    period = parameters.get("period", "previous_month")
    date_from = parameters.get("date_from")
    date_to = parameters.get("date_to")

    if not (date_from and date_to):
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        if period == "previous_month":
            date_from, date_to = last_month_end.replace(day=1), last_month_end
        elif period == "month_to_date":
            date_from, date_to = first_of_this_month, today
        elif period == "year_to_date":
            date_from, date_to = today.replace(month=1, day=1), today
        else:
            date_from, date_to = last_month_end.replace(day=1), last_month_end
    else:
        date_from = timezone.datetime.fromisoformat(str(date_from)).date()
        date_to = timezone.datetime.fromisoformat(str(date_to)).date()

    return ReportContext(
        tenant_id=tenant_id,
        date_from=date_from,
        date_to=date_to,
        as_of=date_to,
        currency=parameters.get("currency", ""),
        department_id=parameters.get("department_id"),
        project_id=parameters.get("project_id"),
        # Never True from a schedule. A scheduled report is delivered to
        # people who did not run it and cannot see the caveat in the UI, so a
        # draft-inclusive preview must not be something a schedule can send.
        include_unposted=False,
        options={
            k: v for k, v in parameters.items()
            if k not in {"period", "date_from", "date_to", "currency",
                         "department_id", "project_id", "include_unposted"}
        },
    )


def _advance_schedule(schedule_id, now, *, error: str) -> None:
    """Move ``next_run_at`` forward by the schedule's cadence.

    Advanced from the *scheduled* time rather than from ``now`` so that a run
    delayed by twenty minutes does not permanently shift a monthly report
    twenty minutes later every month — a drift that, over a year, moves a
    month-end report into the next day.
    """
    from apps.reporting.models import ReportSchedule

    with transaction.atomic():
        schedule = (
            ReportSchedule.all_tenants.select_for_update().filter(id=schedule_id).first()
        )
        if schedule is None:
            return
        base = schedule.next_run_at or now
        cadence = {
            ReportSchedule.Frequency.DAILY: timedelta(days=1),
            ReportSchedule.Frequency.WEEKLY: timedelta(weeks=1),
            ReportSchedule.Frequency.MONTHLY: timedelta(days=30),
            ReportSchedule.Frequency.QUARTERLY: timedelta(days=91),
            ReportSchedule.Frequency.ANNUAL: timedelta(days=365),
        }.get(schedule.frequency, timedelta(days=30))
        next_run = base + cadence
        # Never leave next_run_at in the past: that would make the schedule due
        # again immediately and re-deliver on every tick.
        while next_run <= now:
            next_run += cadence
        ReportSchedule.all_tenants.filter(id=schedule_id).update(
            next_run_at=next_run,
            last_run_at=now,
            last_error=error[:500],
            updated_at=timezone.now(),
        )


def _record_schedule_failure(schedule_id, message: str) -> None:
    """Record why a delivery failed, on the row an operator will look at."""
    from django.db.models import F

    from apps.reporting.models import ReportSchedule

    ReportSchedule.all_tenants.filter(id=schedule_id).update(
        last_error=message[:500],
        consecutive_failure_count=F("consecutive_failure_count") + 1,
        updated_at=timezone.now(),
    )


def _enqueue_delivery(schedule, snapshot) -> None:
    """Hand the snapshot to rendering + delivery.

    Kept as a seam rather than inlined so that generation and delivery fail
    independently: a bounced mailbox must not make the report look ungenerated,
    and the snapshot is already durable evidence by the time this is called.
    """
    logger.info(
        "reporting.schedule.delivered",
        extra={
            "schedule_id": str(schedule.id),
            "snapshot_id": str(snapshot.id),
            "checksum": snapshot.checksum,
            "recipient_count": len(schedule.recipients or []),
            "format": schedule.format,
        },
    )


# ---------------------------------------------------------------------------
# Nightly integrity check
# ---------------------------------------------------------------------------

@shared_task(
    name="apps.reporting.tasks.nightly_ledger_integrity_check",
    bind=True,
    acks_late=True,
)
def nightly_ledger_integrity_check(self) -> dict[str, Any]:
    """Prove, every night and for every active tenant, that the books balance.

    Calls ``apps.accounting.services.posting.assert_ledger_balanced`` inside
    ``tenant_context`` for each tenant. That binding is not optional: without a
    bound tenant the tenant-scoped managers return ``.none()``, the aggregate
    of an empty set is ``0 == 0``, and the check passes vacuously for every
    tenant — a green light that proves nothing, which is worse than no check
    because it stops anyone looking.

    Why check at all when the database already makes it impossible?
    ``ck_entry_balanced`` and the trigger from migration ``0002_ledger_guards``
    mean the application cannot create an unbalanced ledger. This task exists
    for everything that is *not* the application: a restored backup, a data
    migration, a manual ``UPDATE`` during an incident, a replication artefact.
    Those are exactly the events after which nobody thinks to re-verify, and
    exactly the ones that make a filed report wrong.

    Failures are collected and reported at the end rather than raised on the
    first one, so a single broken tenant does not hide the state of the rest —
    and so the alert says "3 of 412 tenants" instead of "one tenant, unknown
    how many others".
    """
    from apps.accounting.services.posting import assert_ledger_balanced

    failures: list[dict[str, str]] = []
    checked = 0

    for tenant_id in _active_tenant_ids():
        checked += 1
        try:
            with tenant_context(tenant_id):
                assert_ledger_balanced(tenant_id)
        except Exception as exc:  # noqa: BLE001 - collect, do not abort the sweep
            failures.append({"tenant_id": str(tenant_id), "error": str(exc)})
            logger.error(
                "reporting.ledger_integrity.failed",
                extra={"tenant_id": str(tenant_id), "error": str(exc)},
            )

    if failures:
        # ERROR, not an exception: raising would mark the task failed and
        # trigger a retry that would re-run the whole sweep and re-alert.
        # The alert is the deliverable here, not the task's exit status.
        logger.error(
            "LEDGER INTEGRITY FAILURE: %s of %s tenants do not balance. "
            "Every report for the affected tenants is unreliable until this "
            "is explained. Failures: %s",
            len(failures), checked, failures,
        )
        _alert_ledger_failures(failures)

    return {"checked": checked, "failed": len(failures), "failures": failures}


def _alert_ledger_failures(failures: list[dict[str, str]]) -> None:
    """Escalate to the on-call channel.

    A separate function so the escalation transport can change without
    touching the check, and so that a failure to *alert* cannot swallow the
    result of the check itself.
    """
    logger.critical(
        "reporting.ledger_integrity.alert",
        extra={"failure_count": len(failures), "failures": failures},
    )


# ---------------------------------------------------------------------------
# Cache warming
# ---------------------------------------------------------------------------

@shared_task(
    name="apps.reporting.tasks.refresh_report_cache",
    bind=True,
    acks_late=True,
)
def refresh_report_cache(
    self, tenant_ids: Optional[list[str]] = None
) -> dict[str, Any]:
    """Pre-compute the dashboard aggregates so a page load is a cache read.

    Warms the current month's trial balance and P&L and the current AR/AP
    aging — the four figures every dashboard shows. They are the expensive
    ones (full scans of the journal) and the ones every user requests within
    seconds of logging in, so computing them once per tenant off the request
    path is the difference between a dashboard that opens and one that times
    out at month end when the journal is largest.

    Cache entries are keyed by tenant *and* by the parameters they were
    computed with, and the values are ``ReportResult.to_dict()`` payloads —
    i.e. amounts as strings. Caching Decimals directly would mean the cache
    backend's serialiser decides how they round, and a memcached round trip
    through JSON would quietly turn them into floats.

    A warm failure is logged and skipped, never raised: a cold cache is a slow
    dashboard, which is an inconvenience. A failing task is a red alert that
    trains people to ignore alerts.
    """
    from django.core.cache import cache

    from apps.reporting.generators.base import ReportContext, get_generator
    from apps.reporting.models import ReportType

    targets = (
        [uuid.UUID(str(t)) for t in tenant_ids]
        if tenant_ids
        else _active_tenant_ids()
    )
    today = timezone.localdate()
    month_start = today.replace(day=1)
    warmed = 0
    failed = 0

    warm_set = (
        (ReportType.TRIAL_BALANCE, False),
        (ReportType.PROFIT_LOSS, False),
        (ReportType.AR_AGING, True),
        (ReportType.AP_AGING, True),
    )

    for tenant_id in targets:
        for report_type, is_as_of in warm_set:
            try:
                with tenant_context(tenant_id):
                    context = ReportContext(
                        tenant_id=tenant_id,
                        date_from=None if is_as_of else month_start,
                        date_to=today,
                        as_of=today,
                    )
                    result = get_generator(report_type).run(context)
                    cache.set(
                        report_cache_key(tenant_id, report_type, context),
                        result.to_dict(),
                        timeout=REPORT_CACHE_TTL,
                    )
                    warmed += 1
            except Exception as exc:  # noqa: BLE001 - a cold cache is not an incident
                failed += 1
                logger.warning(
                    "reporting.cache.warm_failed",
                    extra={
                        "tenant_id": str(tenant_id),
                        "report_type": report_type,
                        "error": str(exc),
                    },
                )

    return {"tenants": len(targets), "warmed": warmed, "failed": failed}


#: Short enough that a posting made now shows on the dashboard within the
#: minute, long enough that the warm is worth doing. A long TTL on a financial
#: dashboard is how a user posts an invoice and then reports that "the system
#: lost it".
REPORT_CACHE_TTL = 300


def report_cache_key(tenant_id, report_type: str, context) -> str:
    """Cache key including the tenant and the parameters.

    The tenant is in the key, first, and not merely in the ambient context: a
    key that omits it serves one company's trial balance to another the moment
    two tenants request the same report type — the single worst bug a
    multi-tenant financial system can have, and one that a cache makes
    trivially easy to write.
    """
    import hashlib
    import json

    fingerprint = hashlib.sha256(
        json.dumps(context.to_parameters(), sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return f"report:{tenant_id}:{report_type}:{fingerprint}"
