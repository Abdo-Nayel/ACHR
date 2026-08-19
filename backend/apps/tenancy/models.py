"""
Tenancy: the customer organisation and its subscription/config envelope.

Isolation model
---------------
Single database, single schema, ``tenant_id`` on every business row, enforced
by **PostgreSQL Row-Level Security** (see
``apps/tenancy/migrations/0002_rls_policies.py``).

Why not schema-per-tenant: at a few thousand tenants, `migrate` has to run
once per schema, `pg_dump` becomes unusable, the catalog bloats to hundreds
of thousands of relations, and the query planner's shared cache thrashes.
`tenant_id` + RLS gives equivalent isolation guarantees (the database, not
the application, refuses the row) with one migration and one connection pool.

Why RLS *and* an ORM manager: defence in depth. The ORM manager gives clean
errors and good query plans; RLS is the backstop that still holds when a
developer writes `.raw()`, a Celery task forgets to bind context, or an
analyst opens psql.
"""

from __future__ import annotations

import uuid

from django.core.validators import MinLengthValidator, RegexValidator
from django.db import models

from apps.core.fields import MoneyField, RateField
from apps.core.models import Currency, TimeStampedModel, UUIDModel


class Tenant(UUIDModel, TimeStampedModel):
    """One customer organisation (a company using the product).

    This row is *not* itself tenant-scoped — it is the scope. It lives
    outside RLS and is readable only through the platform-admin path or by
    joining from an authenticated membership.
    """

    class Status(models.TextChoices):
        TRIAL = "trial", "Trial"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        SUSPENDED = "suspended", "Suspended"
        CLOSED = "closed", "Closed"

    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=255, blank=True)
    #: URL-safe identifier used for sub-domain routing (acme.app.example.com).
    slug = models.SlugField(
        max_length=63, unique=True,
        validators=[MinLengthValidator(3), RegexValidator(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")],
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.TRIAL, db_index=True
    )

    country = models.CharField(max_length=2, help_text="ISO-3166 alpha-2")
    timezone = models.CharField(max_length=64, default="UTC")
    #: The currency the general ledger is kept in. Immutable after the first
    #: posted journal entry — changing it would invalidate every historical
    #: report. Enforced in `apps.tenancy.services.settings.change_base_currency`.
    base_currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.EGP
    )
    tax_registration_number = models.CharField(max_length=64, blank=True)

    #: First month of the tenant's fiscal year (1 = January, 7 = July).
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1)

    #: Free-form per-tenant feature flags and branding. JSONB so it is
    #: queryable and indexable without a migration per setting.
    settings = models.JSONField(default=dict, blank=True)

    trial_ends_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tenancy_tenant"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(fiscal_year_start_month__gte=1)
                & models.Q(fiscal_year_start_month__lte=12),
                name="ck_tenant_fiscal_month_range",
            ),
        ]
        indexes = [models.Index(fields=["status"], name="ix_tenant_status")]

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    @property
    def is_operational(self) -> bool:
        """Whether write operations are permitted. Read-only access survives
        `PAST_DUE` so a customer can always export their own books."""
        return self.status in {self.Status.TRIAL, self.Status.ACTIVE}


class TenantDomain(UUIDModel, TimeStampedModel):
    """Custom domains mapped to a tenant, used by the host-header resolver."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="domains")
    domain = models.CharField(max_length=253, unique=True)
    is_primary = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tenancy_domain"
        constraints = [
            # Exactly one primary domain per tenant, expressed as a partial
            # unique index so the DB rejects a second primary rather than
            # trusting application code to keep it consistent.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_primary=True),
                name="uq_domain_one_primary_per_tenant",
            )
        ]


class Subscription(UUIDModel, TimeStampedModel):
    """Billing plan for a tenant. Kept separate from `Tenant` so that plan
    history is append-only and can be reported on."""

    class Plan(models.TextChoices):
        STARTER = "starter", "Starter"
        STANDARD = "standard", "Standard"
        PROFESSIONAL = "professional", "Professional"
        ENTERPRISE = "enterprise", "Enterprise"

    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="subscriptions"
    )
    plan = models.CharField(max_length=20, choices=Plan.choices)
    seats = models.PositiveIntegerField(default=1)
    monthly_amount = MoneyField()
    currency = models.CharField(max_length=3, choices=Currency.choices)
    started_on = models.DateField()
    ended_on = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "tenancy_subscription"
        ordering = ["-started_on"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ended_on__isnull=True)
                | models.Q(ended_on__gte=models.F("started_on")),
                name="ck_subscription_period_order",
            ),
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(ended_on__isnull=True),
                name="uq_subscription_one_open_per_tenant",
            ),
        ]


class TenantAuditLog(UUIDModel):
    """Append-only record of security-relevant actions.

    Deliberately not a `TenantScopedModel`: it must be writable even when the
    tenant context is being *established* (login, tenant switch, impersonation)
    and must never be deletable by tenant users.
    """

    class Action(models.TextChoices):
        LOGIN = "login", "Login"
        LOGIN_FAILED = "login_failed", "Failed login"
        ROLE_GRANTED = "role_granted", "Role granted"
        ROLE_REVOKED = "role_revoked", "Role revoked"
        IMPERSONATION = "impersonation", "Platform admin impersonation"
        EXPORT = "export", "Data export"
        PERIOD_CLOSED = "period_closed", "Accounting period closed"
        ENTRY_REVERSED = "entry_reversed", "Journal entry reversed"
        PAYROLL_APPROVED = "payroll_approved", "Payroll approved"
        SETTING_CHANGED = "setting_changed", "Setting changed"

    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="audit_logs", null=True
    )
    actor_id = models.UUIDField(null=True, blank=True, db_index=True)
    actor_email = models.EmailField(blank=True)  # denormalised: survives user deletion
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.UUIDField(null=True, blank=True)
    #: Before/after snapshot. Never store secrets or full payment card data.
    payload = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "tenancy_audit_log"
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["tenant", "-occurred_at"], name="ix_audit_tenant_time"),
            models.Index(fields=["object_type", "object_id"], name="ix_audit_object"),
        ]
