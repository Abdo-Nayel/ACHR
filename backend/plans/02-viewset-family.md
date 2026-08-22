# Phase 2 — One viewset family

*Reuse what `apps/core` already provides instead of re-declaring it. Two
high-value wins; the API surface is byte-identical to baseline.*

## What changed

### A. `IdempotentActionMixin` + `NotImplementedYet` moved to core
Both lived in `apps/accounting/viewsets.py` and were imported by five sibling
apps (hr, expenses, sales, payroll, payments) — inbound edges into the ledger
module that had nothing to do with the ledger.
- `IdempotentActionMixin` → `apps/core/viewsets.py` (beside the idempotency
  helpers it already reused via a cross-module import).
- `NotImplementedYet` → `apps/core/exceptions.py` (with the rest of the error
  vocabulary).
- All six importers now import from core; `apps/accounting/viewsets.py`'s
  `__all__` and the now-unused helper imports are gone.

**Result:** no app outside `apps/accounting` imports `apps.accounting.viewsets`
anymore — six edges into the GL removed before the GL is touched (D4).

### B. `abac` flag replaces seven `RbacOnlyQuerysetMixin` copies (F1)
`ScopedQuerysetMixin` (in `apps/iam/permissions.py`) gains `abac: bool = True`;
when `False` it skips the actor-scope `Q` (tenancy + RLS still apply) and no
longer requires `scope_resource`. `RbacOnlyQuerysetMixin` is now a **single**
3-line mixin in `apps/core/viewsets.py` that just sets `abac = False`.

The seven byte-identical local copies (banking, expenses, hr, inventory,
payments, payroll, reporting — each a ~15-line re-implementation of
`get_queryset`) are deleted and replaced with an import of the core one. The
~30 viewset class declarations that use it did not change. The explicit
`order_by(*self.ordering)` those copies carried was redundant: `OrderingFilter`
(a default filter backend) and the cursor paginators already own response
ordering — confirmed by the suite (incl. `test_account_ledger`) staying green.

## Verification (PostgreSQL 18, as `erp_app`)

| Check | Result |
|---|---|
| `pytest` | **291 passed** |
| `manage.py check` | 0 issues |
| OpenAPI schema vs baseline | paths + components **byte-identical** (D2) |
| `grep "class RbacOnlyQuerysetMixin" apps/` | only `apps/core/viewsets.py` |
| `grep "from apps.accounting.viewsets import" apps/` (non-accounting) | none |
| ruff F-category on all touched files | clean |

## Scope notes (deliberate deferrals)
- **Folding `IamViewSetMixin` / `TenancyViewSetMixin`** into the core family
  (originally Phase 2 part 3, closes F13) moves to **Phase 4**, where the IAM
  module is already being restructured — cheaper and less risky there than a
  standalone pass now.
- **Moving stray serializers / pagination / exception classes out of the
  `viewsets.py` files** (Phase 2 part 4) moves to **Phase 3**, done opportunistically
  while those same viewsets are being thinned into services.
Both are recorded so nothing is lost; neither is high-value enough to justify
touching those files twice.
