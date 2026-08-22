# Refactor plans

One file per phase of the backend clean-code refactor, in execution order.
The plan is committed alongside the code so the reasoning travels with the diff.

| Phase | File | Scope |
|---|---|---|
| 0 | `00-baseline.md` | Environment, branch, test baseline, tooling config |
| 1 | `01-core-kernel.md` | Tenant binding, exception vocabulary, status transitions |
| 2 | `02-viewset-family.md` | One viewset base; delete the duplicated mixins |
| 3 | `03-services.md` | Business logic out of views and into services |
| 4 | `04-iam-split.md` | Split the 1,170-line permissions module |
| 5 | `05-tenancy-checks.md` | Make the RLS guarantee machine-checked |
| 6 | `06-config-honesty.md` | Delete config that names modules which don't exist |
| 7 | `07-reporting.md` | Reporting dedup and god-function split |
| 8 | `08-general-ledger.md` | **Deferred.** The GL, refactored last |

## Ground rules

1. **Behaviour and the public API do not change.** `backend/plans/baseline-openapi.json`
   is the gate: regenerate the schema after each phase and diff it.
2. **Every phase deletes more than it adds.** This refactor is subtraction.
3. **The database schema and all 23 migrations are frozen.** Python only.
4. **`apps/accounting` ledger internals are frozen until Phase 8.** Earlier phases may
   move files *out* of that app and add new ones, but change no ledger logic.
5. **Tests green before and after every phase**, measured against the Phase 0 baseline.
