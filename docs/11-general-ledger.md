# 11 — General Ledger

**Scope:** the general-ledger module as it now stands after the GL refactor — the
5-level coded chart of accounts, the server-side coding scheme, the English default
chart, the add-account contract, the manual-journal lifecycle, the four core reports,
and the frontend screens that drive them. Describes the system *as implemented* on
branch `feature/gl-refactor-and-frontend`.

**Binding rules:** `CONVENTIONS.md`, `docs/03-data-model.md` (the `Account` entity),
`docs/04-state-machines.md` (the journal-entry and fiscal-period machines), and
`docs/06-api-contract.md` (endpoint conventions). Where this doc and the code disagree,
the code wins — every figure below is measured from it.

**Design stance:** the GL was *evolved in place*, not rebuilt. The single posting
choke point `post_entry()` and the nine module integrations are untouched; the chart
gained a coding scheme and an English default set ported from the reference GL, and the
screens were rebuilt in the existing SPA. ACHR keeps its own tenancy (single-DB + RLS)
and its own ledger-guard triggers — no tenancy code was ported.

---

## 1. The coding scheme

Every account sits at a **level 1–5** and carries a small `segment_code` — its number
within its parent. The absolute `full_code` packs the ancestor segments into one integer
using fixed per-level digit widths, so an account's place in the tree is a single
sortable, uniquely indexable number, and the human `code` is just that number formatted
with dots.

Source: `apps/accounting/services/coding.py`.

| Level | Meaning | Postable | Slot width |
|---|---|---|---|
| 1 | Financial-statement section (Assets, …) | no | 4 |
| 2 | General ledger | no | 4 |
| 3 | Subsidiary ledger 1 | no | 3 |
| 4 | Subsidiary ledger 2 | no | 2 |
| 5 | Account (leaf) | **yes** | 4 |

```
LEVEL_CODE_WIDTHS = (4, 4, 3, 2, 4)      # widest full_code = 17 digits, fits a signed BigInteger
```

**The rules, in one place:**

- level 1 is a section and is never postable;
- each child is exactly `parent.level + 1`; the tree is exactly five deep;
- **only level-5 accounts may be posted to, and every level-5 account must be** —
  `is_postable ⇔ level == 5`;
- codes are allocated **top-down by the server**: a caller names an account, never types
  a number; the segment is `max(sibling) + 1` taken under a parent row lock, so two
  concurrent creates under one parent cannot collide.

**Core functions:**

| Function | Does |
|---|---|
| `compute_full_code(parent_full, code, level)` | packs a segment onto the parent's full code |
| `max_sibling_code(level)` | largest segment that still fits the level's slot |
| `format_full_code(full_code, level)` | decodes the packed integer back to a dotted human code (e.g. `1.1.1.1.1`) |
| `validate_account_hierarchy(level, parent_level, is_postable)` | enforces child = parent+1 and postable ⇔ L5 |
| `next_sibling_code(tenant_id, parent)` | `Max(segment)+1` under the parent (inactive siblings still count) |
| `allocate_account(tenant_id, *, parent, name, …)` | creates the account, deriving level/segment/full_code/code/postability under a `select_for_update` parent lock |

`allocate_account` derives everything: level (root → 1, else `parent.level + 1`),
postability (L5), and inherits the section `type` and `income_category` from the parent
unless overridden. It raises a clean `DomainError` if the name is blank, the parent is in
another tenant, or the parent's numbering slot is exhausted.

---

## 2. The `Account` model extensions

One additive, nullable/defaulted migration (`0005_account_full_code_…`) extended the
existing `Account` (still a `TenantScopedModel` with `type`, `system_key`,
`cached_balance`) — see `docs/03-data-model.md` for the base columns.

| Column | Type | Purpose |
|---|---|---|
| `level` | `PositiveSmallInteger`, null | 1–5 depth in the tree |
| `segment_code` | `PositiveInteger`, null | this account's number within its parent |
| `full_code` | `BigInteger`, null, indexed | positional-encoded absolute code |
| `normal_balance_override` | `CharField` (`NormalBalance`), blank | forces debit/credit against the section default |
| `income_category` | `CharField` (`IncomeCategory`), default `none` | classifies a leaf on the income statement |
| `requires_party` | `Boolean` | a control account whose lines must name a partner |

`normal_balance` is a property: `normal_balance_override or NORMAL_BALANCE[type]`. Two
partial unique constraints hold the scheme together — `uq_account_full_code`
(tenant + full_code) and `uq_account_parent_segment` (tenant + parent + segment_code).

`type` (asset / liability / equity / income / expense) is retained: it drives the balance
sheet and the nine integrations. The coding scheme sits *alongside* it, not instead of it.

---

## 3. The English default chart

`apps/accounting/chart/english_chart.py` builds the default chart — ported from the
reference GL's `default_eg.json`, translated to English, and extended so every role the
ERP posts to resolves by `system_key` on a leaf.

Measured shape:

| Metric | Value |
|---|---|
| Root sections | 5 (Assets, Liabilities, Equity, Revenue, Expenses) |
| Total nodes | 137 |
| Postable leaves (all at L5) | 59 |
| Leaves carrying a `system_key` | 30 |

`build_default_chart(tenant_id, *, user_id) -> (counts, by_key)` upserts nodes on
`(tenant, full_code)` — **idempotent**: a re-run creates nothing. `required_system_keys()`
returns the set the integrations depend on; `_assert_system_keys` in the seed command
fails loudly if any is missing.

`seed_chart_of_accounts` builds this chart plus the six journals and the fiscal calendar.
The **integration guarantee** is that the nine modules (sales, purchasing, payments,
expenses, inventory, banking, projects, payroll, …) resolve accounts by `system_key`,
never by code — so re-charting is safe. Proven by `seed_demo_tenant` posting an invoice,
payment, expense and payroll run, and by `verify_core_invariants` passing 29/29 as the
non-owner `erp_app` role.

---

## 4. Adding an account (server-allocated coding)

`POST /accounts/` — the client sends `parent`, `name`, optional `normal_balance`
(override), `requires_party`, `income_category`; it **never sends a code**. The serializer
calls `allocate_account()` and returns the created account with its server-allocated
`code` / `level` / `full_code`. `code`, `level`, `full_code`, `type` and `is_postable` are
read-only; `update()` cannot re-parent a coded account.

Supporting endpoints on `AccountViewSet`:

| Endpoint | Returns |
|---|---|
| `GET /accounts/tree/` | the hierarchy with `level` / `full_code` for rendering |
| `GET /accounts/stats/` | per-level counts, e.g. `{"levels": {"1":5,"2":11,…,"5":59}}` |
| `GET /accounts/{id}/ledger/` | one account's movement with a running balance |
| `POST /accounts/{id}/archive/` | soft-archive (reauth-gated) |

Validation surfaces as clean 400s: name required, parent in the same tenant, level ≤ 5,
sibling/full-code uniqueness, numbering slot exhausted. Concurrency is covered by the
parent row lock.

---

## 5. The manual journal

`post_entry()` remains the **only** ledger write path. A manual entry is created as a
draft, then posted:

1. `POST /journal-entries/` with
   `{journal, entry_date, currency, exchange_rate, memo, lines:[{account, description, debit, credit, partner_type?, partner_id?}]}`
   → a **draft** (`201`). Lines must be ≥ 2 and balanced; a `requires_party` account's
   line must name a partner.
2. `POST /journal-entries/{id}/post/` → posts through `post_entry()` and allocates a
   gapless number (e.g. `GEN-2026-000001`). **This is a reauth-gated sensitive action** —
   the request must carry an `X-Reauth-Token` (obtained from `POST /auth/reauth/`).
3. `POST /journal-entries/{id}/void/` and `…/reverse/` complete the lifecycle.

See `docs/04-state-machines.md` for the full draft → posted → reversed machine and its
guards. Posting into a `SOFT_CLOSED`/`CLOSED` fiscal period is refused by the period
service (`apps/accounting/services/periods.py`).

---

## 6. The four core reports

Live endpoints under `/reporting/*`, each behind a shared date filter:

| Report | Endpoint | Filter |
|---|---|---|
| Trial balance | `GET /reporting/trial-balance/` | `date_from`, `date_to` |
| Income statement | `GET /reporting/profit-loss/` | `date_from`, `date_to` |
| Balance sheet | `GET /reporting/balance-sheet/` | `as_of` |
| General ledger | `GET /accounts/{id}/ledger/` | per-account movement + running balance |

The filter parameters are required — a bare GET returns `400`. Responses carry
`report_type`, `currency`, `generated_at`, `row_count` and `totals` (the trial balance's
`totals.difference` is `0` when balanced).

---

## 7. The frontend

Rebuilt in the existing vanilla-JS hash-router SPA (`frontend/app.js` + the CSS tokens in
`frontend/index.html`), using ACHR's own patterns (`api` / `go` / `V` / `simple` /
`openForm`, the `VIEWS` registry) and design tokens only (indigo `--acc`, `--panel*`,
`--ok`/`--warn`/`--dang`, `--r`, `--sh*`, Inter) — **English / LTR, light + dark**.

- **Chart of accounts** — a hierarchical tree from `/accounts/tree/`: `L{level}` badges,
  dotted codes, dr/cr tags on leaves, expand/collapse and search; an add-account modal
  (name + side only — the server allocates the code) mirroring the reference's top-down
  coding; rename and archive via `act()` (confirm + reauth); a per-level stats bar.
- **Manual journal** — a debit/credit line grid (account · party · currency · rate ·
  debit · credit) with a live balance indicator that gates *Post*; draft → post → reverse
  through the `journal-entries` API (post supplies the `X-Reauth-Token`).
- **Reports** — Trial balance, Income statement, Balance sheet and per-account General
  ledger over the endpoints above, behind a shared date bar.

---

## 8. Verification

| Check | Result |
|---|---|
| Test suite | **349 passed** (327 baseline + 13 coding + 3 chart + 6 add-account) |
| Core invariants | 29/29 as the non-owner `erp_app` role |
| Demo seed | `seed_demo_tenant` posts invoice / payment / expense / payroll on the new chart |
| End-to-end | seed → browse the English tree → add an account (code allocated) → post a balanced manual journal → the entry appears in trial balance, general ledger, balance sheet and income statement, in ACHR's theme |
| API compatibility | OpenAPI schema diffed each phase; only the intended account-field additions appear |

---

## 9. Where things live

| Concern | File |
|---|---|
| Public GL facade | `apps/accounting/services/__init__.py` |
| Posting choke point | `apps/accounting/services/posting.py` (`post_entry`/`void_entry`/`reverse_entry`) |
| Coding scheme | `apps/accounting/services/coding.py` |
| Fiscal-period service | `apps/accounting/services/periods.py` |
| English default chart | `apps/accounting/chart/english_chart.py` |
| Chart seed command | `apps/accounting/management/commands/seed_chart_of_accounts.py` |
| Account model + enums | `apps/accounting/models.py` |
| Account serializer / viewset | `apps/accounting/serializers.py`, `apps/accounting/viewsets.py` |
| Reports | `apps/reporting/` + `/reporting/*` |
| Frontend screens | `frontend/app.js`, `frontend/index.html` |
| Phase log | `backend/plans/gl-refactor.md` |
