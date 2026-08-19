# Engineering conventions — binding contract for every module

Every model file in `backend/apps/*/models.py` MUST follow these rules.
They exist to make whole classes of bug structurally impossible.

## 1. Imports and base classes

```python
from apps.core.fields import MoneyField, QuantityField, RateField, ZERO
from apps.core.models import (
    TenantScopedModel, ImmutableFinancialModel, TimeStampedModel, Currency,
)
```

* Any row owned by a customer organisation subclasses `TenantScopedModel`
  (gives it `id` UUID PK, `tenant` FK, `created_at/updated_at`,
  `created_by/updated_by`, tenant-filtered default manager).
* Any *posted* financial document subclasses `ImmutableFinancialModel`
  (same, plus `delete()` raises).
* Reference/lookup rows that are genuinely global (country list, currency
  table) subclass plain `models.Model`.

## 2. Money

* **Never** `models.FloatField`, `float`, or `models.DecimalField` directly
  for money. Use `MoneyField` / `QuantityField` / `RateField`.
* Every model that stores an amount also stores `currency`
  (`models.CharField(max_length=3, choices=Currency.choices)`) — unless it is
  a child line whose parent already pins the currency, in which case add a
  `CheckConstraint` or a `clean()` assertion that they match.
* Amounts default to `ZERO`, never `None`, unless "unknown" is a real state.

## 3. Meta block — required on every concrete model

```python
class Meta(TenantScopedModel.Meta):
    db_table = "<app>_<entity>"          # explicit, snake_case, plural-free
    constraints = [...]                   # see below
    indexes = [...]                       # see below
    ordering = ["-created_at"]
```

Note `class Meta(TenantScopedModel.Meta)` — inheriting keeps the
`(tenant, -created_at)` index. If you override `indexes`, re-add it.

### Constraints you must add where applicable

* `UniqueConstraint(fields=["tenant", "<natural key>"], name="uq_<table>_<key>")`
  — document numbers, employee codes and SKUs are unique **per tenant**, not
  globally. A global unique index is a cross-tenant information leak and a
  guaranteed production incident.
* `CheckConstraint` for every non-negative amount, every
  `end >= start` date pair, and every enum/status invariant the DB can express.
* Partial unique indexes via `condition=Q(...)` for "only one active X".

### Indexes you must add

* Every FK used in a list filter.
* `(tenant, status)` wherever a status is filtered in the UI.
* `(tenant, <date>)` for anything that appears in a period report.

## 4. Status fields

Use `models.TextChoices` nested in the model, `db_index=True`, and a
`transition()` method that validates against an explicit
`ALLOWED_TRANSITIONS: dict[str, set[str]]` map. Never assign `.status =`
directly from a view.

## 5. Deletion

Business documents are archived (`is_archived`), voided (`status=VOIDED`) or
reversed. `on_delete=models.PROTECT` is the default for every FK.
`CASCADE` is permitted only for child lines of an unposted parent
(e.g. `InvoiceLine` -> `Invoice`) and for pure join tables.

## 6. Docstrings

Every model gets a docstring explaining *what business fact it records* and
any non-obvious invariant. Comment the "why", not the "what" — a reader can
see that a field is a FK; they cannot see why it is `PROTECT`.

## 7. GL integration

Any module that produces a financial effect exposes a
`build_journal_entry(self) -> JournalEntryDraft` method rather than writing
`JournalLine` rows itself. Posting goes through
`apps.accounting.services.posting.post_entry()` exclusively — that is the
single choke point where `sum(debits) == sum(credits)` is verified.

## 8. Naming

* Models: `PascalCase`, singular (`Invoice`, not `Invoices`).
* Tables: `<app>_<entity>` snake_case (`sales_invoice`).
* Constraint names: `ck_`, `uq_`, `fk_`, `ix_` prefixes, ≤ 63 chars
  (PostgreSQL identifier limit — longer names are silently truncated and
  collide).
* Booleans: `is_` / `has_` prefix.
* Money columns: end in `_amount`, `_total`, `_price`, `_balance`.

## 9. Style

* `from __future__ import annotations` at the top of every module.
* Python 3.11+, Django 5.x syntax. Type hints on all service functions.
* Line length 100.
* No business logic in models beyond invariants and transitions; put it in
  `apps/<app>/services/`.
