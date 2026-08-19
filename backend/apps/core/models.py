"""
Abstract base models shared by every business app.

Three invariants are enforced here, once, so that no downstream module has
to remember them:

1. **Tenant scoping.** Every business row carries ``tenant_id``. The default
   manager refuses to return rows outside the active tenant, and PostgreSQL
   Row-Level Security enforces the same rule below the ORM so that a raw
   query, a Celery task or a psql session cannot leak across tenants.
2. **Auditability.** Rows record who created and last touched them.
3. **Immutability of posted records.** Financial documents are never
   hard-deleted; they are voided or reversed. ``delete()`` raises.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import models

from apps.core.tenancy_context import get_current_tenant_id


# ---------------------------------------------------------------------------
# Managers
# ---------------------------------------------------------------------------

class TenantQuerySet(models.QuerySet):
    def for_tenant(self, tenant_id):
        return self.filter(tenant_id=tenant_id)

    def delete(self):  # pragma: no cover - guard rail
        raise PermissionDenied(
            "Bulk delete is disabled on tenant-scoped models. Use archive/void."
        )


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """Implicitly filters to the tenant bound to the current request/task.

    ``use_in_migrations`` is deliberately False: migrations run without a
    tenant context and must see every row.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            # No ambient tenant: return nothing rather than everything.
            # Fail-closed is the only safe default in a shared database.
            return qs.none()
        return qs.filter(tenant_id=tenant_id)


class AllTenantsManager(models.Manager.from_queryset(TenantQuerySet)):
    """Escape hatch for migrations, platform admin and cross-tenant reports.

    Every call site must be reviewed; it bypasses the ORM-level guard but
    *not* PostgreSQL RLS.
    """


# ---------------------------------------------------------------------------
# Abstract bases
# ---------------------------------------------------------------------------

class UUIDModel(models.Model):
    """UUIDv4 primary keys.

    Sequential integer PKs leak business volume across tenants (an invoice
    id of 41 tells a customer how many invoices exist) and make record
    merging during tenant migration painful.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditedModel(TimeStampedModel):
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        abstract = True


class TenantScopedModel(UUIDModel, AuditedModel):
    """Base class for every row that belongs to a customer organisation."""

    tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        db_index=True,
    )

    objects = TenantManager()
    all_tenants = AllTenantsManager()

    class Meta:
        abstract = True
        # Every concrete subclass gets a (tenant_id, created_at) index; almost
        # every list endpoint is "this tenant's rows, newest first".
        indexes = [models.Index(fields=["tenant", "-created_at"])]

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            tenant_id = get_current_tenant_id()
            if tenant_id is None:
                raise PermissionDenied(
                    f"{type(self).__name__} saved without a tenant context."
                )
            self.tenant_id = tenant_id
        return super().save(*args, **kwargs)


class ImmutableFinancialModel(TenantScopedModel):
    """Posted financial documents: no hard delete, ever.

    Deleting a posted journal entry destroys the audit trail and silently
    changes historical reports that have already been filed. The only legal
    corrections are *void* (before the period closes) and *reverse* (after).
    """

    class Meta(TenantScopedModel.Meta):
        abstract = True

    def delete(self, *args, **kwargs):
        raise PermissionDenied(
            f"{type(self).__name__} is immutable once posted. "
            f"Create a reversing entry instead of deleting."
        )


# ---------------------------------------------------------------------------
# Shared choice sets
# ---------------------------------------------------------------------------

class Currency(models.TextChoices):
    """Deliberately a short list; extend per deployment.

    Stored as ISO-4217 alpha-3 on every monetary row so that a report can
    never silently add EGP to USD.
    """

    EGP = "EGP", "Egyptian Pound"
    USD = "USD", "US Dollar"
    EUR = "EUR", "Euro"
    GBP = "GBP", "Pound Sterling"
    SAR = "SAR", "Saudi Riyal"
    AED = "AED", "UAE Dirham"
    KWD = "KWD", "Kuwaiti Dinar"
