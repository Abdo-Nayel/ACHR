# 03 — Data Model Reference

**Scope:** every persistent entity, its purpose, its key columns, its constraints and
its indexes; the chart-of-accounts design; the cross-app referential integrity map; the
indexing strategy; and the row-count growth model.

**Binding rules:** `CONVENTIONS.md`. Entities marked ✅ already exist in code and are
described *as implemented* — do not contradict them. Entities marked 🔨 are specified
here for implementation.

**Universal bases** (from `apps/core/models.py`):

| Base | Gives |
|---|---|
| `UUIDModel` | `id UUID PK DEFAULT uuid4` |
| `TimeStampedModel` | `created_at` (indexed), `updated_at` |
| `AuditedModel` | the above + `created_by`, `updated_by` (FK → `iam.User`, `PROTECT`, nullable) |
| `TenantScopedModel` | the above + `tenant` FK (`PROTECT`), `objects = TenantManager()` (fails closed), `all_tenants = AllTenantsManager()`, and a `(tenant, -created_at)` index |
| `ImmutableFinancialModel` | `TenantScopedModel` + `delete()` raises `PermissionDenied` |

Every column below is in addition to those. Every `MoneyField`/`QuantityField` is
`numeric(19,6)`; every `RateField` is `numeric(9,6)`.

---

## 1. App map

| App | Owns | Depends on |
|---|---|---|
| `core` | Abstract bases, money primitives, tenancy context, shared enums, attachments, notifications | — |
| `tenancy` | Tenant, domains, subscription, audit log | `core` |
| `iam` | User, membership, roles, permissions, ABAC scopes, API keys | `tenancy`, `hr` (soft, nullable) |
| `accounting` | Chart of accounts, tax rates, fiscal calendar, journals, entries, lines, FX, sequences | `core`, `tenancy` |
| `sales` | Customers, quotes, orders, invoices, credit notes, recurring templates | `accounting`, `inventory`, `projects` |
| `purchasing` | Vendors, purchase orders, vendor bills | `accounting`, `inventory` |
| `payments` | Payments, allocations, gateway accounts, webhook events, refunds, disputes | `accounting`, `sales`, `purchasing` |
| `expenses` | Expense categories, claims, claim lines, mileage, reimbursements | `accounting`, `hr` |
| `inventory` | Items, item categories, warehouses, stock levels, movements, adjustments, transfers | `accounting` |
| `banking` | Bank accounts, statements, statement lines, match rules, reconciliations | `accounting` |
| `projects` | Projects, tasks, members, timesheets, budgets | `accounting`, `hr`, `sales` |
| `hr` | Departments, positions, employees, contracts, leave, attendance, documents | `tenancy`, `core` |
| `payroll` | Pay components, salary structures, statutory rules, payroll runs, payslips | `hr`, `accounting` |
| `reporting` | Report definitions, saved reports, materialised balances, export jobs, integrity runs | `accounting` (read) |

---

## 2. `core`

| Entity | Purpose |
|---|---|
| ✅ `Currency` (TextChoices) | ISO-4217 alpha-3 code set: EGP, USD, EUR, GBP, SAR, AED, KWD. Extendable per deployment. |
| 🔨 `Attachment` | A file in object storage bound to any business document. |
| 🔨 `Notification` | An in-app / email / push message queued for a membership. |
| 🔨 `IdempotencyRecord` | Remembers an HTTP `Idempotency-Key` and the response it produced. |
| 🔨 `Country` | Global reference table (ISO-3166, default currency, tax regime code). Plain `models.Model` — genuinely global, not tenant-scoped. |

### `core_attachment` 🔨 — `TenantScopedModel`

| Column | Type | Notes |
|---|---|---|
| `object_type` | `varchar(64)` | e.g. `sales.Invoice`, `expenses.ExpenseClaim` |
| `object_id` | `uuid` | Generic pointer; deliberately not a Django `GenericForeignKey` (no cross-table FK integrity either way, but this form indexes cleanly and survives app renames) |
| `storage_key` | `varchar(512)` | `tenant/{tenant_id}/{domain}/{object_id}/{sha256}.{ext}` |
| `file_name`, `content_type`, `byte_size`, `sha256` | | `content_type` verified by magic bytes, never trusted from the client |
| `uploaded_by` | FK → `iam.User` `PROTECT` | |
| `is_confirmed` | `bool` | False until the server verifies the object exists; unconfirmed rows swept after 24 h |

Constraints/indexes: `uq_attachment_key` on `(tenant, storage_key)`;
`ix_attachment_object` on `(tenant, object_type, object_id)`;
`ck_attachment_size_positive`.

### `core_idempotency_record` 🔨 — `TenantScopedModel`

`key varchar(255)`, `endpoint varchar(255)`, `request_hash char(64)`,
`status` (`in_flight|completed`), `response_status smallint`, `response_body jsonb`,
`expires_at timestamptz`.
Constraints: `uq_idempotency_tenant_key` on `(tenant, key)`;
index `ix_idempotency_expiry` on `(expires_at)` for the sweeper.
A repeat with a *different* `request_hash` returns `422`, never a replay.

### `core_notification` 🔨 — `TenantScopedModel`

`membership` FK → `iam.TenantMembership` `CASCADE`, `channel`
(`in_app|email|sms|push`), `template_code`, `payload jsonb`, `read_at`, `sent_at`,
`failed_reason`.
Index `ix_notification_unread` on `(tenant, membership, read_at)`.

---

## 3. `tenancy` ✅ (implemented)

| Entity | Purpose |
|---|---|
| `Tenant` | One customer organisation. **This row is the scope, so it is not itself tenant-scoped** — it lives outside RLS and is reached via the platform-admin path or by joining from an authenticated membership. |
| `TenantDomain` | Custom domains mapped to a tenant, used by the host-header resolver. |
| `Subscription` | Billing plan; kept separate from `Tenant` so plan history is append-only. |
| `TenantAuditLog` | Append-only record of security-relevant actions. |

### `tenancy_tenant`

Key columns: `name`, `legal_name`, `slug` (globally unique, 3–63 chars, subdomain-safe),
`status` (`trial|active|past_due|suspended|closed`), `country` (ISO-3166 alpha-2),
`timezone`, `base_currency`, `tax_registration_number`, `fiscal_year_start_month`,
`settings jsonb`, `trial_ends_at`, `suspended_at`.

* `ck_tenant_fiscal_month_range` — 1 ≤ `fiscal_year_start_month` ≤ 12.
* `ix_tenant_status`.
* `base_currency` is immutable after the first posted journal entry (service-enforced in
  `apps.tenancy.services.settings.change_base_currency`) — changing it would invalidate
  every historical report.
* `is_operational` is `trial|active`. `past_due` keeps **read** access so a customer can
  always export their own books.

### `tenancy_domain`

`domain varchar(253) UNIQUE`, `is_primary`, `verified_at`.
`uq_domain_one_primary_per_tenant` — a *partial* unique index on `(tenant)` where
`is_primary`, so the database rejects a second primary rather than trusting application
code.

### `tenancy_subscription`

`plan` (`starter|standard|professional|enterprise`), `seats`, `monthly_amount`,
`currency`, `started_on`, `ended_on`.
`ck_subscription_period_order`; `uq_subscription_one_open_per_tenant` (partial, where
`ended_on IS NULL`).

### `tenancy_audit_log`

Not a `TenantScopedModel` **by design**: it must be writable while the tenant context is
still being *established* (login, tenant switch, impersonation) and must never be
deletable by tenant users. `tenant` is a nullable FK (`PROTECT`).

`actor_id uuid`, `actor_email` (denormalised — survives user deletion), `action`
(login, login_failed, role_granted, role_revoked, impersonation, export, period_closed,
entry_reversed, payroll_approved, setting_changed), `object_type`, `object_id`,
`payload jsonb` (before/after; never secrets or PANs), `ip_address`, `user_agent`,
`occurred_at`.
Indexes: `ix_audit_tenant_time` on `(tenant, -occurred_at)`; `ix_audit_object` on
`(object_type, object_id)`.

---

## 4. `iam` ✅ (implemented)

| Entity | Purpose |
|---|---|
| `User` | A human login. **Global, not tenant-scoped** — one identity can serve five client companies. |
| `TenantMembership` | Links a user to a tenant; the join row a session is built on. |
| `Permission` | One atomic capability, `<domain>.<resource>.<action>`. Global: the catalogue of what the software *can* do is a property of the software. |
| `Role` | A named bundle of permissions. `tenant IS NULL` ⇒ system role shipped with the product. |
| `RolePermission` | Explicit through-model so grants are auditable and revocable. |
| `RoleAssignment` | Grants a role to a membership, optionally narrowed to a department or project, optionally time-bounded. |
| `ScopeRule` | The ABAC predicate for a (role, resource) pair. |
| `ApiKey` | Machine credential; only a hash is stored. |

### `iam_user`

`email UNIQUE`, `full_name`, `phone`, `locale`, `timezone`, `is_active`, `is_staff`,
`is_platform_admin`, `mfa_enabled`, `mfa_secret`, `last_login_ip`,
`password_changed_at`, `failed_login_count`, `locked_until`.
Index `ix_user_active`.

### `iam_tenant_membership`

`tenant` FK `CASCADE`, `user` FK `CASCADE`, `employee` **OneToOne** → `hr.Employee`
`SET_NULL` nullable, `is_active`, `is_owner`, `invited_by`,
`invitation_accepted_at`, `last_active_at`.

* `uq_membership_tenant_user`, `uq_membership_tenant_employee` (partial, where
  `employee IS NOT NULL`).
* `ix_membership_user_active`, `ix_membership_tenant_act`.
* The employee link is **one-way and optional in both directions**: not every user is an
  employee (external auditor), and not every employee has a login (factory floor staff
  clocked in by a supervisor).
* Deactivating a membership must revoke access immediately, so the token-refresh path
  re-reads this row rather than trusting the JWT claim.

### `iam_permission`

`codename varchar(100) PK` (the grammar is load-bearing: middleware derives the required
permission from a view's `resource` + HTTP method, so a typo is a startup error rather
than a silent authorisation bypass), `domain`, `resource`, `action`, `description`,
`is_sensitive`.
Index `ix_perm_res_action`.

### `iam_role`

`tenant` nullable FK `CASCADE`, `code slug`, `name`, `description`, `is_system`,
`rank smallint` (lower = more authority; a user may only grant roles ranked strictly
below their own, which is how privilege escalation is blocked).

* `uq_role_tenant_code`; `uq_role_system_code` (partial, where `tenant IS NULL`).
* `ck_role_system_has_no_tenant` — system ⇔ no tenant. A tenant may clone a system role,
  never edit it; otherwise a product update that adds a permission would silently grant
  it to a customer-modified role.

### `iam_role_assignment`

`membership` FK `CASCADE`, `role` FK `PROTECT`, `department` nullable FK →
`hr.Department` `CASCADE`, `project` nullable FK → `projects.Project` `CASCADE`,
`valid_from`, `valid_until`, `granted_by`.
`uq_assignment_unique_scope` on `(membership, role, department, project)`;
`ck_assignment_validity_order`; `ix_assign_member` on `(membership, valid_until)`.
Time-bounding is the point: a temporary auditor gets `valid_until = quarter_end` and
access lapses without anyone remembering to revoke it.

### `iam_scope_rule`

`role` FK `CASCADE`, `resource` (matches `Permission.resource`), `strategy` — a **closed
vocabulary**: `all`, `own_record`, `own_department`, `department_subtree`,
`assigned_projects`, `managed_employees`, `scoped_department`, `none` — and
`parameters jsonb` (e.g. `{"max_amount": "5000.00"}`).
`uq_scope_rule_role_resource`.
Free-form expression text is prohibited: an injection bug in the authorisation layer is
a full data breach, whereas a fixed enum means every possible predicate was reviewed
once, in code.

### `iam_api_key`

`tenant` FK `CASCADE`, `name`, `prefix UNIQUE`, `key_hash`, `role` FK `PROTECT`,
`created_by` FK `PROTECT`, `last_used_at`, `expires_at`, `revoked_at`.
`ix_apikey_tenant` on `(tenant, revoked_at)`.

---

## 5. `accounting` ✅ (implemented)

| Entity | Purpose |
|---|---|
| `Account` | A node in the tenant's chart of accounts. |
| `TaxRate` | VAT/sales-tax definition linked to the accounts it posts to. |
| `FiscalYear` | A financial year; closing one rolls net income into equity. |
| `FiscalPeriod` | Usually a month. The unit at which the books lock. |
| `Journal` | A book of original entry (Sales, Purchase, Cash, Payroll, Inventory, General). |
| `JournalEntry` | A balanced set of debits and credits on one date. Immutable once posted. |
| `JournalLine` | One debit **or** one credit against one account. |
| `ExchangeRate` | Daily FX rates, per tenant. |
| `DocumentSequence` | Gapless counter per (tenant, scope, year). |

### 5.1 `accounting_account`

| Column | Notes |
|---|---|
| `code varchar(20)` | unique per tenant |
| `name varchar(150)` | |
| `type` | `asset\|liability\|equity\|income\|expense`, indexed |
| `parent` | self-FK `PROTECT` — hierarchy for presentation only |
| `currency` | nullable; NULL = tenant base currency |
| `is_postable` | **only leaf accounts may be posted to.** Posting to a parent makes its balance ambiguous — its own postings or the roll-up? |
| `is_active` | |
| `system_key varchar(50)` | indexed; identifies wired-in accounts |
| `is_reconcilable` | bank/cash accounts eligible for reconciliation |
| `cached_balance`, `cached_balance_as_of` | denormalised; maintained only by the posting service inside the posting transaction |

Constraints: `uq_account_code` on `(tenant, code)`; `uq_account_system_key` on
`(tenant, system_key)` partial where `system_key <> ''`; `ck_account_no_self_parent`.
Indexes: `(tenant, -created_at)`, `ix_account_type` on `(tenant, type, is_active)`,
`ix_account_reconcilable` on `(tenant, is_reconcilable)`.

### 5.2 Chart of accounts design

**Account types and normal balance.** `NORMAL_BALANCE` in
`apps/accounting/models.py` is the single mapping that turns an intent into a correct
side, and is the reason `post_entry()` never asks a caller to specify one:

| Type | Normal balance | Increases on | Decreases on | Appears on | Closed at year end |
|---|---|---|---|---|---|
| `asset` | debit | debit | credit | Balance sheet | no |
| `expense` | debit | debit | credit | P&L | **yes → retained earnings** |
| `liability` | credit | credit | debit | Balance sheet | no |
| `equity` | credit | credit | debit | Balance sheet | no |
| `income` | credit | credit | debit | P&L | **yes → retained earnings** |

**Numbering.** Ranges are a per-country template, not a hardcoded rule (the localised
standard chart differs per market), but the shipped default is:

| Range | Block |
|---|---|
| 1000–1999 | Assets |
| 2000–2999 | Liabilities |
| 3000–3999 | Equity |
| 4000–4999 | Income |
| 5000–5999 | Cost of sales |
| 6000–7999 | Operating expenses |
| 8000–8999 | Other income/expense |
| 9000–9999 | Tax & appropriations |

**Depth.** Three levels is the target: header (non-postable) → group (non-postable) →
detail (`is_postable = True`). The database does not cap depth, but a chart deeper than
four levels is a reporting problem, not a feature.

**`system_key` — required system accounts.** Automated postings resolve accounts by
`system_key`, never by `code`, because codes differ per country's standard chart. Every
tenant must have all of these before the first posting; tenant provisioning creates them
and refuses to complete if any is missing.

| `system_key` | Type | Normal | Used by | Why it must exist |
|---|---|---|---|---|
| `ar_control` | asset | debit | Invoices, receipts, credit notes | The AR subledger total must equal this account; nothing else may post to it |
| `ap_control` | liability | credit | Vendor bills, vendor payments | Same, for AP |
| `inventory_asset` | asset | debit | Stock receipts, issues, adjustments | Must equal `Σ(qty × avg_cost)` (FR-STK-02) |
| `cogs` | expense | debit | Stock issues on sale | Recognised at issue using the cost *at that moment* |
| `output_vat` | liability | credit | Sales tax collected | Referenced by `TaxRate.collected_account` |
| `input_vat` | asset | debit | Recoverable purchase tax | Referenced by `TaxRate.paid_account`; unused when `is_recoverable = False` |
| `retained_earnings` | equity | credit | Year-end close | Destination of closed P&L balances |
| `salaries_payable` | liability | credit | Payroll post → payroll pay | Must return to zero for a fully paid run |
| `income_tax_payable` | liability | credit | Payroll statutory deduction | Remitted to the authority separately |
| `social_insurance_payable` | liability | credit | Payroll employee + employer contributions | |
| `gateway_clearing` | asset | debit | Payment capture → settlement | Must net to zero per settled batch; anything older than 5 days is alerted |
| `bank_fees` | expense | debit | Settlement fees, bank charges found in reconciliation | Fee taken from the settlement payload, never estimated |
| `fx_gain_loss` | income or expense | — | Revaluation and settlement of foreign-currency balances | Modelled as one account with both directions, or a pair; one account keeps the P&L line honest |
| `opening_balance_equity` | equity | credit | Migration opening balances | The balancing side of the single `OPENING` journal entry; must be zero once migration is complete, and a non-zero balance here is a migration defect, not an accounting result |

Recommended additional keys: `employee_payable` (expense reimbursements),
`grni` (goods received not invoiced), `inventory_adjustment`, `rounding_difference`,
`suspense` (bank items pending classification — must be empty at period close),
`unearned_revenue`, `interest_income`.

Guard rails: an account with a non-empty `system_key` cannot be deleted, cannot change
`type`, and cannot be made non-postable; the constraint `uq_account_system_key` prevents
two accounts claiming the same role.

### 5.3 `accounting_tax_rate`

`name`, `code`, `rate` (fraction: `0.140000` = 14%), `is_compound`, `is_recoverable`,
`collected_account` FK `PROTECT` (output VAT — a liability), `paid_account` nullable FK
`PROTECT` (input VAT — an asset when recoverable), `is_active`, `effective_from`,
`effective_to`.
`uq_tax_rate_code` on `(tenant, code, effective_from)` — rate changes are new rows, so
historical documents keep the rate they were taxed at; `ck_tax_rate_fraction`
(0 ≤ rate ≤ 1); `ck_tax_rate_period_order`.

### 5.4 `accounting_fiscal_year` / `accounting_fiscal_period`

`FiscalYear`: `name`, `start_date`, `end_date`, `status` (`open|closed`), `closed_at`.
`uq_fiscal_year_name`; `ck_fiscal_year_date_order`.

`FiscalPeriod`: `fiscal_year` FK `PROTECT`, `name`, `start_date`, `end_date`, `status`
(`open|soft_closed|closed`), `closed_at`, `closed_by`.
`uq_period_start` on `(tenant, start_date)`; `ck_period_date_order`; indexes
`(tenant, -created_at)` and `ix_period_range` on `(tenant, start_date, end_date)`.

`accepts_postings` is `status == OPEN`. `SOFT_CLOSED` allows posting only to holders of
`accounting.period.post_to_soft_closed` — month-end is a process, not an instant, and
this state removes the usual workaround of leaving periods open "just in case", which
is how prior-period figures silently change.

### 5.5 `accounting_journal`

`code`, `name`, `kind` (`sales|purchase|cash|payroll|inventory|general`),
`default_account`, `sequence_prefix` (default `JE`), `is_active`.
`uq_journal_code` on `(tenant, code)`.
Separate journals give each document type its own numbering sequence and let the audit
trail answer "every payroll posting in March" without scanning the ledger.

### 5.6 `accounting_journal_entry` — `ImmutableFinancialModel`

| Column | Notes |
|---|---|
| `journal` FK `PROTECT`, `period` FK `PROTECT` | |
| `number varchar(32)` | blank on drafts; allocated at **posting** time, so an abandoned draft cannot burn a number and create an audit gap |
| `entry_date` | indexed |
| `status` | `draft\|posted\|voided\|reversed`, indexed |
| `source` | `manual\|invoice\|bill\|payment\|expense\|payroll\|inventory\|bank\|opening\|closing`, indexed |
| `memo varchar(500)`, `currency`, `exchange_rate` | rate to base currency at `entry_date`; 1 when they match |
| `total_debit`, `total_credit` | materialised control totals — the physical embodiment of "debits equal credits" |
| `posted_at`, `posted_by` | |
| `reversal_of` | OneToOne self-FK `PROTECT`; set on the mirror, `related_name="reversed_by"` |
| `void_reason varchar(255)` | |
| `source_document_type`, `source_document_id` | generic pointer back to the subsidiary document |
| `idempotency_key varchar(128)` | blank when unused |

Constraints:
* `uq_entry_number` on `(tenant, journal, number)` partial where `number <> ''`
* `uq_entry_idempotency` on `(tenant, idempotency_key)` partial where non-empty — the real guarantee behind webhook/task retries
* `ck_entry_balanced` — `status <> 'posted'` **OR** (`total_debit = total_credit` AND `total_debit > 0`). A draft may be unbalanced while it is being built; a posted entry may not, and may not be zero
* `ck_entry_totals_non_negative`, `ck_entry_posted_has_timestamp`, `ck_entry_fx_positive`

Indexes: `(tenant, -created_at)`, `ix_entry_status` on `(tenant, status, entry_date)`,
`ix_entry_period` on `(tenant, period, status)`, `ix_entry_source_doc` on
`(source_document_type, source_document_id)`.

`ALLOWED_TRANSITIONS`: `draft → {posted, voided}`, `posted → {voided, reversed}`,
`voided → {}`, `reversed → {}`.

### 5.7 `accounting_journal_line` — `ImmutableFinancialModel`

| Column | Notes |
|---|---|
| `entry` FK **CASCADE** | the only legal cascade in the ledger: lines have no meaning without their entry, and the entry itself cannot be deleted (`delete()` raises) |
| `line_number smallint` | |
| `account` FK `PROTECT` | |
| `description varchar(500)` | |
| `debit`, `credit` | non-negative; exactly one is > 0 |
| `base_debit`, `base_credit` | converted at the entry's rate and **stored**, so historical reports do not shift when a rate table is corrected |
| `partner_type varchar(20)`, `partner_id uuid` | `customer\|vendor\|employee` — a polymorphic dimension, not an FK, because the subledgers live in four apps |
| `project` nullable FK `PROTECT`, `department` nullable FK `PROTECT` | analytical dimensions |
| `tax_rate` nullable FK `PROTECT` | |
| `reconciled_at` | set when matched to a bank statement line |

Constraints: `uq_line_number_per_entry` on `(entry, line_number)`;
`ck_line_non_negative`; **`ck_line_single_sided`** — `(debit > 0 AND credit = 0) OR
(credit > 0 AND debit = 0)`; `ck_line_base_non_negative`.

Indexes: `(tenant, -created_at)`, `ix_line_account` on `(tenant, account)`,
`ix_line_partner` on `(tenant, partner_type, partner_id)`, `ix_line_project`,
`ix_line_department`, `ix_line_unreconciled` on `(account, reconciled_at)`.

### 5.8 `accounting_exchange_rate`

`from_currency`, `to_currency`, `rate`, `rate_date` (indexed), `source`.
`uq_fx_rate_day` on `(tenant, from_currency, to_currency, rate_date)`; `ck_fx_positive`;
`ck_fx_distinct_currencies`. Per tenant because a group may use a corporate rate table
that differs from the central bank's.

### 5.9 `accounting_document_sequence`

`scope varchar(50)` (`journal:SAL`, `invoice`, `payslip`, `payment`, `credit_note`),
`year smallint`, `prefix varchar(12)`, `next_value`, `padding smallint` (default 6).
`uq_sequence_scope_year` on `(tenant, scope, year)`; `ck_sequence_positive`.
A locked counter row, not a PostgreSQL `SEQUENCE` — see ADR-005.

---

## 6. `sales` 🔨

| Entity | Purpose |
|---|---|
| `Customer` | A party we bill. Carries the AR subledger identity. |
| `CustomerContact` | People at a customer; one is billing-primary. |
| `Quote` / `QuoteLine` | A priced proposal, convertible to an order or invoice. |
| `SalesOrder` / `SalesOrderLine` | An accepted commitment; drives fulfilment and invoicing. |
| `Invoice` / `InvoiceLine` | The AR document. Immutable once issued. |
| `CreditNote` / `CreditNoteLine` | The mirror document reducing AR. |
| `RecurringInvoiceTemplate` | Schedule + template producing invoices. |
| `PaymentTerm` | Named terms (Net 30, 2/10 Net 30) driving `due_date`. |

**`sales_customer`** — `TenantScopedModel`.
`code`, `display_name`, `legal_name`, `tax_registration_number`, `currency`,
`payment_term` FK `PROTECT`, `credit_limit MoneyField`, `receivable_account` nullable FK
`PROTECT` (defaults to `ar_control`), `billing_address jsonb`, `shipping_address jsonb`,
`is_active`, `notes`.
`uq_customer_code` on `(tenant, code)`; `ck_customer_credit_limit_non_negative`;
indexes `(tenant, is_active)`, and a `pg_trgm` GIN index on `display_name` for search
(`ILIKE '%acme%'` is unindexable with a B-tree).

**`sales_invoice`** — `ImmutableFinancialModel`.

| Column | Notes |
|---|---|
| `customer` FK `PROTECT` | you cannot delete a customer with history |
| `number varchar(32)` | blank while `DRAFT`; allocated at `DRAFT → SENT` |
| `status` | `draft\|sent\|partially_paid\|paid\|voided\|written_off`, indexed. **No `overdue` value** — overdue is derived (FR-INV-04) |
| `issue_date`, `due_date`, `delivery_date` | |
| `currency`, `exchange_rate` | |
| `subtotal_amount`, `discount_amount`, `tax_amount`, `total_amount`, `paid_amount`, `balance_due` | all `MoneyField` |
| `payment_term` FK `PROTECT`, `project` nullable FK `PROTECT`, `sales_order` nullable FK `PROTECT`, `quote` nullable FK `SET_NULL` | |
| `journal_entry` nullable OneToOne → `accounting.JournalEntry` `PROTECT` | the GL effect of issuance |
| `notes`, `terms`, `internal_memo` | |
| `pdf_attachment` nullable FK → `core.Attachment` `SET_NULL`, `pdf_sha256` | proves what the customer received |
| `sent_at`, `voided_at`, `written_off_at`, `write_off_entry` | |

Constraints: `uq_invoice_number` on `(tenant, number)` partial where `number <> ''`;
`ck_invoice_due_after_issue`; `ck_invoice_amounts_non_negative`;
`ck_invoice_balance` — `balance_due = total_amount - paid_amount`;
`ck_invoice_paid_not_over` — `paid_amount <= total_amount`;
`ck_invoice_draft_has_no_number`.
Indexes: `(tenant, -created_at)`; `ix_invoice_status` on `(tenant, status, due_date)`
(drives both the list filter and the ageing report);
`ix_invoice_customer` on `(tenant, customer, status)`;
`ix_invoice_issue_date` on `(tenant, issue_date)`;
partial `ix_invoice_open` on `(tenant, due_date)` where
`status IN ('sent','partially_paid')` — the ageing report reads only open invoices, and
a partial index keeps it small forever.

**`sales_invoice_line`** — `TenantScopedModel`.
`invoice` FK **CASCADE** (child of an unposted parent — permitted by `CONVENTIONS.md` §5),
`line_number`, `item` nullable FK → `inventory.Item` `PROTECT`, `description`,
`quantity QuantityField`, `unit_price MoneyField`, `discount_rate RateField`,
`discount_amount`, `tax_rate` nullable FK `PROTECT`, `tax_amount`, `line_total`,
`revenue_account` FK `PROTECT`, `project` nullable FK `PROTECT`, `department` nullable FK
`PROTECT`, `unit_of_measure`, `item_classification_code` (e-invoicing readiness).
`uq_invoice_line_number` on `(invoice, line_number)`;
`ck_invoice_line_quantity_positive`; `ck_invoice_line_amounts_non_negative`;
`ck_invoice_line_currency_matches_parent` (or a `clean()` assertion — the parent pins the
currency, per `CONVENTIONS.md` §2).

**`sales_credit_note` / `_line`** — same shape; `invoice` nullable FK `PROTECT`,
`reason`, `applied_amount`, `unapplied_amount`.
`ck_credit_note_not_over_applied` and a service check that the note never exceeds the
originating invoice net of prior notes.

**`sales_recurring_template`** — `frequency` (`weekly|monthly|quarterly|yearly`),
`interval`, `next_run_date`, `end_date`, `occurrences_generated`,
`create_as` (`draft|sent`), `template_payload jsonb`.
Plus `sales_recurring_occurrence(template, occurrence_date, invoice)` with
`uq_recurring_occurrence` on `(tenant, template, occurrence_date)` — this is what makes
regeneration idempotent (AC-INV-06).

---

## 7. `purchasing` and `payments` 🔨

### 7.1 `purchasing`

| Entity | Purpose |
|---|---|
| `Vendor` | A party we owe. AP subledger identity. Mirrors `Customer`. |
| `PurchaseOrder` / `PurchaseOrderLine` | A commitment to buy; drives receipt and bill matching. |
| `VendorBill` / `VendorBillLine` | The AP document. Immutable once approved. |

`purchasing_vendor`: `uq_vendor_code` on `(tenant, code)`; `payable_account` defaults to
`ap_control`.
`purchasing_vendor_bill`: `vendor` FK `PROTECT`, `vendor_reference` (their invoice
number), `status` (`draft|awaiting_approval|approved|partially_paid|paid|voided`),
`bill_date`, `due_date`, amounts as on `Invoice`, `journal_entry` OneToOne `PROTECT`.
`uq_bill_vendor_reference` on `(tenant, vendor, vendor_reference)` partial where
non-empty — duplicate vendor invoices are the classic AP fraud/error vector and the
database should refuse them.
Three-way match (PO ↔ receipt ↔ bill) tolerance lives in `Tenant.settings`.

### 7.2 `payments`

| Entity | Purpose |
|---|---|
| `PaymentMethod` | Tenant-configured method (bank transfer, cash, card, wallet) mapped to a GL account. |
| `GatewayAccount` | Credentials/config for one provider connection. Secrets by reference, never in the row. |
| `Payment` | One money movement in or out. |
| `PaymentAllocation` | How much of a payment settles which invoice or bill. |
| `Refund` | A returned payment, posted as a reversal path. |
| `Dispute` | A chargeback/dispute lifecycle against a payment. |
| `WebhookEvent` | Durable record of an inbound provider event. |

**`payments_payment`** — `ImmutableFinancialModel`.

| Column | Notes |
|---|---|
| `direction` | `inbound\|outbound` |
| `customer` / `vendor` | nullable FKs `PROTECT`; exactly one set, enforced by `ck_payment_one_counterparty` |
| `number` | own sequence scope |
| `status` | `pending\|authorized\|captured\|settled\|failed\|refunded\|partially_refunded\|disputed`, indexed |
| `amount`, `currency`, `exchange_rate`, `fee_amount`, `net_amount` | fee comes from the settlement payload, never estimated |
| `payment_method` FK `PROTECT`, `gateway_account` nullable FK `PROTECT` | |
| `bank_account` nullable FK → `banking.BankAccount` `PROTECT` | |
| `provider_reference`, `provider_payment_id` | |
| `card_last4`, `card_brand`, `card_expiry_month/year`, `gateway_token` | **never a PAN or CVV** |
| `payment_date`, `authorized_at`, `captured_at`, `settled_at`, `failed_reason` | |
| `capture_entry`, `settlement_entry`, `refund_entry` | nullable OneToOne → `JournalEntry` `PROTECT` — three distinct economic events, three entries |
| `idempotency_key` | |

Constraints: `uq_payment_number`; `uq_payment_provider_id` on
`(tenant, gateway_account, provider_payment_id)` partial where non-empty;
`ck_payment_amount_positive`; `ck_payment_fee_non_negative`;
`ck_payment_net_equals_amount_minus_fee`; `ck_payment_one_counterparty`.
Indexes: `ix_payment_status` on `(tenant, status, payment_date)`;
`ix_payment_customer` on `(tenant, customer, status)`;
`ix_payment_provider` on `(tenant, provider_payment_id)`.

**`payments_allocation`** — `ImmutableFinancialModel`.
`payment` FK `PROTECT`, `invoice` nullable FK `PROTECT`, `bill` nullable FK `PROTECT`,
`credit_note` nullable FK `PROTECT`, `amount`, `allocated_at`, `reversed_at`,
`reversal_of` self-FK.
`ck_allocation_one_target` (exactly one of invoice/bill/credit_note);
`ck_allocation_amount_positive`;
`uq_allocation_payment_target` on `(payment, invoice, bill, credit_note)` partial where
`reversed_at IS NULL`.
Allocations are never deleted — they are reversed, which reverses the GL effect.

**`payments_webhook_event`** — deliberately **not** tenant-scoped at write time: the
tenant is resolved *during* processing from `gateway_account`, and an event for an
unknown account must still be persisted for forensics.
`provider`, `provider_event_id`, `event_type`, `signature_valid bool`,
`raw_payload jsonb`, `received_at`, `status` (`received|processing|processed|failed|parked`),
`attempts`, `last_error`, `processed_at`, `tenant` nullable FK, `payment` nullable FK.
**`uq_webhook_provider_event` on `(provider, provider_event_id)`** — this single unique
index is what makes redelivery a cheap no-op (ADR-006).
Indexes: `ix_webhook_status` on `(status, received_at)`; `ix_webhook_tenant` on
`(tenant, received_at)`.

---

## 8. `expenses` 🔨

| Entity | Purpose |
|---|---|
| `ExpenseCategory` | Maps a spend type to an expense account and a default tax rate. |
| `ExpenseClaim` | A batch of expenses submitted by one employee for approval. |
| `ExpenseClaimLine` | One receipt-backed expense within a claim. |
| `MileageRate` | Per-distance reimbursement rate, effective-dated. |
| `ExpenseApproval` | One approval step, recording who, when, and at what limit. |

`expenses_claim` — `TenantScopedModel` (it becomes immutable at `APPROVED`, enforced by
service + trigger rather than by the base class, because it is editable while `DRAFT`).
`employee` FK → `hr.Employee` `PROTECT`, `number`, `status`
(`draft|submitted|approved|rejected|reimbursed|cancelled`, indexed), `title`, `submitted_at`,
`approved_at`, `approved_by`, `rejected_reason`, `total_amount`, `reimbursed_amount`,
`currency`, `department` FK `PROTECT`, `project` nullable FK `PROTECT`,
`reimbursement_method` (`bank|payroll|cash`), `payroll_run` nullable FK `SET_NULL`,
`approval_entry` / `reimbursement_entry` nullable OneToOne → `JournalEntry` `PROTECT`.
`uq_expense_claim_number`; `ck_expense_claim_total_non_negative`;
`ck_expense_reimbursed_not_over_total`.
Indexes: `ix_claim_status` on `(tenant, status, submitted_at)`;
`ix_claim_employee` on `(tenant, employee, status)`.

`expenses_claim_line`: `claim` FK **CASCADE**, `line_number`, `category` FK `PROTECT`,
`expense_date`, `description`, `gross_amount`, `tax_rate` nullable FK `PROTECT`,
`tax_amount`, `net_amount`, `is_billable`, `project` nullable FK `PROTECT`,
`vendor_name`, `attachment` FK → `core.Attachment` `PROTECT`,
`distance_km` + `mileage_rate` for mileage lines.
`ck_claim_line_amounts_non_negative`; `ck_claim_line_receipt_required` (service-level
against the tenant threshold).
Non-recoverable input VAT is folded into the expense debit — no input VAT line at all
(AC-EXP-03).

`expenses_approval`: `claim` FK `CASCADE`, `step`, `approver` FK → `iam.User` `PROTECT`,
`decision` (`pending|approved|rejected`), `decided_at`, `comment`, `limit_applied`.
`uq_approval_claim_step`. Self-approval is blocked unconditionally, whatever permissions
the actor holds.

---

## 9. `inventory` 🔨

| Entity | Purpose |
|---|---|
| `ItemCategory` | Groups items and supplies default GL accounts. |
| `Item` | A sellable/purchasable product or service. |
| `UnitOfMeasure` | Unit with a conversion factor to a base unit. |
| `Warehouse` | A physical or logical stock location. |
| `StockLevel` | Current quantity and weighted-average cost per (item, warehouse). |
| `StockMovement` | Append-only ledger of every quantity change. |
| `StockAdjustment` | A counted correction, header for adjustment movements. |
| `StockTransfer` | Movement between warehouses, optionally in-transit. |
| `PriceList` / `PriceListItem` | Customer- or currency-specific pricing. |

**`inventory_item`**: `sku`, `name`, `description`, `type` (`stock|service|non_stock`),
`category` FK `PROTECT`, `unit_of_measure` FK `PROTECT`, `sale_price`, `purchase_price`,
`tax_rate` nullable FK `PROTECT`, `income_account` FK `PROTECT`,
`expense_account` FK `PROTECT`, `inventory_account` nullable FK `PROTECT`,
`is_tracked bool`, `reorder_level QuantityField`, `barcode`, `is_active`,
`hs_code`/`classification_code`.
**`uq_item_sku` on `(tenant, sku)`** — per tenant, never global (FR-STK-05; a global
unique index tells tenant A whether tenant B uses a SKU).
`ck_item_prices_non_negative`; `ck_item_tracked_has_inventory_account`.
Indexes: `(tenant, is_active)`, trigram GIN on `name` and `sku` for search.

**`inventory_stock_level`**: `item` FK `PROTECT`, `warehouse` FK `PROTECT`,
`quantity_on_hand`, `quantity_reserved`, `quantity_available` (generated or maintained),
`average_cost MoneyField`, `total_value MoneyField`, `last_movement_at`.
`uq_stock_level_item_warehouse` on `(tenant, item, warehouse)`;
`ck_stock_level_cost_non_negative`.
Updated only inside the movement transaction, with `SELECT ... FOR UPDATE` on the level
row — average-cost recomputation is a read-modify-write and *must* serialise per
(item, warehouse).

**`inventory_stock_movement`** — `ImmutableFinancialModel` (FR-STK-03).
`item` FK `PROTECT`, `warehouse` FK `PROTECT`, `movement_type`
(`receipt|issue|adjustment|transfer_out|transfer_in|opening`),
`quantity` (always positive; the type carries the direction — same reasoning as
separate debit/credit columns, ADR-003), `unit_cost`, `total_cost`,
`average_cost_after`, `quantity_after`, `movement_date`,
`source_document_type`, `source_document_id`,
`journal_entry` nullable FK → `JournalEntry` `PROTECT`, `idempotency_key`.
`ck_movement_quantity_positive`; `ck_movement_cost_non_negative`;
`uq_movement_idempotency` on `(tenant, idempotency_key)` partial.
Indexes: `ix_movement_item_date` on `(tenant, item, movement_date)`;
`ix_movement_warehouse` on `(tenant, warehouse, movement_date)`;
`ix_movement_source` on `(source_document_type, source_document_id)`.
`quantity_after` and `average_cost_after` are stored so a valuation as-of any date is a
lookup rather than a replay of the whole movement history.

---

## 10. `banking` 🔨

| Entity | Purpose |
|---|---|
| `BankAccount` | A real bank or cash account, bound to a reconcilable GL account. |
| `BankStatement` | One imported statement with opening/closing balances. |
| `BankStatementLine` | One line on a statement, deduplicated on import. |
| `MatchRule` | Tenant rule proposing a match or a categorisation. |
| `Reconciliation` | A reconciliation session for an account and date range. |
| `ReconciliationMatch` | Links statement lines to journal lines, many-to-many. |

**`banking_bank_account`**: `name`, `account_number_masked`, `iban`, `swift`,
`bank_name`, `branch`, `currency`, `gl_account` **OneToOne** FK `PROTECT` (must have
`is_reconcilable = True` — asserted on save), `is_active`, `opening_balance`,
`opening_balance_date`, `last_reconciled_at`, `last_statement_balance`.
`uq_bank_account_gl` on `(tenant, gl_account)` — two bank accounts pointing at one GL
account makes reconciliation meaningless.

**`banking_statement_line`**: `statement` FK `CASCADE`, `line_number`, `value_date`,
`booking_date`, `description`, `bank_reference`, `counterparty_name`,
`counterparty_account`, `amount` (signed here — a statement is an external document and
we transcribe it as given; the *ledger* is where the debit/credit discipline applies),
`currency`, `running_balance`, `status` (`unmatched|proposed|matched|ignored`, indexed),
`dedupe_hash char(64)`.
**`uq_stmt_line_dedupe` on `(tenant, bank_account, dedupe_hash)`** — re-importing the
same file is a no-op (AC-BNK-01).
Indexes: `ix_stmt_line_status` on `(tenant, bank_account, status, value_date)`.

**`banking_reconciliation_match`**: `reconciliation` FK `CASCADE`, `statement_line` FK
`PROTECT`, `journal_line` FK `PROTECT`, `amount`, `confidence RateField`,
`status` (`proposed|confirmed|rejected`), `matched_by`, `matched_at`, `rule` nullable FK
`SET_NULL`.
Many-to-many by construction: one statement line may settle three invoices, three lines
may settle one. Only `CONFIRMED` sets `JournalLine.reconciled_at`.
`ck_match_amount_positive`; `ix_match_statement_line`; `ix_match_journal_line`.

---

## 11. `projects` 🔨

| Entity | Purpose |
|---|---|
| `Project` | A billable or internal engagement; a reporting dimension on every journal line. |
| `ProjectMember` | An employee's participation, with their rate on this project. |
| `Task` | A unit of work within a project. |
| `TimesheetEntry` | Hours worked by an employee on a task on a date. |
| `ProjectBudget` | Budgeted hours and amount, by category. |
| `ProjectExpenseAllocation` | Links non-time costs to a project. |

**`projects_project`**: `code`, `name`, `customer` nullable FK `PROTECT`,
`manager` FK → `hr.Employee` `PROTECT`, `department` nullable FK `PROTECT`,
`status` (`planned|active|on_hold|completed|cancelled`, indexed),
`billing_type` (`fixed_price|time_and_materials|non_billable`), `currency`,
`default_billing_rate`, `contract_amount`, `budget_hours`, `budget_amount`,
`start_date`, `end_date`, `is_billable`.
`uq_project_code` on `(tenant, code)`; `ck_project_date_order`;
`ck_project_amounts_non_negative`.
Indexes: `ix_project_status` on `(tenant, status)`; `ix_project_customer`.

**`projects_timesheet_entry`** — `TenantScopedModel`, becomes immutable at `INVOICED`.
`employee` FK `PROTECT`, `project` FK `PROTECT`, `task` nullable FK `PROTECT`,
`work_date`, `hours QuantityField`, `description`, `is_billable`,
`billing_rate MoneyField` and `cost_rate MoneyField` — **resolved and stored at entry
time** (task override → project member → project default → employee default), so a later
rate change never rewrites history (AC-PRJ-01),
`status` (`draft|submitted|approved|invoiced`, indexed), `submitted_at`, `approved_at`,
`approved_by`, `invoice_line` nullable FK → `sales.InvoiceLine` `SET_NULL`.
`ck_timesheet_hours_positive`; `ck_timesheet_hours_max` (`hours <= 24`);
`ck_timesheet_rates_non_negative`;
`uq_timesheet_invoice_line` partial on `(invoice_line)` where not null.
Indexes: `ix_timesheet_employee_date` on `(tenant, employee, work_date)`;
`ix_timesheet_project_status` on `(tenant, project, status)`;
partial `ix_timesheet_billable` on `(tenant, project, work_date)` where
`is_billable AND status = 'approved'` — the "what can I bill?" query, kept small.

`invoice_line` is `SET_NULL` **on purpose**: voiding an invoice returns its entries to
`APPROVED` so they can be re-billed rather than being orphaned in `INVOICED` (AC-PRJ-02).

---

## 12. `hr` 🔨

| Entity | Purpose |
|---|---|
| `Department` | A node in the org tree; a reporting dimension on every journal line. |
| `Position` | A job title with a grade and a default salary band. |
| `Employee` | A person employed by the tenant. |
| `EmploymentContract` | An effective-dated employment agreement. |
| `EmployeeDocument` | A stored document with an expiry. |
| `LeaveType` | A category of leave with its accrual policy. |
| `LeaveBalance` | Current entitlement per (employee, leave type, year). |
| `LeaveTransaction` | Append-only ledger of accruals, holds, usage and expiry. |
| `LeaveRequest` | An employee's request and its approval chain. |
| `Shift` | A working pattern. |
| `AttendanceRecord` | A clock-in/out pair with derived worked and overtime hours. |
| `Holiday` | A tenant/country public holiday. |

**`hr_department`**: `code`, `name`, `parent` self-FK `PROTECT`,
`manager` nullable FK → `Employee` `SET_NULL`, `cost_center_code`,
`path varchar(255)` (materialised ancestor path, e.g. `/root/ops/cairo/`), `depth`,
`is_active`.
`uq_department_code` on `(tenant, code)`; `ck_department_no_self_parent`.
Indexes: `ix_department_parent`; `ix_department_path` (`varchar_pattern_ops`, so
`path LIKE '/root/ops/%'` resolves the whole subtree in one index range scan).
The materialised `path` exists because ABAC `department_subtree` runs on *every request*
for a department manager; an N+1 tree walk there is not acceptable (AC-HR-02).
Reparenting rewrites `path` for the subtree but **never** rewrites historical
`JournalLine.department` — cost history stays as reported.

**`hr_employee`**: `employee_code`, `first_name`, `last_name`, `first_name_ar`,
`last_name_ar` (Arabic legal names are required on payroll and statutory filings in the
launch markets, and transliteration is not acceptable), `national_id_encrypted`,
`date_of_birth`, `gender`, `marital_status`, `nationality`, `personal_email`,
`work_email`, `phone`, `address jsonb`,
`department` FK `PROTECT`, `position` FK `PROTECT`, `manager` nullable self-FK `SET_NULL`,
`hire_date`, `termination_date`, `termination_reason`,
`status` (`active|on_leave|suspended|terminated`, indexed),
`employment_type` (`full_time|part_time|contract|intern`),
`bank_account_encrypted`, `bank_name`, `iban_encrypted`,
`social_insurance_number_encrypted`, `tax_id`,
`base_salary MoneyField`, `currency`, `salary_structure` nullable FK `PROTECT`.
**`uq_employee_code` on `(tenant, employee_code)`** (FR-HR-01);
`ck_employee_termination_after_hire`;
`ck_employee_terminated_has_date` — `status = 'terminated'` ⇒ `termination_date IS NOT NULL`.
Indexes: `ix_employee_status` on `(tenant, status)`;
`ix_employee_department` on `(tenant, department, status)`;
`ix_employee_manager` on `(tenant, manager)`.
Salary and identity columns are column-encrypted (SEC-12) on top of volume encryption.

**`hr_leave_transaction`** — append-only.
`employee` FK `PROTECT`, `leave_type` FK `PROTECT`, `year`,
`transaction_type` (`accrual|hold|release|usage|adjustment|carry_over|expiry`),
`days QuantityField` (signed — this is an entitlement ledger, not the GL),
`effective_date`, `leave_request` nullable FK `PROTECT`, `reason`, `created_by`.
Index `ix_leave_txn_balance` on `(tenant, employee, leave_type, year)`.
Balance = `SUM(days)` over this table. There is no mutable balance column that can drift;
`hr_leave_balance` is a cached projection with a nightly recompute, treated exactly like
`Account.cached_balance` (ADR-010).

**`hr_leave_request`**: `employee` FK `PROTECT`, `leave_type` FK `PROTECT`,
`start_date`, `end_date`, `days_requested`, `is_half_day_start/end`, `reason`,
`status` (`draft|submitted|pending_manager|pending_hr|approved|rejected|cancelled`, indexed),
`manager_approver`, `manager_decided_at`, `hr_approver`, `hr_decided_at`,
`rejection_reason`, `hold_transaction` nullable OneToOne → `LeaveTransaction` `PROTECT`.
`ck_leave_date_order`; `ck_leave_days_positive`;
`uq_leave_no_overlap` — an exclusion constraint (`EXCLUDE USING gist`) on
`(tenant WITH =, employee WITH =, daterange(start_date, end_date, '[]') WITH &&)` where
`status IN ('approved','pending_manager','pending_hr')`. This is the one place a GiST
exclusion constraint earns its keep: overlapping approved leave is otherwise a race
between two approvers (AC-HR-04).

**`hr_attendance_record`**: `employee` FK `PROTECT`, `work_date`, `shift` nullable FK
`PROTECT`, `clock_in`, `clock_out`, `worked_hours`, `overtime_hours`, `late_minutes`,
`early_leave_minutes`, `source` (`web|mobile|biometric|manual`), `geo_lat`, `geo_lng`,
`is_approved`, `notes`.
`uq_attendance_employee_date` on `(tenant, employee, work_date)`;
`ck_attendance_clock_order`.
Derived `overtime_hours` here is the **only** source of the payroll overtime component
(AC-HR-05) — there is no free-text overtime field on the payroll run.

---

## 13. `payroll` 🔨

| Entity | Purpose |
|---|---|
| `PayComponent` | An earning, deduction or employer contribution with a calculation method. |
| `SalaryStructure` / `SalaryStructureComponent` | A named, versioned bundle of components. |
| `EmployeeSalaryComponent` | Per-employee override or one-off. |
| `StatutoryRule` | Effective-dated tax bands and insurance ceilings, per country. |
| `PayrollRun` | One payroll cycle for a set of employees. |
| `Payslip` | One employee's result within a run. |
| `PayslipLine` | One component's computed value on a payslip. |
| `PayrollPayment` | The disbursement of a run, and the bank file it produced. |

**`payroll_pay_component`**: `code`, `name`, `name_ar`,
`component_type` (`earning|deduction|employer_contribution`),
`calculation_method` (`fixed|percentage_of_base|formula|attendance_derived|statutory`),
`amount`, `rate`, `formula_expression` (a restricted AST over a whitelisted variable
set — **never `eval()`**; an injection bug here is a payroll-wide breach and a fraud
vector), `sequence smallint`, `is_taxable`, `is_insurable`, `affects_gross`,
`debit_account` FK `PROTECT`, `credit_account` FK `PROTECT`, `is_active`.
`uq_pay_component_code` on `(tenant, code)`;
`ck_component_sequence_positive`; `ck_component_rate_fraction`.
A component may only reference components of a strictly lower `sequence`; a cycle is a
validation error at save time, not a runtime infinite loop (AC-PRL-01).

**`payroll_run`** — `TenantScopedModel` until `POSTED`, then immutable by service +
trigger.
`number`, `name`, `period_start`, `period_end`, `payment_date`,
`fiscal_period` FK `PROTECT`,
`status` (`draft|calculating|calculated|pending_approval|approved|posted|paid|cancelled`, indexed),
`run_type` (`regular|off_cycle|bonus|final_settlement`),
`department` nullable FK `PROTECT` (scoped runs), `currency`,
`employee_count`, `total_gross`, `total_deductions`, `total_employer_cost`, `total_net`,
`calculated_at`, `calculated_by`, `approved_at`, `approved_by`, `posted_at`,
`journal_entry` nullable OneToOne → `JournalEntry` `PROTECT`,
`payment_entry` nullable OneToOne → `JournalEntry` `PROTECT`,
`calculation_snapshot jsonb` — the salary structures, component definitions and
statutory bands actually used, frozen, so a policy change next month cannot alter last
month's numbers (AC-PRL-03),
`cancelled_reason`, `idempotency_key`.
`uq_payroll_run_number`; `ck_payroll_period_order`;
`ck_payroll_totals_non_negative`;
`uq_payroll_run_period` on `(tenant, period_start, period_end, run_type, department)`
partial where `status <> 'cancelled'` — you cannot accidentally run March twice.
Indexes: `ix_payroll_run_status` on `(tenant, status, period_end)`.
`calculated_by <> approved_by` is enforced at service level (FR-PRL-08); overriding it
requires a tenant setting and is audit-logged.

**`payroll_payslip`** — `ImmutableFinancialModel` once the run is posted.
`run` FK `PROTECT`, `employee` FK `PROTECT`, `number`,
`gross_amount`, `total_earnings`, `total_deductions`, `employer_contributions`,
`taxable_amount`, `income_tax`, `social_insurance_employee`,
`social_insurance_employer`, `net_amount`, `currency`,
`worked_days`, `absent_days`, `overtime_hours`, `leave_days`,
`department` FK `PROTECT` (snapshotted — an employee who transfers next month must not
retroactively move last month's cost), `cost_center_code`,
`bank_account_masked`, `payment_status` (`unpaid|paid|failed`),
`pdf_attachment` nullable FK `SET_NULL`.
`uq_payslip_run_employee` on `(tenant, run, employee)`;
`ck_payslip_net_equals_gross_minus_deductions`;
`ck_payslip_amounts_non_negative`.
Indexes: `ix_payslip_employee` on `(tenant, employee, -created_at)`;
`ix_payslip_run` on `(tenant, run)`.

**`payroll_payslip_line`**: `payslip` FK `CASCADE`, `component` FK `PROTECT`,
`sequence`, `description`, `description_ar`, `quantity`, `rate`, `amount`,
`component_type`, `is_taxable`, `calculation_trace jsonb` (what inputs produced this
number — the difference between "payroll is wrong" and "here is why").
`uq_payslip_line_component` on `(payslip, component)`;
`ck_payslip_line_amount_non_negative`.

**`payroll_statutory_rule`**: `country`, `rule_type` (`income_tax|social_insurance|other`),
`effective_from`, `effective_to`, `bands jsonb`, `ceiling_amount`, `floor_amount`,
`employee_rate`, `employer_rate`, `is_active`.
`uq_statutory_rule` on `(tenant, country, rule_type, effective_from)`;
`ck_statutory_period_order`.
The run selects the rule effective at `period_end`; changing the 2027 band cannot alter
a 2026 payslip (AC-PRL-07).

---

## 14. `reporting` 🔨

| Entity | Purpose |
|---|---|
| `ReportDefinition` | A code-backed report and its parameter schema. Reports are code, not a designer. |
| `SavedReport` | A tenant's saved parameter set for a report. |
| `ReportRun` | One execution: parameters, timing, result hash, output location. |
| `ExportJob` | An async CSV/XLSX/PDF export and its signed artefact. |
| `PeriodBalance` | Materialised per-(period, account) totals. A cache, rebuildable from `JournalLine`. |
| `IntegrityCheckRun` | One night's result for one tenant across all integrity checks. |

**`reporting_period_balance`**: `fiscal_period` FK `PROTECT`, `account` FK `PROTECT`,
`currency`, `opening_balance`, `period_debit`, `period_credit`, `closing_balance`,
`base_period_debit`, `base_period_credit`, `line_count`, `computed_at`.
`uq_period_balance` on `(tenant, fiscal_period, account, currency)`;
Index `ix_period_balance_account` on `(tenant, account, fiscal_period)`.
This is the mitigation that turns a trial balance from a 100 M-row scan into a
few-thousand-row read (see §17). It is **never** the source of truth — it is rebuildable
at any time from `JournalLine`, and the nightly job verifies it.

**`reporting_integrity_check_run`**: `check_name`, `run_date`, `status`
(`passed|failed|error`), `detail jsonb`, `duration_ms`.
`uq_integrity_run` on `(tenant, check_name, run_date)`.
Storing a row per night per check gives a *history*, not just an alert — which is what
lets you answer "when did this start?".

**`reporting_export_job`**: `report_definition` FK `PROTECT`, `parameters jsonb`,
`format` (`csv|xlsx|pdf`), `status` (`queued|running|completed|failed|expired`),
`row_count`, `attachment` nullable FK `SET_NULL`, `requested_by` FK `PROTECT`,
`expires_at`, `error`.
Every completed export writes an `EXPORT` row to `TenantAuditLog` — bulk data leaving
the system is exactly what an audit trail is for.

---

## 15. Referential integrity map

Every **cross-app** foreign key, its policy, and why. Intra-app parent→child FKs are
listed only where the policy is interesting.

Default per `CONVENTIONS.md` §5: **`PROTECT`**. `CASCADE` is permitted only for child
lines of an unposted parent and for pure join rows. `SET_NULL` is used where the link is
informational and its loss must not destroy the row.

| From | To | Policy | Justification |
|---|---|---|---|
| `core.Attachment.uploaded_by` | `iam.User` | `PROTECT` | The uploader is part of the evidence chain for a receipt. |
| `core.Notification.membership` | `iam.TenantMembership` | `CASCADE` | A notification for a removed member has no meaning and no audit value. |
| `*.created_by` / `*.updated_by` | `iam.User` | `PROTECT` | You cannot delete a user who has touched financial data. Departures are handled by `is_active = False`, never by deletion. |
| `TenantScopedModel.tenant` | `tenancy.Tenant` | `PROTECT` | A tenant with data cannot be deleted; off-boarding is an explicit, audited purge job, not a cascade nobody reviewed. |
| `tenancy.TenantDomain.tenant` | `tenancy.Tenant` | `CASCADE` | Domains are pure routing configuration with no historical value. |
| `tenancy.Subscription.tenant` | `tenancy.Tenant` | `PROTECT` | Billing history must outlive an attempt to delete the tenant row. |
| `tenancy.TenantAuditLog.tenant` | `tenancy.Tenant` | `PROTECT` | The audit log is the last thing that may disappear. |
| `iam.TenantMembership.tenant` / `.user` | `tenancy.Tenant` / `iam.User` | `CASCADE` | The membership is a pure join; its historical facts live in `TenantAuditLog`, which survives. |
| **`iam.TenantMembership.employee`** | **`hr.Employee`** | **`SET_NULL`** | The link is informational. An employee record may be anonymised under GDPR (§6.3 of the TRD) while the login persists, and vice versa. Cascading either way would destroy the other domain's record. |
| `iam.RoleAssignment.membership` | `iam.TenantMembership` | `CASCADE` | Removing a member must remove their grants immediately and completely. |
| `iam.RoleAssignment.role` | `iam.Role` | `PROTECT` | A role that is in use cannot be deleted out from under a live session. |
| **`iam.RoleAssignment.department`** | **`hr.Department`** | **`CASCADE`** | A scope narrowing that points at a deleted department is worse than no assignment: fail closed by removing the grant. |
| **`iam.RoleAssignment.project`** | **`projects.Project`** | **`CASCADE`** | Same reasoning. |
| `iam.ApiKey.role` | `iam.Role` | `PROTECT` | Deleting a role must not silently broaden or void a machine credential. |
| `accounting.Account.parent` | `self` | `PROTECT` | Deleting a parent would orphan the subtree and change every roll-up. |
| `accounting.TaxRate.collected_account` / `.paid_account` | `accounting.Account` | `PROTECT` | An account referenced by a tax rate is load-bearing for every future posting. |
| `accounting.FiscalPeriod.fiscal_year` | `accounting.FiscalYear` | `PROTECT` | |
| `accounting.FiscalPeriod.closed_by` | `iam.User` | `PROTECT` | Who closed the period is an audit fact. |
| `accounting.JournalEntry.journal` / `.period` | — | `PROTECT` | A period or journal with entries is immovable. |
| `accounting.JournalEntry.posted_by` | `iam.User` | `PROTECT` | Attribution of a posting must never be lost. |
| `accounting.JournalEntry.reversal_of` | `self` | `PROTECT` | The original and its reversal are a pair; neither is deletable anyway (`delete()` raises). |
| **`accounting.JournalLine.entry`** | `accounting.JournalEntry` | **`CASCADE`** | The only cascade in the ledger, and it is safe precisely because the parent can never be deleted — `ImmutableFinancialModel.delete()` raises, and the DB role lacks `DELETE`. It exists so that a draft entry being rebuilt replaces its lines cleanly. |
| `accounting.JournalLine.account` | `accounting.Account` | `PROTECT` | An account with postings cannot be deleted; it is archived (`is_active = False`). |
| **`accounting.JournalLine.project`** | **`projects.Project`** | **`PROTECT`** | Deleting a project would silently remove cost history from the P&L. Projects are archived, never deleted. |
| **`accounting.JournalLine.department`** | **`hr.Department`** | **`PROTECT`** | Same: departmental cost history must survive an org restructure. Departments are deactivated and reparented, never deleted. |
| `accounting.JournalLine.tax_rate` | `accounting.TaxRate` | `PROTECT` | The VAT return must be reproducible. |
| `sales.Invoice.customer` | `sales.Customer` | `PROTECT` | |
| `sales.Invoice.journal_entry` | `accounting.JournalEntry` | `PROTECT` | The document and its GL effect are inseparable. |
| **`sales.Invoice.project`** | **`projects.Project`** | **`PROTECT`** | |
| `sales.InvoiceLine.invoice` | `sales.Invoice` | `CASCADE` | Child line of a parent that is only mutable while `DRAFT`. |
| **`sales.InvoiceLine.item`** | **`inventory.Item`** | **`PROTECT`** | An item that has been sold cannot be deleted; the line's description is a snapshot but the link is what makes item profitability computable. |
| `sales.InvoiceLine.revenue_account` | `accounting.Account` | `PROTECT` | |
| `sales.CreditNote.invoice` | `sales.Invoice` | `PROTECT` | |
| `purchasing.VendorBill.vendor` | `purchasing.Vendor` | `PROTECT` | |
| **`payments.Payment.customer` / `.vendor`** | `sales.Customer` / `purchasing.Vendor` | `PROTECT` | |
| **`payments.Payment.bank_account`** | **`banking.BankAccount`** | **`PROTECT`** | |
| `payments.Payment.capture_entry` / `.settlement_entry` / `.refund_entry` | `accounting.JournalEntry` | `PROTECT` | |
| **`payments.PaymentAllocation.payment` / `.invoice` / `.bill`** | — | **`PROTECT`** | An allocation is a financial fact. It is reversed, never deleted — hence no cascade anywhere on this table. |
| `payments.WebhookEvent.tenant` | `tenancy.Tenant` | `SET_NULL` | The raw event must survive even if it cannot be attributed; forensics need it more than referential tidiness. |
| **`expenses.ExpenseClaim.employee`** | **`hr.Employee`** | **`PROTECT`** | A reimbursement is a financial fact tied to a person. |
| **`expenses.ExpenseClaim.payroll_run`** | **`payroll.PayrollRun`** | **`SET_NULL`** | If a run is cancelled before posting, the claim must revert to unreimbursed rather than vanish. |
| `expenses.ExpenseClaimLine.claim` | `expenses.ExpenseClaim` | `CASCADE` | Child of a `DRAFT`-mutable parent. |
| **`expenses.ExpenseClaimLine.attachment`** | **`core.Attachment`** | **`PROTECT`** | Deleting the receipt would leave an unsupported expense — exactly what an auditor disallows. |
| `expenses.ExpenseClaimLine.category` | `expenses.ExpenseCategory` | `PROTECT` | |
| **`inventory.StockMovement.item` / `.warehouse`** | — | **`PROTECT`** | Valuation history must be reconstructible. |
| **`inventory.StockMovement.journal_entry`** | **`accounting.JournalEntry`** | **`PROTECT`** | |
| `inventory.StockLevel.item` / `.warehouse` | — | `PROTECT` | |
| `inventory.Item.income_account` / `.expense_account` / `.inventory_account` | `accounting.Account` | `PROTECT` | |
| **`banking.BankAccount.gl_account`** | **`accounting.Account`** | **`PROTECT`** | The bank account is meaningless without its ledger account. |
| `banking.BankStatementLine.statement` | `banking.BankStatement` | `CASCADE` | A statement can be deleted only while wholly unmatched; the service enforces that, and then the lines go with it. |
| **`banking.ReconciliationMatch.journal_line`** | **`accounting.JournalLine`** | **`PROTECT`** | |
| `banking.ReconciliationMatch.statement_line` | `banking.BankStatementLine` | `PROTECT` | |
| **`projects.Project.customer`** | **`sales.Customer`** | **`PROTECT`** | |
| **`projects.Project.manager`** | **`hr.Employee`** | **`PROTECT`** | |
| **`projects.TimesheetEntry.employee`** | **`hr.Employee`** | **`PROTECT`** | Timesheets are cost and billing evidence. |
| **`projects.TimesheetEntry.invoice_line`** | **`sales.InvoiceLine`** | **`SET_NULL`** | Voiding an invoice must return the entry to `APPROVED` and make it re-billable, not orphan it in `INVOICED` (AC-PRJ-02). This is the one place `SET_NULL` is doing real state-machine work. |
| `hr.Department.parent` | `self` | `PROTECT` | Reparent explicitly; never lose a subtree to a cascade. |
| `hr.Department.manager` | `hr.Employee` | `SET_NULL` | A manager can leave; the department continues. |
| `hr.Employee.department` / `.position` | — | `PROTECT` | |
| `hr.Employee.manager` | `self` | `SET_NULL` | Same reasoning as department manager. |
| `hr.LeaveRequest.employee` / `.leave_type` | — | `PROTECT` | |
| `hr.LeaveTransaction.leave_request` | `hr.LeaveRequest` | `PROTECT` | The entitlement ledger is append-only; it cannot lose its justification. |
| **`payroll.PayrollRun.journal_entry` / `.payment_entry`** | **`accounting.JournalEntry`** | **`PROTECT`** | |
| `payroll.PayrollRun.fiscal_period` | `accounting.FiscalPeriod` | `PROTECT` | |
| **`payroll.Payslip.employee`** | **`hr.Employee`** | **`PROTECT`** | A payslip is a statutory record with a 7-year retention; the employee row cannot be deleted, only anonymised. |
| `payroll.Payslip.run` | `payroll.PayrollRun` | `PROTECT` | |
| **`payroll.Payslip.department`** | **`hr.Department`** | **`PROTECT`** | Snapshotted at run time; departmental payroll cost history is immovable. |
| `payroll.PayslipLine.payslip` | `payroll.Payslip` | `CASCADE` | Recalculation before posting rebuilds lines wholesale; after posting nothing is deletable anyway. |
| `payroll.PayComponent.debit_account` / `.credit_account` | `accounting.Account` | `PROTECT` | |
| `reporting.PeriodBalance.account` / `.fiscal_period` | — | `PROTECT` | |
| `reporting.ExportJob.attachment` | `core.Attachment` | `SET_NULL` | Expired exports have their artefact swept; the job's audit record stays. |

**Circularity note.** `iam ↔ hr` is a genuine dependency cycle at the app level
(`TenantMembership.employee → hr.Employee`, and `hr` references `iam.User` through
`AuditedModel`). It is resolved by string references (`"hr.Employee"`) and nullable FKs;
neither app may import the other's models at module scope. `sales ↔ projects` is the same
pattern (`Project.customer → sales.Customer`, `Invoice.project → projects.Project`).

---

## 16. Indexing strategy

### 16.1 The rules

1. **Every index leads with `tenant_id`.** Every query has an equality predicate on it
   (via the ORM manager and via RLS), so a leading `tenant_id` makes the index usable
   for that predicate and keeps each tenant's rows physically clustered within the index.
   An index without it is unusable for the common query and is dead weight on write.
2. **`(tenant, -created_at)` comes free** from `TenantScopedModel.Meta`. If a model
   overrides `indexes`, it must re-add it — `CONVENTIONS.md` §3 says so, and the reason
   is that "this tenant's rows, newest first" is the shape of almost every list endpoint.
3. **`(tenant, status)` wherever the UI filters by status**, and
   `(tenant, status, <date>)` where it also sorts by date.
4. **`(tenant, <date>)` for anything in a period report** — that is the range-scan the
   report performs.
5. **Every FK used in a list filter is indexed.** Django indexes FK columns by default,
   but the *single-column* index is rarely the useful one; add the composite with
   `tenant` leading.
6. **Partial indexes for "open" subsets.** Open invoices, unreconciled lines, active
   employees, unread notifications. These sets stay small while the table grows without
   bound, so the index stays small forever. `ix_invoice_open` and `ix_line_unreconciled`
   are the load-bearing examples.
7. **Partial unique indexes for "only one active X"**: one primary domain per tenant, one
   open subscription per tenant, one unreversed allocation per (payment, target).
8. **Trigram GIN for name search.** `ILIKE '%acme%'` cannot use a B-tree. Customers,
   vendors, items and employees each get a `pg_trgm` GIN index on their searchable name.
9. **GIN on JSONB only where it is queried.** `Tenant.settings` is queried by feature
   flag, so it gets `jsonb_path_ops`. `calculation_snapshot` and `raw_payload` are
   written and read whole; no index.
10. **Every uniqueness rule that is per-tenant is `(tenant, key)`, never `(key)`.** A
    global unique index on a document number, SKU or employee code is a cross-tenant
    information leak — tenant A learns whether tenant B uses a value, from the error.
11. **Index the FK side you actually traverse.** `ix_entry_source_doc` on
    `(source_document_type, source_document_id)` deliberately omits `tenant` because it
    answers "which entry came from this document?" given a document UUID, which is
    already globally unique.
12. **Do not index low-cardinality booleans alone.** `is_active` earns its place only
    inside a composite or as a partial-index predicate.

### 16.2 Index budget and hygiene

* Writes pay for reads. `accounting_journal_line` has six indexes and is the highest
  insert-rate table in the system; adding a seventh needs a measured justification.
* All production index builds are `CREATE INDEX CONCURRENTLY`, in their own migration,
  never in the same migration as a table alteration.
* `pg_stat_user_indexes` is reviewed monthly; an index with `idx_scan = 0` after a full
  quarter (covering month-end and year-end) is dropped.
* `pg_stat_statements` is reviewed weekly; any statement with mean time > 100 ms gets a
  ticket (see the performance budgets in the TRD §5.1).
* `default_statistics_target` is raised on `journal_line.account_id` and
  `journal_entry.entry_date`; skewed distributions there produce bad plans on reports.

---

## 17. Row-count growth model

### 17.1 Growth classes

| Class | Behaviour | Tables |
|---|---|---|
| **A — Fixed per tenant** | Tens to hundreds of rows, never grows with volume | `Tenant`, `Subscription`, `Journal`, `FiscalYear`, `FiscalPeriod` (12/yr), `Account` (100–500), `TaxRate`, `Warehouse`, `Department`, `Position`, `LeaveType`, `PayComponent`, `SalaryStructure`, `ReportDefinition` |
| **B — Grows with the customer's business size** | Thousands, roughly linear in headcount/customer count, not in transaction volume | `User`, `TenantMembership`, `RoleAssignment`, `Customer`, `Vendor`, `Item`, `Employee`, `Project`, `BankAccount` |
| **C — Linear in transaction volume** | Millions per year for an active tenant | `Invoice`, `InvoiceLine`, `VendorBill`, `Payment`, `PaymentAllocation`, `ExpenseClaimLine`, `StockMovement`, `BankStatementLine`, `TimesheetEntry`, `AttendanceRecord`, `Payslip`, `PayslipLine`, `Notification`, `TenantAuditLog`, `WebhookEvent`, **`JournalEntry`**, **`JournalLine`** |
| **D — Multiplicative** | Grows as the product of two class-C drivers | **`JournalLine`** (entries × lines/entry), `PayslipLine` (employees × components × runs) |

### 17.2 Sizing a "large" tenant

Assumptions: 200 employees, 2 000 invoices/month, 1 500 bills/month, 3 000 payments/month,
400 stock movements/day, 200 employees × 20 timesheet entries/month.

| Table | Rows/year | Rows at 5 years | Bytes/row (with indexes) | 5-year size |
|---|---|---|---|---|
| `sales_invoice` | 24 k | 120 k | ~1.2 kB | ~150 MB |
| `sales_invoice_line` | 120 k | 600 k | ~0.6 kB | ~360 MB |
| `payments_payment` | 36 k | 180 k | ~0.9 kB | ~160 MB |
| `inventory_stock_movement` | 146 k | 730 k | ~0.5 kB | ~365 MB |
| `projects_timesheet_entry` | 48 k | 240 k | ~0.5 kB | ~120 MB |
| `payroll_payslip_line` | 200 × 25 × 12 = 60 k | 300 k | ~0.4 kB | ~120 MB |
| `accounting_journal_entry` | ~70 k | 350 k | ~0.8 kB | ~280 MB |
| **`accounting_journal_line`** | **~350 k** | **~1.75 M** | **~0.7 kB** | **~1.2 GB** |

At 10 000 tenants with a long tail, `journal_line` is the table that decides the
architecture. The 100 M-row threshold in the scaling path (`02-architecture.md` §9.3) is
reached by either a single very large tenant or the aggregate across the fleet — both
are handled the same way.

### 17.3 Mitigations, in the order we apply them

| Pressure | Mitigation | Trigger |
|---|---|---|
| Reports scan `journal_line` over wide date ranges | Route all reporting reads to a **read replica** | Immediately at GA — this is a design default, not a reaction |
| Trial balance / P&L aggregate millions of lines | Materialise **`reporting.PeriodBalance`** per (period, account); maintained on posting, rebuildable from `JournalLine` | When a trial balance exceeds the 1.5 s p95 budget |
| `journal_line` insert and vacuum cost | **RANGE partition by period** (monthly), with `entry_date`/`period_start` denormalised onto the line so the partition key is present in every unique index | ~100 M rows in a tenant, or ~500 M overall |
| Closed years are read rarely but occupy hot storage | `DETACH PARTITION` closed fiscal years to a cold tablespace; reads remain possible, latency-tolerant | After a year is closed and filed |
| `TenantAuditLog` grows unbounded and is append-only | Partition by month; retain 7 years; the recent partition stays small and hot | 50 M rows |
| `WebhookEvent` raw payloads dominate storage | 30-day retention on the payload column (null it out), keep the metadata row forever | Immediately — it is a scheduled job from day one |
| `Notification` grows with users × events | Delete read notifications older than 90 days; unread older than 1 year | 10 M rows |
| `AttendanceRecord` grows at headcount × 365 | No mitigation needed — 200 employees is 73 k rows/year. Only relevant for a 10 000-employee tenant, where monthly partitioning applies | 20 M rows |
| `reporting.ExportJob` artefacts fill object storage | Lifecycle rule expires artefacts after 30 days; job rows survive for the audit trail | Immediately |
| A single tenant outgrows the cluster | **Shard by tenant.** Every business row already carries `tenant_id` and every query already filters on it, so this is a routing change rather than a data-model change | Last resort; deliberately deferred by ADR-001 |

### 17.4 What we deliberately do not do

* **No soft-delete columns on financial tables.** `is_deleted` invites a query that
  forgets it. Financial documents are voided or reversed, which is a *state*, not a
  hidden row.
* **No archiving that removes rows from the primary within the retention window.**
  Partition detachment moves storage; it does not make a 7-year-old invoice unreadable.
* **No summary table that is the source of truth.** `PeriodBalance` and
  `Account.cached_balance` are both caches, both rebuildable, and both verified nightly
  (ADR-010). A summary that cannot be rebuilt is a liability, not an optimisation.
