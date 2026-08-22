# Phase 7 — Reporting cleanup

*Reporting is the best-factored app in the codebase (a clean generator registry,
one shared `ledger_query`, a template-method base). The scope here is the one
real latent bug; the other candidates were assessed and deliberately left.*

## What changed

### Selectable-but-unimplemented report types (the real bug)
`ReportType` offers `general_ledger` and `expense_by_category`, but no generator
is registered for either. A `ReportDefinition` saved with one — and any schedule
built on it — used to fail only later, inside the Celery run, as an
uninformative `last_error`. `ReportDefinitionSerializer.validate_report_type` now
refuses a type with no generator up front, with a clear 400 that lists what *is*
available. The enum keeps both values (they are on the roadmap); you simply
cannot save a report the engine cannot produce. Covered by
`tests/test_report_definition_validation.py`.

## Assessed and deliberately not done

- **Route `kpis._totals` through `generators.base.ledger_query`** (the flagged
  duplication). On inspection these are genuinely different interfaces —
  `ledger_query` is `ReportContext`-based (dept/project filters, period logic),
  `_totals` is `(tenant_id, upto, since)`-based and tenant-wide by design. KPIs
  are not filtered by department/project, so there is no filter being *dropped*
  in practice; forcing one through the other would add coupling and risk the
  tested KPI path for marginal gain. Left as-is.
- **Merge `financial._AccountBook` with `kpis._Chart`** — two chart caches with
  different shapes in different modules; same conclusion.
- **Split `CashFlowGenerator.generate` (236 lines) / `compute_hr_metrics` (203)**
  — cohesive report builders, fully covered by `test_kpis`/`test_hr_metrics`.
  Purely a readability change; skipped under the "simplest, finish fast" brief.

## Verification (PostgreSQL 18)

| Check | Result |
|---|---|
| `pytest` | **327 passed** (324 + 3 new) |
| `test_kpis` / `test_hr_metrics` | unchanged, green |
| OpenAPI schema vs baseline | byte-identical (D2) |
