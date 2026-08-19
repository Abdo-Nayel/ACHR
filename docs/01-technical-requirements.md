# 01 — Technical Requirements Document (TRD)

**Product:** multi-tenant accounting + HR ERP (Zoho Books-class)
**Version:** v1 (first GA)
**Status:** binding for implementation
**Audience:** senior backend/frontend/mobile engineers implementing this system

> This document defines *what* must be true. `02-architecture.md` defines *how* the
> system is shaped, `03-data-model.md` defines the persistent state, and
> `04-state-machines.md` defines legal lifecycle transitions. Where this document and
> `CONVENTIONS.md` disagree, `CONVENTIONS.md` wins — it is the structural contract.

---

## 1. Purpose and scope

### 1.1 Purpose

Build a single-database, multi-tenant SaaS that gives a small-to-mid-sized company
(5–500 employees) a complete, audit-defensible set of books plus the HR and payroll
machinery that feeds those books. The general ledger is the product; every other
module is a subsidiary record whose only job is to produce correct journal entries
through `apps.accounting.services.posting.post_entry()`.

### 1.2 Scope

In scope for v1:

| Area | Included |
|---|---|
| Accounting | Chart of accounts, journals, journal entries, fiscal year/period control, multi-currency with stored FX, trial balance, P&L, balance sheet, cash flow (indirect), general ledger, aged AR/AP |
| Sales | Customers, quotes, sales orders, invoices, recurring invoices, credit notes, tax handling, delivery of PDF invoices |
| Payments | Customer receipts, vendor payments, gateway integration, allocation across invoices, refunds, disputes |
| Purchasing | Vendors, purchase orders, vendor bills, bill payment |
| Expenses | Employee expense claims with receipt capture, approval chain, reimbursement, mileage |
| Inventory | Items, warehouses, stock movements, weighted-average costing, COGS posting, stock valuation |
| Banking | Bank/cash accounts, statement import (CSV/OFX/MT940), rule-based auto-matching, manual reconciliation |
| Projects | Projects, tasks, project members, budgets, timesheets, billable time → invoice |
| HR | Departments, positions, employees, contracts, leave types/balances/requests, attendance, documents |
| Payroll | Salary structures, pay components, payroll runs, statutory deductions, payslips, GL posting, bank transfer file |
| Platform | Tenants, subscriptions, IAM (RBAC + ABAC), audit log, notifications, file storage, exports |

### 1.3 Deployment assumptions

Single PostgreSQL 16 primary + at least one streaming read replica; Django 5 / DRF API
tier behind a WAF and L7 load balancer; Celery + Redis worker tier; S3-compatible
object storage; Next.js 14 web client; Expo (React Native) mobile client for iOS and
Android. Primary launch markets: Egypt, Saudi Arabia, UAE — hence Arabic/RTL is a
first-class requirement, not a later localisation project.

---

## 2. Product goals and non-goals

### 2.1 Goals

| # | Goal | How we know it is met |
|---|---|---|
| G1 | The books are always provably correct | The nightly `assert_ledger_balanced()` job passes for every tenant, every night, with zero exceptions, and any failure pages an on-call engineer |
| G2 | Tenant isolation is enforced by the database, not by developer discipline | A raw SQL query issued without `app.current_tenant` set returns zero business rows; there is an automated test proving it for every RLS-protected table |
| G3 | Money is never wrong by rounding | No `float` anywhere in the money path; allocation uses largest-remainder so `sum(parts) == total` exactly; a property-based test asserts this over random inputs |
| G4 | Every financial mutation is attributable | Every posted entry has `posted_by`, `posted_at`, and a `source_document_type`/`source_document_id`; every privileged action lands in `TenantAuditLog` |
| G5 | Closing a month is a real lock, not a convention | Posting into a `CLOSED` period is impossible through every path: API, Celery task, management command, psql |
| G6 | Arabic-speaking users get a first-class product | Full RTL layout, Arabic numerals option, Arabic invoice/payslip PDFs, Hijri date display option |
| G7 | An accountant can migrate in one day | CSV import for COA, customers, vendors, items, opening balances, employees; opening balances post as a single balanced `OPENING` journal entry |

### 2.2 Non-goals for v1

* We are not building a general-purpose workflow engine. Approval chains are fixed
  shapes (see `04-state-machines.md`), configurable in depth but not in topology.
* We are not building a report designer. Reports are code, parameterised.
* We are not competing on manufacturing (BOM, work orders, routing).
* We are not a payment processor. We integrate gateways; we never touch a PAN.
* We are not building an in-app tax filing submission for v1 (see §6.5 for the
  e-invoicing readiness posture we do commit to).

---

## 3. Personas

| Persona | System role | What they need | What must be impossible for them |
|---|---|---|---|
| **Business Owner** | `owner` (rank 10) | Dashboard: cash position, AR ageing, this month's P&L, payroll cost. Approve large payments and payroll. Add/remove users. | Nothing is impossible — but every privileged action is MFA-gated and audit-logged. Cannot silently edit a posted entry (nobody can). |
| **Accountant** | `accountant` (rank 20) | Full ledger access: manual journals, period close, reconciliation, tax reports, corrections via reversal. | Cannot grant themselves a higher-ranked role (`Role.rank` guard). Cannot see employee salary detail unless separately granted `payroll.payslip.read`. |
| **HR Manager** | `hr_manager` (rank 30) | Employee master data, contracts, leave approval, payroll run preparation. | Cannot post to the ledger directly. Cannot approve their own payroll run if `payroll.run.approve` is separated from `payroll.run.calculate` (segregation of duties, see FR-PRL-08). |
| **Department Manager** | `dept_manager` (rank 50) + ABAC `department_subtree` | Approve leave, timesheets and expenses for their subtree. See their department's cost lines. | Cannot see any row outside their department subtree — enforced by `ScopeRule.strategy = department_subtree`, compiled to a `Q` object, not by hiding UI. |
| **Employee** | `employee` (rank 90) + ABAC `own_record` | Submit expenses, timesheets, leave; view own payslips and leave balance. | Cannot see any other employee's payslip, salary, or contract. Cannot see the ledger at all. |
| **External Auditor** | `auditor` (rank 40), read-only, `RoleAssignment.valid_until` set | Read every posted entry, every attachment, every audit log row; export. | Cannot write anything, anywhere. Access lapses automatically at `valid_until` with no revocation ticket needed. |

**Design rule this implies:** the permission catalogue (`iam.Permission`) is global and
seeded from `config/permissions.json`; the *bundling* into roles is per tenant. A
tenant that wants "HR Manager who may also post payroll" clones the system role — it
never edits the shipped one (`Role.is_system` + `ck_role_system_has_no_tenant`).

---

## 4. Functional requirements

Format: `FR-<MODULE>-<NN>`. Each requirement is testable. "AC" = acceptance criteria;
each AC should map to at least one automated test.

### 4.1 Invoicing & Sales — `FR-INV`

| ID | Requirement |
|---|---|
| **FR-INV-01** | The system shall issue customer invoices with a **gapless** per-tenant, per-year number allocated from `accounting.DocumentSequence`, not from a PostgreSQL `SEQUENCE`. |

**AC-INV-01**
1. Two invoices created concurrently in separate transactions receive consecutive numbers (`INV-2026-000041`, `INV-2026-000042`) — never the same number, never a gap.
2. A transaction that rolls back after allocating a number leaves the counter unchanged (the counter row is locked `FOR UPDATE` inside the same transaction).
3. A draft invoice has `number = ""`; the number is allocated at the `DRAFT → SENT` transition, so an abandoned draft cannot burn a number.
4. `SELECT number FROM sales_invoice WHERE tenant_id = :t AND number <> '' ORDER BY number` has no gaps for any tenant, verified by a nightly job.

| ID | Requirement |
|---|---|
| **FR-INV-02** | Invoice totals shall be computed as: line subtotal → line discount → line tax → document-level discount → rounding adjustment, with all intermediate arithmetic at `numeric(19,6)` and presentation rounding applied exactly once at posting. |

**AC-INV-02**
1. For an invoice of 3 lines at 33.333333 EGP with 14% VAT, `sum(line_total) == invoice.subtotal` exactly, and `subtotal + tax_total - discount_total == total_amount` exactly.
2. Passing a Python `float` into any invoice service raises `ValidationError` (from `to_money`), it does not silently coerce.
3. Document-level discount is distributed across lines using `apps.core.fields.allocate()`; `sum(allocated) == discount_amount` to the minor unit, always.
4. A JPY invoice (0 minor units) and a KWD invoice (3 minor units) both round correctly — `quantize_currency` is driven by `CURRENCY_MINOR_UNITS`, not a hardcoded 2.

| ID | Requirement |
|---|---|
| **FR-INV-03** | Sending an invoice shall post a journal entry: Dr *AR control* / Cr *Revenue* (per line's revenue account) / Cr *Output VAT*, in one atomic transaction with the status change. |

**AC-INV-03**
1. If `post_entry()` raises, the invoice remains `DRAFT` and no number is consumed — verified by forcing `PeriodClosed`.
2. The created `JournalEntry` carries `source = 'invoice'`, `source_document_type = 'sales.Invoice'`, `source_document_id = invoice.id`.
3. `idempotency_key = f"invoice:issue:{invoice.id}"`; issuing twice returns the same entry and does not double the AR balance.
4. Invoicing in a foreign currency stores `exchange_rate` on the entry and `base_debit`/`base_credit` on every line, taken from `accounting.ExchangeRate` for `entry_date`; a missing rate is a hard error, never an implicit 1.0.

| ID | Requirement |
|---|---|
| **FR-INV-04** | Invoice lifecycle shall be exactly `DRAFT → SENT → PARTIALLY_PAID → PAID`, with `VOIDED` and `WRITTEN_OFF` as terminal branches and `OVERDUE` as a derived flag, never a stored status. |

**AC-INV-04**
1. `OVERDUE` is computed as `status IN (SENT, PARTIALLY_PAID) AND due_date < today AND balance_due > 0`. There is no `overdue` value in the `Status` TextChoices — a stored overdue status goes stale the moment a payment lands.
2. Any transition not in `ALLOWED_TRANSITIONS` raises; direct `.status = ` assignment from a view is prohibited by convention and caught in code review + a lint rule.
3. Voiding a `SENT` invoice in an open period reverses its journal entry via `void_entry()`; voiding one in a closed period is refused with a message directing the user to a credit note.

| ID | Requirement |
|---|---|
| **FR-INV-05** | Credit notes shall be first-class documents with their own sequence, optionally linked to an originating invoice, posting the mirror entry (Dr Revenue / Dr Output VAT / Cr AR). |

**AC-INV-05**
1. A credit note cannot exceed the originating invoice's `total_amount` minus credit notes already applied.
2. Applying a credit note to an invoice reduces `balance_due` and can move the invoice to `PAID` without a payment row.
3. An unapplied credit note appears as a customer credit balance on the AR ageing report.

| ID | Requirement |
|---|---|
| **FR-INV-06** | Recurring invoice templates shall generate invoices on a schedule via a Celery beat task, idempotently per (template, occurrence date). |

**AC-INV-06**
1. Running the generator twice for the same occurrence date creates exactly one invoice (`uq_recurring_occurrence`).
2. A generation failure for one tenant does not block other tenants — the beat task fans out one task per tenant.
3. Generated invoices are created in the state configured on the template (`DRAFT` for review, or `SENT` for auto-send).

| ID | Requirement |
|---|---|
| **FR-INV-07** | Every invoice shall render to PDF in the tenant's chosen template and locale, including a full RTL Arabic layout with an Arabic-shaped font. |

**AC-INV-07**
1. An `ar` locale invoice renders right-aligned, with mirrored table column order and correctly shaped/joined Arabic glyphs (not disconnected letterforms).
2. Numbers render in the tenant's configured digit style (Western `123` or Eastern-Arabic `١٢٣`) while remaining `numeric` in the database.
3. The PDF is stored in object storage under `tenant/<tenant_id>/invoices/<invoice_id>/<sha256>.pdf` and served only through a signed URL with a ≤15 minute TTL.
4. A regenerated PDF for a `SENT` invoice must be byte-identical unless the invoice changed — the stored hash proves what the customer received.

| ID | Requirement |
|---|---|
| **FR-INV-08** | Quotes and sales orders shall convert to invoices without re-keying, carrying line references forward for traceability. |

**AC-INV-08**: converting a quote sets `quote.status = ACCEPTED`, creates a `DRAFT` invoice whose lines carry `source_quote_line_id`, and a second conversion attempt is refused.

---

### 4.2 Payments — `FR-PAY`

| ID | Requirement |
|---|---|
| **FR-PAY-01** | A payment shall be allocatable across many invoices, and an invoice receivable of many payments; the allocation is an explicit `PaymentAllocation` row, never inferred. |

**AC-PAY-01**
1. `sum(allocation.amount) <= payment.amount` is enforced by a check at service level and a nightly integrity query.
2. `sum(allocation.amount for an invoice) <= invoice.total_amount` — over-application is refused with the exact overage in the error message.
3. Unallocated payment remainder appears as customer credit and can be applied later or refunded.
4. Deleting an allocation is impossible; it is reversed, which reverses its GL effect.

| ID | Requirement |
|---|---|
| **FR-PAY-02** | Payment lifecycle shall be `PENDING → AUTHORIZED → CAPTURED → SETTLED` with `FAILED`, `REFUNDED`, `PARTIALLY_REFUNDED` and `DISPUTED` branches, and the GL shall reflect the *economic* event, not the UI event. |

**AC-PAY-02**
1. `AUTHORIZED` posts nothing — an authorisation is not cash.
2. `CAPTURED` posts Dr *Gateway clearing* / Cr *AR*.
3. `SETTLED` posts Dr *Bank* / Dr *Bank fees* / Cr *Gateway clearing*, with the fee taken from the settlement payload, not estimated.
4. The gateway clearing account nets to zero for every fully settled batch; a nightly job reports any clearing balance older than 5 days.

| ID | Requirement |
|---|---|
| **FR-PAY-03** | All gateway webhooks shall be **stored first, processed second**, and processing shall be idempotent on the gateway's event id. |

**AC-PAY-03**
1. The webhook endpoint validates the signature, writes a `WebhookEvent` row, returns `202` in under 200 ms p95, and enqueues processing on the `payments` queue.
2. Redelivery of the same `provider_event_id` is a no-op that returns `202` — `uq_webhook_provider_event`.
3. Events arriving out of order (settlement before capture) are parked and retried with exponential backoff up to 24 h, then alerted; they are never dropped.
4. A signature failure is logged with the raw body retained for 30 days and returns `400` without enqueuing.

| ID | Requirement |
|---|---|
| **FR-PAY-04** | The system shall never store a full PAN, CVV, or bank credential. Only a gateway token, last-4, brand and expiry. |

**AC-PAY-04**: a repository-wide test scans models for fields named like card data; the payment payload JSONB is filtered through an allowlist before persistence.

| ID | Requirement |
|---|---|
| **FR-PAY-05** | Vendor payments shall support batch execution and produce a bank transfer file in the tenant's country format. |

**AC-PAY-05**: a batch of 200 vendor payments produces one file and 200 `PaymentAllocation` rows against bills, posted as a single journal entry with one Cr *Bank* line and 200 Dr *AP control* lines.

| ID | Requirement |
|---|---|
| **FR-PAY-06** | Refunds shall post as reversals of the original capture path, never as negative payments. |

**AC-PAY-06**: a partial refund moves the payment to `PARTIALLY_REFUNDED`, restores the invoice's `balance_due` by the refunded amount, and posts Dr *AR* / Cr *Bank*.

---

### 4.3 Expenses & Cash Flow — `FR-EXP`

| ID | Requirement |
|---|---|
| **FR-EXP-01** | Employees shall submit expense claims with at least one attached receipt image, captured from the mobile app camera or uploaded on web. |

**AC-EXP-01**
1. Receipt upload goes directly to object storage via a pre-signed POST; the API never proxies the bytes.
2. Accepted types: `image/jpeg`, `image/png`, `image/heic`, `application/pdf`; max 15 MB; content type verified server-side by magic bytes, not by the client's header.
3. A claim without a receipt can be submitted only if the tenant setting `expenses.require_receipt_over` threshold is not exceeded.

| ID | Requirement |
|---|---|
| **FR-EXP-02** | Expense approval shall be `DRAFT → SUBMITTED → APPROVED\|REJECTED → REIMBURSED`, with approver selection driven by the submitter's department subtree and the approver's ABAC scope + `max_amount` parameter. |

**AC-EXP-02**
1. A manager with `ScopeRule.parameters = {"max_amount": "5000.00"}` cannot approve a 5000.01 claim; it escalates to the next rank up.
2. An employee cannot approve their own claim even if they hold `expenses.claim.approve` — self-approval is blocked unconditionally.
3. `APPROVED` posts Dr *Expense account* / Dr *Input VAT (if recoverable)* / Cr *Employee payable*.
4. `REIMBURSED` posts Dr *Employee payable* / Cr *Bank*, or, when reimbursed through payroll, links the claim to a `PayrollRun` and posts through the payroll entry instead — never both.

| ID | Requirement |
|---|---|
| **FR-EXP-03** | Non-recoverable input VAT shall be expensed to the same account as the expense line, not to the input VAT account. |

**AC-EXP-03**: for a `TaxRate` with `is_recoverable = False`, the posted entry has no line against the input VAT account, and the expense debit equals the gross amount.

| ID | Requirement |
|---|---|
| **FR-EXP-04** | The system shall produce a cash-flow statement using the indirect method, derived from the ledger only. |

**AC-EXP-04**
1. Operating/investing/financing classification comes from an `Account.cash_flow_category` attribute on the chart of accounts, not from hardcoded account codes.
2. Net change in cash from the statement equals the movement in all `is_reconcilable` accounts for the period, to the minor unit.

| ID | Requirement |
|---|---|
| **FR-EXP-05** | A 13-week rolling cash forecast shall combine: bank balances, AR by due date, AP by due date, recurring invoices, and scheduled payroll cost. |

**AC-EXP-05**: the forecast is recomputed nightly per tenant on the `reports` queue and is available in under 500 ms p95 from cache.

---

### 4.4 Inventory — `FR-STK`

| ID | Requirement |
|---|---|
| **FR-STK-01** | Stock shall be valued using **weighted average cost**, recomputed on every receipt, per (item, warehouse). |

**AC-STK-01**
1. Receiving 10 @ 100 then 10 @ 120 yields an average cost of 110.000000 exactly, stored at `numeric(19,6)`.
2. Issuing stock uses the average cost *at the moment of issue*; a later receipt never retroactively changes a posted COGS figure.
3. Negative stock is refused by default; a tenant setting may permit it, in which case the average cost is held and a variance is posted on the next receipt.

| ID | Requirement |
|---|---|
| **FR-STK-02** | Every stock movement with a financial effect shall post to the GL: receipts Dr *Inventory asset* / Cr *GRNI or AP*; issues Dr *COGS* / Cr *Inventory asset*; adjustments Dr/Cr *Inventory adjustment*. |

**AC-STK-02**
1. `sum(inventory asset account balance)` equals `sum(quantity_on_hand * average_cost)` across all items and warehouses, to the minor unit, checked nightly. A mismatch is a P2 alert.
2. Non-stock ("service") items never touch inventory accounts.

| ID | Requirement |
|---|---|
| **FR-STK-03** | Stock movements shall be an append-only ledger; corrections are new opposite movements. |

**AC-STK-03**: `StockMovement` subclasses `ImmutableFinancialModel`; `delete()` raises; there is no update path for `quantity` or `unit_cost`.

| ID | Requirement |
|---|---|
| **FR-STK-04** | The system shall support multiple warehouses and inter-warehouse transfers with in-transit accounting. |

**AC-STK-04**: a transfer creates two movements (out + in) and, when `in_transit` is enabled, holds value in an in-transit account until receipt is confirmed.

| ID | Requirement |
|---|---|
| **FR-STK-05** | Item SKUs shall be unique **per tenant**, never globally. |

**AC-STK-05**: `UniqueConstraint(fields=["tenant", "sku"], name="uq_item_sku")`. A global unique index would let tenant A discover whether tenant B uses a SKU — an information leak, per `CONVENTIONS.md` §3.

---

### 4.5 Bank Reconciliation — `FR-BNK`

| ID | Requirement |
|---|---|
| **FR-BNK-01** | The system shall import bank statements from CSV (with a saved per-bank column mapping), OFX and MT940. |

**AC-BNK-01**
1. Re-importing the same statement file is a no-op: statement lines are deduplicated on `(bank_account, value_date, amount, bank_reference)` hashed into `dedupe_hash` with `uq_stmt_line_dedupe`.
2. A malformed row aborts the whole import with a line-numbered error; partial imports are never committed.

| ID | Requirement |
|---|---|
| **FR-BNK-02** | Auto-matching shall propose matches by rules (exact amount + date window, reference substring, payee alias) and shall never auto-post a match below the tenant's confidence threshold. |

**AC-BNK-02**
1. A proposed match is a `ReconciliationMatch` in `PROPOSED`; only `CONFIRMED` sets `JournalLine.reconciled_at`.
2. Confirming a match is reversible until the reconciliation is finalised.
3. Matching is many-to-many: one statement line may settle three invoices; three statement lines may settle one.

| ID | Requirement |
|---|---|
| **FR-BNK-03** | Finalising a reconciliation shall lock the matched lines and record the closing balance; the reconciled balance must equal the statement's closing balance exactly. |

**AC-BNK-03**: finalisation is refused when `book_balance + unreconciled_items != statement_closing_balance`, with the difference shown to the minor unit.

| ID | Requirement |
|---|---|
| **FR-BNK-04** | Bank charges and interest discovered during reconciliation shall be postable inline as journal entries against *Bank fees* / *Interest income*. |

---

### 4.6 Projects & Time Tracking — `FR-PRJ`

| ID | Requirement |
|---|---|
| **FR-PRJ-01** | Time shall be recorded against a (project, task, employee, date) with a billable flag, a billing rate, and a cost rate. |

**AC-PRJ-01**
1. Billing rate resolution order: task override → project member rate → project default → employee default. The resolved rate is **stored on the entry**, so a later rate change never rewrites history.
2. `hours` uses `QuantityField` (`numeric(19,6)`), not a float; 7.5 h is exact.
3. Overlapping entries for the same employee/date are warned about but not blocked (a consultant may bill two clients in the same hour under some contracts); total > 24 h/day is blocked.

| ID | Requirement |
|---|---|
| **FR-PRJ-02** | Timesheet lifecycle shall be `DRAFT → SUBMITTED → APPROVED → INVOICED`, and only `APPROVED` billable entries may be pulled into an invoice. |

**AC-PRJ-02**
1. An entry that reaches `INVOICED` stores `invoice_line_id` and becomes immutable.
2. Voiding the invoice returns its entries to `APPROVED`, making them re-billable — they are never orphaned in `INVOICED`.
3. Bulk-approving a week is one transaction; a single invalid entry fails the whole batch with the offending entry identified.

| ID | Requirement |
|---|---|
| **FR-PRJ-03** | Projects shall carry a budget (hours and/or amount) and report actual vs budget from `JournalLine.project` plus timesheet cost. |

**AC-PRJ-03**: crossing 80% and 100% of budget raises a notification to the project manager exactly once per threshold per project.

| ID | Requirement |
|---|---|
| **FR-PRJ-04** | Every journal line shall be optionally taggable with `project` and `department`, and both dimensions shall be filterable in every report. |

**AC-PRJ-04**: this is already structural — `accounting.JournalLine.project` and `.department` exist with `ix_line_project` and `ix_line_department`. Reports must use them rather than re-deriving from source documents.

---

### 4.7 Financial Reporting — `FR-RPT`

| ID | Requirement |
|---|---|
| **FR-RPT-01** | Every financial report shall be computed from `JournalLine` aggregates over `POSTED` entries only, never from `Account.cached_balance`. |

**AC-RPT-01**
1. `cached_balance` is used only for dashboard tiles, always labelled with `cached_balance_as_of`.
2. A test that corrupts `cached_balance` leaves the trial balance, P&L and balance sheet unchanged.
3. Draft, voided and reversed entries never appear in a report; the reversing mirror entry does (it is itself `POSTED`).

| ID | Requirement |
|---|---|
| **FR-RPT-02** | Reports shall be reproducible: the same parameters at any later time produce the same figures, unless a correcting entry was posted in between. |

**AC-RPT-02**: a report run stores its parameter set and a hash of the result; re-running a period-closed report yields an identical hash.

| ID | Requirement |
|---|---|
| **FR-RPT-03** | The v1 report set is: Trial Balance, General Ledger, Journal Report, Balance Sheet, Profit & Loss, Cash Flow (indirect), AR Ageing, AP Ageing, Customer/Vendor Statement, VAT Return summary, Inventory Valuation, Project Profitability, Payroll Register, Payroll Cost by Department, Leave Balance Report. |

**AC-RPT-03**: each report has comparative-period support, a drill-down to journal lines, and CSV + XLSX + PDF export.

| ID | Requirement |
|---|---|
| **FR-RPT-04** | Large exports shall be asynchronous: the request returns a job id, the worker writes to object storage, the user receives a signed download link. |

**AC-RPT-04**: any export projected to exceed 5 000 rows or 5 s is forced async; the synchronous path returns `202` with a poll URL.

| ID | Requirement |
|---|---|
| **FR-RPT-05** | Reporting queries shall be routed to a read replica. |

**AC-RPT-05**: a Django database router sends the `reporting` app's querysets to the `replica` alias; replica lag over 30 s degrades gracefully with a "figures as of HH:MM" banner rather than serving silently stale data.

---

### 4.8 HR — `FR-HR`

| ID | Requirement |
|---|---|
| **FR-HR-01** | Employees shall have a per-tenant unique `employee_code`, a department, a position, and an employment status timeline. |

**AC-HR-01**
1. `UniqueConstraint(fields=["tenant", "employee_code"], name="uq_employee_code")`.
2. An employee may exist without a login (`iam.TenantMembership.employee` is nullable and `OneToOne`) — factory staff clocked in by a supervisor are employees but not users.
3. A user may exist without an employee record (external auditor). The link is one-way and optional by design.

| ID | Requirement |
|---|---|
| **FR-HR-02** | Departments shall form a tree, and ABAC `department_subtree` shall resolve the whole subtree in one query. |

**AC-HR-02**
1. Cycles are impossible: a check constraint blocks self-parenting, and the service validates the path on reparent.
2. Subtree resolution uses a recursive CTE or a materialised `path` column; it must not be an N+1 walk.
3. Reparenting a department does **not** rewrite historical `JournalLine.department` values — cost history stays as it was reported.

| ID | Requirement |
|---|---|
| **FR-HR-03** | Leave shall be modelled as accrual policies, per-employee balances, and requests, with balances computed from an append-only `LeaveTransaction` ledger. |

**AC-HR-03**
1. Balance = `sum(accruals) - sum(taken) - sum(pending holds)`; a pending request holds balance so an employee cannot double-book.
2. Rejecting or cancelling a request releases the hold in the same transaction.
3. Carry-over caps and expiry are applied by a scheduled job that writes explicit `expiry` transactions — the balance is never silently adjusted.
4. Half-days and hour-granularity leave are supported via `QuantityField` days.

| ID | Requirement |
|---|---|
| **FR-HR-04** | Leave approval shall be `DRAFT → SUBMITTED → PENDING_MANAGER → PENDING_HR → APPROVED\|REJECTED` with `CANCELLED`, where the HR stage is skippable per leave type. |

**AC-HR-04**
1. An employee's manager is resolved from `Employee.manager`, falling back to the department head; if neither resolves, the request routes to HR with a warning.
2. An approver cannot approve their own request at any stage.
3. Approving a leave request that overlaps an existing approved request is refused.

| ID | Requirement |
|---|---|
| **FR-HR-05** | Attendance shall support shift definitions, clock-in/out with optional geofence, and derive overtime hours feeding payroll. |

**AC-HR-05**: overtime hours computed by the attendance engine are the *only* source of the overtime component in payroll; there is no free-text overtime entry on the payroll run.

| ID | Requirement |
|---|---|
| **FR-HR-06** | Employee documents (contract, ID, certificates) shall be stored encrypted at rest with expiry tracking and reminders. |

**AC-HR-06**: documents are only reachable through signed URLs; an employee sees their own documents, HR sees their department scope, nobody else sees any.

---

### 4.9 Payroll — `FR-PRL`

| ID | Requirement |
|---|---|
| **FR-PRL-01** | Payroll shall be defined by composable `PayComponent`s (earning / deduction / employer contribution), each with a calculation method: fixed, percentage-of-base, formula from a closed vocabulary, or attendance-derived. |

**AC-PRL-01**
1. Formulas are not `eval()`. They are a restricted expression AST over a whitelisted variable set — an injection bug in payroll is a payroll-wide data breach and a fraud vector.
2. Component evaluation order is explicit (`sequence`), and a component may only reference components with a lower sequence. A cycle is a startup/validation error, not a runtime infinite loop.

| ID | Requirement |
|---|---|
| **FR-PRL-02** | A payroll run shall be `DRAFT → CALCULATING → CALCULATED → PENDING_APPROVAL → APPROVED → POSTED → PAID`, with `CANCELLED` reachable only before `POSTED`. |

**AC-PRL-02**
1. `CALCULATING` is a real state, not a spinner: the run is locked, and a crashed worker leaves a visibly stuck run that a retry can resume rather than a silently half-calculated one.
2. Recalculation is only legal from `CALCULATED` or `PENDING_APPROVAL`; it discards and rebuilds all payslips atomically.
3. Once `POSTED`, the run is immutable — corrections are an off-cycle run or a reversing journal entry.

| ID | Requirement |
|---|---|
| **FR-PRL-03** | Payroll calculation shall be deterministic and reproducible: the same run inputs produce identical payslips. |

**AC-PRL-03**: the run snapshots the salary structure, component definitions and tax bands it used (`PayrollRun.calculation_snapshot` JSONB), so a policy change next month does not alter last month's numbers.

| ID | Requirement |
|---|---|
| **FR-PRL-04** | The payroll GL posting shall be a single balanced journal entry per run: Dr *Salaries expense* (by department) / Dr *Employer contributions expense* / Cr *Salaries payable* / Cr *Income tax payable* / Cr *Social insurance payable*. |

**AC-PRL-04**
1. Departmental salary expense lines carry `JournalLine.department`, so payroll cost by department is a ledger query, not a payroll-module query.
2. The entry balances by construction: `sum(gross) + sum(employer_contributions) == sum(net) + sum(deductions) + sum(employer_contributions)`. Any mismatch fails in `post_entry()` before anything persists.
3. `idempotency_key = f"payroll:post:{run.id}"`.

| ID | Requirement |
|---|---|
| **FR-PRL-05** | Paying a payroll run shall post Dr *Salaries payable* / Cr *Bank* and produce a bank transfer file. |

**AC-PRL-05**: after `PAID`, the salaries-payable account balance attributable to that run is zero.

| ID | Requirement |
|---|---|
| **FR-PRL-06** | Payslips shall be individually retrievable by the employee, in English or Arabic, as a PDF. |

**AC-PRL-06**: an employee with only `own_record` scope receives `404` (not `403`) for another employee's payslip id — a `403` confirms the id exists.

| ID | Requirement |
|---|---|
| **FR-PRL-07** | Statutory deduction rules (income tax bands, social insurance ceilings) shall be data, versioned by effective date, per country. |

**AC-PRL-07**: changing the 2027 tax band does not alter any 2026 payslip; the run used the band effective at its `period_end`.

| ID | Requirement |
|---|---|
| **FR-PRL-08** | Payroll shall enforce segregation of duties: the user who calculates a run may not be the user who approves it, unless the tenant explicitly disables the control and that fact is audit-logged. |

**AC-PRL-08**: `PAYROLL_APPROVED` is written to `TenantAuditLog` with both actor ids; the approval endpoint requires MFA re-authentication.

| ID | Requirement |
|---|---|
| **FR-PRL-09** | Salary figures shall be visible only to holders of `payroll.payslip.read` at the appropriate ABAC scope, and every read of another person's salary shall be logged. |

**AC-PRL-09**: a `payslip.read` on a row where `employee_id != actor.employee_id` writes an audit row. Yes, this is chatty; salary snooping is the single most common HR-system abuse and the log is what makes it detectable.

---

## 5. Non-functional requirements

### 5.1 Performance budgets

Measured at the load balancer, per environment, at the stated concurrency. These are
**budgets**, not aspirations: a PR that regresses a budget by >10% fails CI.

| Operation | p50 | p95 | p99 | Notes |
|---|---|---|---|---|
| Authenticated `GET` list (50 rows, indexed filter) | 60 ms | 200 ms | 400 ms | e.g. invoice list |
| Authenticated `GET` detail | 40 ms | 150 ms | 300 ms | |
| Invoice create + post (`POST`) | 120 ms | 400 ms | 800 ms | includes `post_entry` in one transaction |
| Payment webhook ingest (store only) | 25 ms | 200 ms | 400 ms | must not include processing |
| Trial balance, 12 months, 500 k lines | 400 ms | 1.5 s | 3 s | read replica, from `JournalLine` aggregate |
| P&L / Balance sheet, 12 months | 500 ms | 2 s | 4 s | |
| Dashboard first paint (web, warm cache) | — | 1.2 s | — | LCP, 4G profile |
| Mobile cold start to usable list | — | 2.5 s | — | mid-tier Android |
| Payroll run, 500 employees | — | 90 s | 180 s | async, `payroll` queue |
| Bulk statement import, 5 000 lines | — | 60 s | 120 s | async |
| CSV export, 100 k rows | — | 120 s | 240 s | async, streamed to S3 |

Throughput target at GA: 300 sustained req/s across the API tier with headroom to 3×
by horizontal scaling; 10 000 tenants; largest single tenant 100 M `journal_line` rows
before partitioning is required (see `02-architecture.md` §9).

Database rules that back these budgets:
* No endpoint may issue an unbounded query. Every list endpoint is cursor-paginated with a hard `limit ≤ 200`.
* No N+1: DRF serialisers must declare `select_related`/`prefetch_related`; a test harness asserts a query-count ceiling per endpoint.
* Any query without an index-supported `tenant_id` predicate is a bug. `pg_stat_statements` is reviewed weekly; any statement with mean time > 100 ms gets a ticket.

### 5.2 Availability, RPO, RTO

| Metric | Target | Mechanism |
|---|---|---|
| API availability | 99.9% monthly (43 min budget) | ≥3 API pods across ≥2 AZs, rolling deploys, readiness probes |
| Database availability | 99.95% | Managed PG 16 with synchronous standby in a second AZ, automatic failover |
| **RPO** | **≤ 5 minutes** | WAL archiving to object storage every 60 s + synchronous standby |
| **RTO** | **≤ 60 minutes** for full region loss; ≤ 5 minutes for AZ loss | Documented and *rehearsed quarterly* restore; a restore procedure that has never been executed is not a restore procedure |
| Backup retention | 35 days PITR, 12 monthly full backups, 7 yearly | Financial records retention, §6.2 |
| Worker availability | Best-effort; no data loss | Celery `acks_late=True` + idempotent tasks means a killed worker re-runs the task safely |

Degradation policy: if the primary database is unavailable, the API serves read-only
from the replica with an explicit banner rather than returning 500s. A tenant must
always be able to *read* their own books.

### 5.3 Security

| # | Requirement |
|---|---|
| SEC-01 | TLS 1.2+ only, HSTS with preload, modern cipher suites. Plain HTTP is redirected at the edge, never served. |
| SEC-02 | JWT access tokens live 15 minutes. Refresh tokens live 30 days, **rotate on every use**, and are stored hashed. Reuse of a rotated refresh token invalidates the entire token family and raises a security event — this is the standard detection for a stolen refresh token. |
| SEC-03 | MFA (TOTP) is mandatory for `owner`, `accountant`, `hr_manager` and `is_platform_admin`. Sensitive actions (`Permission.is_sensitive`) require re-authentication within the last 10 minutes. |
| SEC-04 | Passwords: Argon2id, minimum 12 characters, checked against a breached-password list. 5 failed attempts locks for 15 minutes (`User.failed_login_count`, `locked_until`). |
| SEC-05 | Every business table has an RLS policy keyed on `current_setting('app.current_tenant')`. The application database role is **not** `BYPASSRLS` and is not the table owner. A test asserts that a fresh connection with no `SET LOCAL` sees zero rows in every RLS table. |
| SEC-06 | Tenant resolution never trusts a client-supplied `X-Tenant-Id` header alone; the tenant comes from the JWT claim, cross-checked against an active `TenantMembership` row read from the database on token refresh. |
| SEC-07 | Object storage is private. Files are served only through signed URLs, TTL ≤ 15 minutes, with the tenant id embedded in the key path so a mis-scoped key is visible in logs. |
| SEC-08 | Secrets live in a secret manager, never in environment files in the repo. Database credentials rotate quarterly. |
| SEC-09 | Rate limits: 5/min per IP on login, 10/min per user on export, 1 000/min per tenant on the API generally, 100/s on webhook ingest per provider. |
| SEC-10 | Platform-admin impersonation requires MFA, is time-boxed to 60 minutes, writes `IMPERSONATION` to `TenantAuditLog` at start and end, and shows a persistent banner in the UI. |
| SEC-11 | Dependency scanning and container image scanning run on every build; a critical CVE blocks deploy. Annual third-party penetration test. |
| SEC-12 | PII columns (national id, bank account, salary) are encrypted at rest at the column level in addition to volume encryption, with keys in the secret manager. |

### 5.4 Auditability

| # | Requirement |
|---|---|
| AUD-01 | Posted financial records are append-only. `ImmutableFinancialModel.delete()` raises, a database trigger rejects `UPDATE` of monetary columns on posted rows, and the application database role lacks `DELETE` on `accounting_journal_entry` and `accounting_journal_line`. Three independent layers, because one is a single point of failure. |
| AUD-02 | Every row records `created_by`/`updated_by`/`created_at`/`updated_at` (`AuditedModel`). |
| AUD-03 | Security- and money-relevant actions write to `tenancy.TenantAuditLog`: login, failed login, role grant/revoke, impersonation, export, period close, entry reversal, payroll approval, setting change. |
| AUD-04 | Document sequences are gapless per (tenant, scope, year). A nightly job asserts this and alerts on any gap. |
| AUD-05 | Every posted entry is traceable to its source document via `source_document_type` + `source_document_id`, indexed by `ix_entry_source_doc`. |
| AUD-06 | The audit log is retained for 7 years and is not deletable by any tenant user. |
| AUD-07 | The nightly `assert_ledger_balanced()` job runs per tenant; a failure is a P1 page, because it means data entered the ledger outside the application. |

### 5.5 Localisation

| # | Requirement |
|---|---|
| L10N-01 | UI languages at v1: English (`en`) and Arabic (`ar`). All user-visible strings come from message catalogues; no string literals in components. |
| L10N-02 | **RTL is a layout mode, not a stylesheet flip.** Next.js sets `dir="rtl"` on `<html>`; all spacing uses CSS logical properties (`margin-inline-start`, not `margin-left`). React Native uses `I18nManager.forceRTL` with a required app restart handled gracefully. |
| L10N-03 | Directionally-meaningful icons (back arrow, indent, undo) mirror in RTL; brand marks and media controls do not. |
| L10N-04 | Numbers, currency and dates format per locale via ICU. Digit style (Western vs Eastern-Arabic numerals) is a per-user preference independent of language. |
| L10N-05 | Mixed LTR/RTL content (an Arabic invoice with an English item name, or an IBAN) must not visually reorder. Use Unicode isolates (`⁨`/`⁩`), not manual `dir` spans. |
| L10N-06 | Documents (invoice, credit note, payslip, statement) render in the *recipient's* locale, not the sender's. |
| L10N-07 | Gregorian dates are canonical in storage (`DateField`); Hijri is a display option. |
| L10N-08 | Timezones: everything stored UTC; displayed in the user's timezone, falling back to the tenant's (`Tenant.timezone`). Fiscal period boundaries are evaluated in the **tenant's** timezone — an invoice posted at 23:30 on 31 March in Cairo belongs to March, not April. |
| L10N-09 | Arabic-shaping-capable fonts are bundled for PDF generation. A tofu box in a payslip is a release blocker. |

### 5.6 Accessibility

| # | Requirement |
|---|---|
| A11Y-01 | Web conforms to WCAG 2.2 AA. |
| A11Y-02 | All interactive elements are keyboard reachable in a logical order; visible focus ring; no keyboard traps. |
| A11Y-03 | Contrast ≥ 4.5:1 for text, ≥ 3:1 for UI components and graphical objects. |
| A11Y-04 | Data tables use real `<table>` semantics with `<th scope>`; financial figures carry an accessible label including the currency. |
| A11Y-05 | Form errors are programmatically associated (`aria-describedby`) and announced; error text says what to do, not just what is wrong. |
| A11Y-06 | Colour is never the sole carrier of meaning — an overdue invoice is red *and* labelled "Overdue". |
| A11Y-07 | Mobile meets platform accessibility guidelines: 44×44 pt targets, VoiceOver/TalkBack labels, Dynamic Type support up to 200%. |
| A11Y-08 | `prefers-reduced-motion` is honoured. |
| A11Y-09 | Automated axe checks run in CI on every page; manual screen-reader testing on the 10 highest-traffic flows each release. |

### 5.7 Observability

| # | Requirement |
|---|---|
| OBS-01 | Every log line is structured JSON and carries `request_id`, `tenant_id`, `user_id`, `route`, `status`, `duration_ms`. A log line without `tenant_id` in a request context is a bug. |
| OBS-02 | Celery tasks propagate `request_id` and `tenant_id` from the enqueuing request into task logs. |
| OBS-03 | Logs never contain: passwords, tokens, MFA secrets, full PANs, national ids, salary amounts. Redaction is a logging filter, not developer discipline. |
| OBS-04 | RED metrics per route, USE metrics per host, queue depth and task latency per Celery queue, DB connection-pool saturation, replica lag. |
| OBS-05 | Alerts that page: ledger integrity failure, RLS policy missing on a table, replica lag > 120 s, payroll queue depth > 50, webhook processing backlog > 15 min, error rate > 1% for 5 min. |

---

## 6. Compliance considerations

### 6.1 Audit trail immutability

The regulatory requirement (and plain professional practice) is that a financial record,
once reported, cannot be changed without a visible trace. Our position:

* No hard delete of any posted financial record, ever. Enforced at three layers (AUD-01).
* Corrections are **void** (same period, still open) or **reverse** (any time, creates a
  dated mirror entry). Both are implemented in `apps.accounting.services.posting`.
* Voided entries keep their document number. A gap in a number sequence is the first
  thing an auditor looks for; a `VOIDED` row with a zero effect is the honest answer.
* Closing a period is a hard lock enforced by a row lock (`SELECT ... FOR SHARE` on
  `FiscalPeriod` during posting, `FOR UPDATE` during close) so the close/post race
  cannot land an entry in a closed period.
* `SOFT_CLOSED` exists so that month-end is a process rather than an instant, without
  the usual workaround of leaving periods open "just in case" — which is how
  prior-period figures silently change.

### 6.2 Data retention

| Data class | Retention | Basis |
|---|---|---|
| Journal entries, lines, invoices, bills, payslips | 7 years after the fiscal year ends | Statutory accounting retention in target markets |
| Tenant audit log | 7 years | Investigations look backwards |
| Attachments (receipts, contracts) | Same as their parent document | Otherwise the record is incomplete |
| Application logs | 90 days hot, 13 months cold | Incident forensics |
| Webhook raw payloads | 30 days | Dispute resolution |
| Terminated employee HR records | 7 years after termination, then anonymised | Employment-law retention beats immediate erasure |
| Closed tenant data | 90-day grace (restorable), then export delivered, then destroyed at 12 months unless statutory hold applies | |

Retention is executed by a scheduled job that writes what it did to the audit log.
Deletion jobs never run without a dry-run report reviewed by a human for the first
12 months of operation.

### 6.3 GDPR-style data subject rights

We treat GDPR as the baseline even where the target market's law is lighter, because
building for the strictest regime once is cheaper than retrofitting.

| Right | Implementation | Limit |
|---|---|---|
| Access | Self-service export of a data subject's own records (profile, payslips, expenses, leave, timesheets) as a machine-readable bundle, within 30 days | |
| Rectification | Editable profile fields; corrections to *posted* financial records go through reversal, not edit | A payslip is not rectified in place |
| Erasure | Pseudonymisation, not deletion: name/email/phone/national id replaced with a tombstone; the financial rows and their amounts survive with an opaque subject key | Financial records are exempt from erasure — a legal obligation to retain overrides the erasure right, and we say so explicitly in the response |
| Restriction | Membership deactivation halts processing while retaining the record | |
| Portability | JSON + CSV export of subject-provided data | |
| Objection to automated decision-making | There is none in v1 — no automated hiring, scoring or credit decisions | |

Supporting requirements: a data-processing register per tenant; sub-processor list
published; DPA available; breach notification path with a 72-hour clock and a named
owner; data residency selectable per tenant at signup (EU / GCC / other), implemented
as separate deployments — **not** as a column, because residency is a physical property.

### 6.4 Tenant isolation as a compliance control

The single most dangerous bug class in this architecture is cross-tenant data
exposure. It is a compliance failure, not just a defect. Controls:

1. `tenant_id` on every business row (`TenantScopedModel`).
2. ORM manager that fails **closed** — no tenant context returns `.none()`, not everything.
3. PostgreSQL RLS as the backstop for `.raw()`, Celery tasks and psql.
4. Cache keys namespaced by tenant, always (see `02-architecture.md` §6).
5. Object storage keys prefixed by tenant.
6. Per-tenant uniqueness on every natural key; a global unique index leaks existence.
7. An automated test suite that, for each RLS table, asserts zero rows visible without a bound tenant.

### 6.5 E-invoicing readiness

We do not submit filings in v1, but v1 must not make submission expensive later.
Structural commitments:

* Every invoice carries the fields the Egyptian ETA and ZATCA (Saudi) schemas require:
  seller and buyer tax registration numbers, per-line tax subtotals with the tax code,
  item classification code, unit-of-measure code, issue timestamp with timezone offset,
  and a stable document UUID (we already use UUID PKs).
* Invoice totals are stored, not derived on render, so a submitted document and a
  re-rendered document cannot disagree.
* A `DocumentSubmission` table shape is reserved: `document_type`, `document_id`,
  `provider`, `status`, `provider_uuid`, `submitted_at`, `response_payload`,
  `error_code` — so adding a provider is an adapter, not a migration of invoices.
* Invoice PDFs are content-hashed and immutable once sent, so a QR/hash-bearing
  variant can be produced without ambiguity about which document was issued.
* Numbering is already gapless per tenant per year, which most e-invoicing regimes require.

---

## 7. Out of scope for v1

Explicitly not built. Listed so nobody plans around them.

| Area | Not in v1 | Why / when |
|---|---|---|
| Manufacturing | BOM, work orders, routing, MRP | Different product; v3 at the earliest |
| Fixed asset register | Depreciation schedules, disposals | v2 — the ledger supports manual depreciation entries meanwhile |
| Budgeting & forecasting | Budget versions, variance workflow, driver-based planning | v2 (project budgets in v1 are the exception) |
| Multi-entity consolidation | Inter-company elimination, consolidated statements | v2; v1 is one legal entity per tenant |
| Purchase requisition / RFQ | Pre-PO approval chain | v2 |
| Landed cost | Freight/duty allocation across receipts | v2 |
| Serial/lot tracking, expiry | | v2 |
| Costing methods other than weighted average | FIFO, standard cost | v2 |
| POS / retail | | Out |
| Recruitment / ATS, performance reviews, LMS | | Out for v1; HR core only |
| Payroll for countries beyond the launch three | | Statutory packs added per market |
| In-app tax filing submission | ETA/ZATCA transmission | v2, on the readiness base of §6.5 |
| Report designer / custom fields UI | | v2 — custom fields exist as tenant `settings` JSONB only |
| Public API v1 with partner marketplace | | API exists for first-party clients and API keys; a documented, versioned public API is v2 |
| Offline-first mobile | Full offline write queue | v1 mobile is read-mostly with offline receipt capture only |
| Schema-per-tenant or per-tenant database | | Deliberately rejected — see ADR-001 in `02-architecture.md` |
| Real-time collaborative editing of documents | | Out |
| Crypto payment methods | | Out |

---

## 8. Traceability

| Requirement group | Architecture section | Data model section | State machine |
|---|---|---|---|
| FR-INV | `02` §3, §5 | `03` §6 (sales) | `04` §2 Invoice |
| FR-PAY | `02` §5, §6 | `03` §7 (payments) | `04` §3 Payment |
| FR-EXP | `02` §5 | `03` §8 (expenses) | `04` §8 Expense |
| FR-STK | `02` §9 | `03` §9 (inventory) | — |
| FR-BNK | `02` §5 | `03` §10 (banking) | — |
| FR-PRJ | `02` §9 | `03` §11 (projects) | `04` §9 Timesheet |
| FR-RPT | `02` §8, §9 | `03` §14 (reporting) | — |
| FR-HR | `02` §4 | `03` §12 (hr) | `04` §7 LeaveRequest |
| FR-PRL | `02` §4, §5 | `03` §13 (payroll) | `04` §6 PayrollRun |
| SEC-05, SEC-06 | `02` §3 | `03` §2, §3 | — |
| AUD-01, AUD-04 | `02` §10 (ADR-003, ADR-005) | `03` §5 | `04` §4 JournalEntry, §5 FiscalPeriod |
