"""Seed the HR master data a new tenant cannot function without.

Why this exists
---------------
``seed_chart_of_accounts`` already treats the ledger's master data as part of
provisioning rather than a convenience, for a stated reason: a tenant missing
a role the services resolve cannot post at all. The HR side had no equivalent,
and the consequence was the same shape — a brand-new tenant reached the "New
Leave Request" form, found an empty Leave Type dropdown, and could not submit.
The screen was not broken; there was simply nothing to choose, and nothing
told the user that or offered a way to fix it.

Overtime types are seeded for the same reason plus one more: an overtime slip
with no type cannot be priced, and payroll refuses a type with no payroll
component, so a tenant that never coded these finds out at approval time.

Idempotent, keyed on ``(tenant, code)`` — the same natural key the uniqueness
constraints use. Re-running refreshes names and leaves everything else alone,
so it is safe to call on an existing tenant that is only missing some of it.

Deliberately *not* seeded: salary structures and payroll components. Those
encode a company's actual pay policy, and a plausible-looking default package
that nobody chose is worse than an empty list — it gets assigned to staff and
discovered at the first payslip.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.tenancy_context import bind_database_session, tenant_context
from apps.hr.models import LeaveType, OvertimeType, Shift
from apps.tenancy.models import Tenant

#: Statutory-ish minimum most jurisdictions expect, and the three every HR
#: system needs on day one. Rates are conservative defaults a tenant is
#: expected to adjust, not legal advice.
LEAVE_TYPES: tuple[dict[str, Any], ...] = (
    {
        "code": "ANNUAL", "name": "Annual leave", "is_paid": True,
        "accrual_rate_days": Decimal("1.75"),     # 21 days a year
        "max_balance_days": Decimal("42"),        # two years' accrual
        "carry_over_limit_days": Decimal("21"),
        "min_notice_days": 7, "affects_payroll": False,
    },
    {
        "code": "SICK", "name": "Sick leave", "is_paid": True,
        "accrual_rate_days": Decimal("0.5"),
        "max_balance_days": Decimal("30"),
        # Evidence after three days is the common threshold; a tenant that
        # wants none sets it to 0 rather than deleting the type.
        "requires_attachment_after_days": 3,
        "affects_payroll": False,
    },
    {
        "code": "UNPAID", "name": "Unpaid leave", "is_paid": False,
        "accrual_rate_days": Decimal("0"),
        "max_balance_days": Decimal("0"),
        "allow_negative_balance": True,
        # The one that must reach payroll: unpaid days prorate the salary.
        "affects_payroll": True,
        "requires_hr_approval": True,
    },
)

SHIFTS: tuple[dict[str, Any], ...] = (
    {
        "code": "MORNING", "name": "Standard morning",
        "start_time": time(9, 0), "end_time": time(17, 0),
        "break_minutes": 60, "expected_hours_per_day": Decimal("8"),
        "overtime_after_hours": Decimal("8"), "late_grace_minutes": 15,
    },
    {
        "code": "NIGHT", "name": "Night shift",
        "start_time": time(22, 0), "end_time": time(6, 0),
        "crosses_midnight": True,
        "break_minutes": 60, "expected_hours_per_day": Decimal("8"),
        "overtime_after_hours": Decimal("8"), "late_grace_minutes": 15,
    },
)

#: Multipliers only. The payroll component each type pays through is left
#: unset on purpose: it decides which expense account the money hits, and
#: guessing that is how overtime lands in the wrong cost centre for a year.
#: `compute_payslip` refuses a type with no component and says so.
OVERTIME_TYPES: tuple[dict[str, Any], ...] = (
    {"code": "WEEKDAY", "name": "Weekday overtime", "multiplier": Decimal("1.5")},
    {"code": "WEEKEND", "name": "Weekend overtime", "multiplier": Decimal("2.0")},
    {"code": "HOLIDAY", "name": "Public holiday overtime", "multiplier": Decimal("2.5")},
)


def seed_tenant_defaults(tenant_id) -> dict[str, int]:
    """Create the default HR master data for one tenant. Returns what it made.

    Callable from the signup flow as well as the command line — provisioning a
    tenant and provisioning it *correctly* should not be two different code
    paths.
    """
    created = {"leave_types": 0, "shifts": 0, "overtime_types": 0}

    with tenant_context(tenant_id):
        bind_database_session(tenant_id)

        for spec in LEAVE_TYPES:
            _, made = LeaveType.objects.get_or_create(
                tenant_id=tenant_id, code=spec["code"],
                defaults={k: v for k, v in spec.items() if k != "code"},
            )
            created["leave_types"] += int(made)

        for spec in SHIFTS:
            _, made = Shift.objects.get_or_create(
                tenant_id=tenant_id, code=spec["code"],
                defaults={k: v for k, v in spec.items() if k != "code"},
            )
            created["shifts"] += int(made)

        for spec in OVERTIME_TYPES:
            _, made = OvertimeType.objects.get_or_create(
                tenant_id=tenant_id, code=spec["code"],
                defaults={k: v for k, v in spec.items() if k != "code"},
            )
            created["overtime_types"] += int(made)

    return created


class Command(BaseCommand):
    help = "Seed default leave types, shifts and overtime types for a tenant."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--tenant", help="Tenant id or slug. Omit with --all to do every tenant."
        )
        parser.add_argument(
            "--all", action="store_true",
            help="Seed every active tenant. Idempotent, so safe to re-run.",
        )

    @transaction.atomic
    def handle(self, *args: Any, **opts: Any) -> None:
        if not opts.get("tenant") and not opts.get("all"):
            raise CommandError("Pass --tenant <id|slug> or --all.")

        if opts.get("all"):
            tenants = list(Tenant.objects.filter(status=Tenant.Status.ACTIVE))
        else:
            raw = opts["tenant"]
            tenant = Tenant.objects.filter(slug=raw).first()
            if tenant is None:
                tenant = Tenant.objects.filter(id=raw).first()
            if tenant is None:
                raise CommandError(f"No tenant matches {raw!r}.")
            tenants = [tenant]

        for tenant in tenants:
            made = seed_tenant_defaults(tenant.id)
            self.stdout.write(
                f"{tenant.slug}: +{made['leave_types']} leave types, "
                f"+{made['shifts']} shifts, +{made['overtime_types']} overtime types"
            )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded defaults for {len(tenants)} tenant(s)."
        ))
