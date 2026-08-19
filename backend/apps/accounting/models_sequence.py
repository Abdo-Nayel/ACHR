"""
Gapless per-tenant document numbering.

A PostgreSQL ``SEQUENCE`` is the obvious choice and the wrong one here: it is
non-transactional, so a rolled-back transaction burns a number and leaves a
gap. Tax authorities in many jurisdictions (Egypt, KSA, EU e-invoicing) treat
gaps in an invoice sequence as prima facie evidence of deleted invoices.

So we use a locked counter row instead. It serialises number allocation per
(tenant, scope, year), which is exactly the contention we want: two invoices
issued at the same instant get 41 and 42, never both 41, and never 41 and 43.
"""

from __future__ import annotations

from django.db import models

from apps.core.models import TenantScopedModel


class DocumentSequence(TenantScopedModel):
    """One counter per (tenant, scope, year)."""

    #: e.g. "journal:SAL", "invoice", "payslip", "payment", "credit_note"
    scope = models.CharField(max_length=50)
    year = models.PositiveSmallIntegerField()
    prefix = models.CharField(max_length=12)
    next_value = models.PositiveIntegerField(default=1)
    #: Zero-padding width for the numeric part.
    padding = models.PositiveSmallIntegerField(default=6)

    class Meta(TenantScopedModel.Meta):
        db_table = "accounting_document_sequence"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "scope", "year"], name="uq_sequence_scope_year"
            ),
            models.CheckConstraint(
                condition=models.Q(next_value__gte=1), name="ck_sequence_positive"
            ),
        ]

    def format(self, value: int) -> str:
        return f"{self.prefix}-{self.year}-{value:0{self.padding}d}"
