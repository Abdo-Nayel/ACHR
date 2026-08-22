"""Turn approved, un-invoiced project time into a draft sales invoice.

This was 160 lines inside ``ProjectViewSet.create_invoice`` — a cross-app
orchestration (projects → sales → accounting) living in an HTTP handler, which
meant it could not be called from a Celery task, a management command or a
scheduled billing run. It is business logic, so it belongs in a service. The
viewset is now a thin adapter over :func:`create_invoice_from_time`.

The guarantees the original documented are preserved exactly:

* One ``transaction.atomic`` — a draft whose lines exist but whose timesheet
  entries were never linked would be billed again on the next run.
* ``select_for_update`` on the entries — two concurrent "invoice this project"
  clicks otherwise read the same unbilled set and bill the hours twice.
* ``TimesheetEntry.invoice_line`` (a OneToOne) is set inside the lock, so the
  database itself refuses a second attachment even if the guards above failed.

The invoice is left in DRAFT: numbering and GL posting happen at
``POST /invoices/{id}/issue``, so a reviewer sees the lines before the customer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.fields import ZERO
from apps.projects.models import Project, TimesheetEntry


@dataclass(frozen=True, slots=True)
class InvoiceFromTime:
    """What the caller needs to report back: the draft and how it was built."""

    invoice: object
    line_count: int
    entry_count: int


def create_invoice_from_time(
    project_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    issue_date: Optional[date] = None,
    due_date: Optional[date] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    group_by_task: bool = False,
    notes: str = "",
) -> InvoiceFromTime:
    """Build a DRAFT invoice from a project's approved, billable, un-invoiced time."""
    # Local imports: sales and accounting import the posting engine, and a
    # module-level import here would close a cycle at app-loading time.
    from apps.accounting.models import Account
    from apps.core.exceptions import DomainError
    from apps.sales.models import Invoice, InvoiceLine

    project = Project.objects.get(pk=project_id)

    if project.customer_id is None:
        raise DomainError(
            "This project has no customer, so its time cannot be invoiced."
        )
    if not project.is_billable:
        raise DomainError("This project is marked non-billable.")

    issue = issue_date or timezone.localdate()
    due = due_date or issue

    with transaction.atomic():
        entries_q = Q(
            project=project,
            status=TimesheetEntry.Status.APPROVED,
            is_billable=True,
            invoice_line__isnull=True,
        )
        if date_from:
            entries_q &= Q(work_date__gte=date_from)
        if date_to:
            entries_q &= Q(work_date__lte=date_to)

        entries = list(
            TimesheetEntry.objects.filter(entries_q)
            # ``of=("self",)`` locks only the timesheet rows: ``task`` is a
            # nullable FK, so ``select_related`` makes it a LEFT OUTER JOIN, and
            # PostgreSQL refuses a plain ``FOR UPDATE`` on an outer join's
            # nullable side (the endpoint 500'd on every such entry before).
            .select_for_update(of=("self",))
            .select_related("employee", "task")
            .order_by("work_date", "created_at")
        )
        if not entries:
            raise DomainError(
                "There is no approved, un-invoiced billable time on this "
                "project for the requested period."
            )

        revenue_account = (
            Account.objects.filter(system_key="service_revenue").first()
            or Account.objects.filter(system_key="sales_revenue").first()
        )
        if revenue_account is None:
            raise DomainError(
                "No service revenue account is configured for this "
                "organisation; seed the chart of accounts first."
            )

        invoice = Invoice.objects.create(
            tenant_id=project.tenant_id,
            customer_id=project.customer_id,
            issue_date=issue,
            due_date=due,
            currency=project.currency,
            project=project,
            status=Invoice.Status.DRAFT,
            notes=notes,
            created_by_id=user_id,
            updated_by_id=user_id,
        )

        groups: dict = {}
        for entry in entries:
            key = entry.task_id if group_by_task else entry.pk
            groups.setdefault(key, []).append(entry)

        subtotal = ZERO
        for index, (_, members) in enumerate(groups.items(), start=1):
            hours = sum((Decimal(e.hours) for e in members), ZERO)
            amount = sum((Decimal(e.billable_amount) for e in members), ZERO)
            # Unit price is derived from the group's own total so that
            # quantity * unit_price == amount exactly; averaging rates across
            # entries would leave a rounding residue the ledger then refuses.
            unit_price = (amount / hours) if hours else ZERO
            first = members[0]
            label = (
                first.task.name if first.task_id else first.description
            ) or f"Professional services — {project.name}"

            line = InvoiceLine.objects.create(
                tenant_id=project.tenant_id,
                invoice=invoice,
                line_number=index,
                description=label[:500],
                quantity=hours,
                unit_price=unit_price,
                line_subtotal=amount,
                line_tax=ZERO,
                line_total=amount,
                income_account=revenue_account,
                project=project,
                created_by_id=user_id,
                updated_by_id=user_id,
            )
            subtotal += amount

            now = timezone.now()
            for entry in members:
                # OneToOne: only the first group may claim an entry. Set before
                # the transition so a failure cannot leave an INVOICED entry
                # with no invoice line.
                entry.invoice_line = line
                entry.invoiced_at = now
                entry.status = TimesheetEntry.Status.INVOICED
                entry.updated_by_id = user_id
                entry.save(update_fields=[
                    "invoice_line", "invoiced_at", "status",
                    "updated_by", "updated_at",
                ])

        invoice.subtotal_amount = subtotal
        invoice.total_amount = subtotal
        invoice.amount_due = subtotal
        invoice.save(update_fields=[
            "subtotal_amount", "total_amount", "amount_due", "updated_at",
        ])

    return InvoiceFromTime(invoice=invoice, line_count=len(groups), entry_count=len(entries))
