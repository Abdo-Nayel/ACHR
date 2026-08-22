# Phase 3 — Services own the business logic

*Views become transport. Scoped deliberately to changes that are high-value and
fully verifiable — two real bug fixes and the two clearest SOLID gaps — rather
than a risky wholesale sweep of complex, untested flows (see "Deferred" below).*

## What changed

### 1. Bulk-delete bug on draft edits — **fixes B4**
`TenantQuerySet.delete()` refuses bulk delete, so editing a draft **bill**'s
lines (`bill.lines.all().delete()`) raised *403 "Bulk delete is disabled"* on
every legitimate edit. Sales had already worked around this with a local
`_replace_draft_lines`; the expenses VendorCredit path had a third inline copy.
Promoted one `replace_draft_lines()` into `apps/core/serializers.py` and routed
all three sites (invoice, credit note, bill, vendor credit) through it. New test
`tests/test_bill_editing.py` covers the bill-edit path, which had none.

### 2. One document-numbering service (F6)
Gapless numbering was written three times — sales invoice workflow, a **service
function living inside `payments/viewsets.py`**, and the ledger's posting engine
(which ignored the sequence's own `padding` column). Added
`apps/accounting/services/sequences.py` with a single `allocate_document_number()`
that always formats via `DocumentSequence.format()` (so `padding` is honoured).
Sales and payments now call it; the payments copy is out of the viewset. The
ledger's own `allocate_number` adopts it in Phase 8 (GL frozen, D4) — the padding
bug is now fixed in the canonical service it will use.

### 3. `projects` gets a service layer; `create_invoice` extracted (F14)
`ProjectViewSet.create_invoice` was **160 lines** of projects→sales→accounting
orchestration in an HTTP handler — and `projects` had no `services/` at all, so
it could never be called from a task or a scheduled billing run. Moved verbatim
into `apps/projects/services/invoicing.py` `create_invoice_from_time()`; the
viewset action is now ~20 lines. New test `tests/test_project_invoicing.py`
covers the guards and the once-only-billing happy path.

**Bug found and fixed during the move:** the original used
`select_for_update().select_related("task")` where `task` is nullable — PostgreSQL
rejects `FOR UPDATE` on an outer join's nullable side, so the endpoint 500'd for
any entry without a task. Fixed with `select_for_update(of=("self",))` (lock only
the timesheet rows), the same pattern the posting engine uses. The test proves it.

## Verification (PostgreSQL 18, as `erp_app`)

| Check | Result |
|---|---|
| `pytest` | **296 passed** (291 + 5 new across the two new test files) |
| `manage.py check` | 0 issues |
| OpenAPI schema vs baseline | paths + components **byte-identical** (D2) |
| `seed_demo_tenant` | INV/PMT/EXP numbers still correct via the new service |

## Deferred (recorded, not done)
The plan's fuller "services" sweep is intentionally **not** attempted here,
because doing it safely means first authoring tests for flows that currently have
none, and rushing it would risk the "no errors" mandate:

- **`payments` apply / refund** (113 + 92 lines in the viewset) — no `test_payments`
  exists; extracting them blind is unsafe. Needs a payments test suite first.
- **Fat actions in hr / expenses / sales** (`terminate`, `check_in/out`, approve/post…)
  — extract into their services incrementally, each behind a test.
- **`@action` → `transition_action`** conversions — cosmetic given
  `IdempotentActionMixin` already gives them idempotency; not worth the D2 risk now.
- **`accounts.py` `system_account()` consolidation (F7)** — the 5–6 copies have
  *different* missing-account semantics (raise vs return None); unifying needs care
  per call site and touches large service modules. Better as its own change.
- **Moving stray serializer/pagination classes out of `viewsets.py`** (Phase 2 part 4)
  — do it opportunistically alongside the extractions above.
