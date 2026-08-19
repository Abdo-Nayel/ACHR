# 05 — Role & permission matrix

**Status:** binding. `backend/config/permissions.json` is the machine-readable
twin of this document and is the file the seeder reads. If the two disagree,
the JSON wins and this document is the bug.

---

## 1. How authorisation is layered

Three independent gates sit between an HTTP request and a row. All three must
pass. They are independent on purpose: each one has a different failure mode,
and no single mistake should be sufficient to leak data.

| Gate | Question | Enforced by | Failure it prevents |
| --- | --- | --- | --- |
| **Tenant** | *Which company's data is this?* | `TenantResolutionMiddleware` → `ContextVar` → `TenantManager` → PostgreSQL RLS | Cross-tenant read. A forgotten `.filter(tenant=...)` still returns nothing, because RLS refuses the row below the ORM. |
| **RBAC** | *May this actor perform this action at all?* | `HasPermission` + `Role` → `RolePermission` → `Permission.codename` | A Department Manager calling `POST /payroll-runs/{id}/approve`. |
| **ABAC** | *On which rows?* | `ScopeRule.strategy` compiled to a `Q` by `build_scope_q()` | An Employee who legitimately holds `payroll.payslip.read` reading a colleague's payslip. |

RBAC without ABAC forces a combinatorial explosion of roles ("HR Manager for
Cairo", "HR Manager for Alexandria"). ABAC without RBAC makes the question
"what can this role do?" uncomputable in an audit. Both, and the matrix stays
small while the answer stays enumerable — this is the split documented in
`apps/iam/models.py`.

### 1.1 Codename grammar

Every permission is `<domain>.<resource>.<action>`, exactly as
`Permission.codename` requires. The grammar is load-bearing: the view layer
derives the required codename from a view's `resource` attribute and its HTTP
method, so a typo raises at startup rather than silently authorising.

`domain` is drawn **only** from `Permission.Domain`, which is a closed
vocabulary of eleven values. Two of the product's user-facing modules do not
have their own domain and are mapped onto an existing one:

| Product module | `Permission.Domain` value | Why |
| --- | --- | --- |
| Payments (`payment`, `refund`, `gateway_config`, `webhook_event`) | `banking` | Money movement and the accounts it moves between are one control surface; splitting them would let a role hold `payment.create` without `bank_account.read`. |
| Expenses (`expense`, `bill`, `vendor`, `category`) | `purchasing` | These *are* the purchase cycle. `Permission.Domain.PURCHASING` already exists; inventing an `expenses` domain would fail the model's `choices` validation. |

`resource` matches `ScopeRule.resource` — that is how a permission and a scope
rule find each other.

One codename breaks the tidy pattern: `accounting.period.post_to_soft_closed`
uses resource `period` rather than `fiscal_period`, because that exact string
is already referenced in the `FiscalPeriod` docstring in
`apps/accounting/models.py`. Renaming it here would make the source comment a
lie; it is carried verbatim and is the single documented exception.

### 1.2 `is_sensitive`

`Permission.is_sensitive` marks actions that **move money or alter posted
books**. The API demands a fresh re-authentication for these (see §6.3) and the
UI shows a confirmation. It is not a synonym for "important" — `iam.role.read`
is important and is not sensitive, because reading a role changes nothing.

---

## 2. The seven system roles

`Role.is_system = True` and `Role.tenant IS NULL` for all seven, which the
`ck_role_system_has_no_tenant` check constraint enforces. Tenants may **clone**
a system role into a custom one but may not edit the originals: if they could,
a product update that adds `inventory.adjustment.approve` would silently grant
it to a customer-modified role.

`rank` is "lower number = more authority" and exists solely to stop privilege
escalation (§6.1).

| `code` | Name | `rank` | Remit |
| --- | --- | :---: | --- |
| `owner` | Owner | **0** | The billing and legal owner of the tenant; holds every permission in the catalogue, including the ones that can destroy the tenant itself. Exists so there is always exactly one accountable human who can re-grant access after everyone else has locked themselves out. |
| `admin` | Admin | **10** | Runs the workspace day to day: invites users, assigns roles of lower rank, configures integrations, reads every module. Deliberately holds no payroll approval and no payslip access by default, so that IT administration and money movement are not the same job. |
| `accountant` | Accountant | **20** | Owns the general ledger, the sales and purchase sub-ledgers, banking and the fiscal calendar; posts, voids, reverses and closes periods. Explicitly excluded from `payroll.payroll_run.approve` so that the person who calculates a payroll run is never the person who releases the money. |
| `hr_manager` | HR Manager | **20** | Owns the employee master file, the org chart, leave policy and the payroll cycle up to and including approval. Sees compensation for the whole tenant but has no ledger access, so HR cannot post an adjusting entry to hide a payroll error. |
| `department_manager` | Department Manager | **30** | A line manager who approves the work and absence of the department subtree they own, and reads the operational data of that subtree only. Their authority is defined almost entirely by ABAC scope rather than by permission grants, which is why one role covers every department in every tenant. |
| `auditor` | Read-Only Auditor | **40** | An external or internal reviewer given time-boxed read access to the books, reports and audit log via `RoleAssignment.valid_until`. Holds no permission whose action mutates state, and is denied the credential surface (`iam.api_key.read`, `banking.gateway_config.read`) because a reviewer never needs it. |
| `employee` | Employee | **50** | The self-service role every staff member with a login receives: their own payslips, leave, attendance, expenses, timesheets and profile. Every grant is paired with an `own_record` scope, so RBAC and ABAC would both have to fail before one employee saw another's pay. |

Two roles share `rank = 20` on purpose. Accountant and HR Manager are peers:
neither may grant the other's role, because the rank rule requires *strictly*
greater rank. That is what keeps the payroll segregation of duties from being
routed around by an HR Manager simply granting themselves Accountant.

### 2.1 Roles are per tenant, not per user

`RoleAssignment` hangs off `TenantMembership`, not off `User`. The same
identity — an outsourced accountant serving five clients — is `accountant` in
one tenant and `auditor` in another. Any code that caches "this user's
permissions" without the tenant in the key is a cross-tenant privilege leak;
see the cache key format in `apps/iam/permissions.py`.

---

## 3. Permission catalogue

187 permissions across eleven domains. Every row here exists as an object in
`permissions[]` in `backend/config/permissions.json`.

<!-- BEGIN GENERATED: catalogue -->

### Accounting (`accounting`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `accounting.account.create` | accounting | account | create | no | Create an account in the chart of accounts. |
| `accounting.account.read` | accounting | account | read | no | View the chart of accounts and account balances. |
| `accounting.account.update` | accounting | account | update | no | Rename or re-parent an account. |
| `accounting.account.archive` | accounting | account | archive | no | Deactivate an account so it can no longer be posted to. |
| `accounting.journal_entry.create` | accounting | journal_entry | create | no | Create a draft journal entry. |
| `accounting.journal_entry.read` | accounting | journal_entry | read | no | View journal entries and their lines. |
| `accounting.journal_entry.update` | accounting | journal_entry | update | no | Edit a draft journal entry before posting. |
| `accounting.journal_entry.post` | accounting | journal_entry | post | **yes** | Post a balanced draft entry to the ledger. |
| `accounting.journal_entry.void` | accounting | journal_entry | void | **yes** | Void a posted entry within an open period. |
| `accounting.journal_entry.reverse` | accounting | journal_entry | reverse | **yes** | Create a reversing mirror entry in the current open period. |
| `accounting.fiscal_period.create` | accounting | fiscal_period | create | no | Generate the periods of a fiscal year. |
| `accounting.fiscal_period.read` | accounting | fiscal_period | read | no | View the fiscal calendar and period statuses. |
| `accounting.fiscal_period.close` | accounting | fiscal_period | close | **yes** | Soft-close or close a period, locking postings. |
| `accounting.fiscal_period.reopen` | accounting | fiscal_period | reopen | **yes** | Reopen a closed period. Break-glass; always audit-logged. |
| `accounting.period.post_to_soft_closed` | accounting | period | post_to_soft_closed | **yes** | Post into a SOFT_CLOSED period during month-end adjustments. Codename referenced verbatim in apps/accounting/models.py::FiscalPeriod. |
| `accounting.tax_rate.read` | accounting | tax_rate | read | no | View VAT / sales-tax definitions. |
| `accounting.tax_rate.manage` | accounting | tax_rate | manage | **yes** | Create, amend or expire a tax rate. |
| `accounting.journal.read` | accounting | journal | read | no | View the books of original entry. |
| `accounting.journal.manage` | accounting | journal | manage | no | Create or configure a journal and its sequence prefix. |
| `accounting.exchange_rate.read` | accounting | exchange_rate | read | no | View the FX rate table. |
| `accounting.exchange_rate.manage` | accounting | exchange_rate | manage | **yes** | Enter or correct an FX rate. |

### Sales (`sales`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `sales.customer.create` | sales | customer | create | no | Create a customer record. |
| `sales.customer.read` | sales | customer | read | no | View customers and their balances. |
| `sales.customer.update` | sales | customer | update | no | Edit customer details, terms and credit limit. |
| `sales.customer.archive` | sales | customer | archive | no | Archive a customer so it stops appearing in pickers. |
| `sales.invoice.create` | sales | invoice | create | no | Create a draft invoice. |
| `sales.invoice.read` | sales | invoice | read | no | View invoices and their lines. |
| `sales.invoice.update` | sales | invoice | update | no | Edit a draft invoice. |
| `sales.invoice.issue` | sales | invoice | issue | **yes** | Issue an invoice: allocate its number and post it to the ledger. |
| `sales.invoice.send` | sales | invoice | send | no | Email or e-invoice an issued invoice to the customer. |
| `sales.invoice.void` | sales | invoice | void | **yes** | Void an issued invoice and reverse its posting. |
| `sales.invoice.write_off` | sales | invoice | write_off | **yes** | Write an uncollectable balance off to bad debt. |
| `sales.credit_note.create` | sales | credit_note | create | no | Create a draft credit note against an invoice. |
| `sales.credit_note.read` | sales | credit_note | read | no | View credit notes. |
| `sales.credit_note.issue` | sales | credit_note | issue | **yes** | Issue a credit note and post it. |
| `sales.credit_note.apply` | sales | credit_note | apply | **yes** | Apply an issued credit note to an outstanding invoice. |
| `sales.recurring_profile.read` | sales | recurring_profile | read | no | View recurring invoice profiles. |
| `sales.recurring_profile.manage` | sales | recurring_profile | manage | no | Create, edit, pause or resume a recurring profile. |
| `sales.recurring_profile.run_now` | sales | recurring_profile | run_now | **yes** | Generate the next invoice from a profile immediately. |

### Payments & Banking (`banking`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `banking.payment.create` | banking | payment | create | **yes** | Record a customer receipt or a vendor payment. |
| `banking.payment.read` | banking | payment | read | no | View payments and their allocations. |
| `banking.payment.void` | banking | payment | void | **yes** | Void a recorded payment and reverse its posting. |
| `banking.payment.allocate` | banking | payment | allocate | **yes** | Allocate or re-allocate a payment across documents. |
| `banking.refund.create` | banking | refund | create | **yes** | Issue a refund back to the original payment method. |
| `banking.refund.read` | banking | refund | read | no | View refunds. |
| `banking.refund.approve` | banking | refund | approve | **yes** | Approve a refund above the role's approval limit. |
| `banking.gateway_config.read` | banking | gateway_config | read | no | View payment gateway configuration (never the secret). |
| `banking.gateway_config.manage` | banking | gateway_config | manage | **yes** | Connect or disconnect a payment gateway. |
| `banking.gateway_config.rotate_secret` | banking | gateway_config | rotate_secret | **yes** | Rotate a gateway API key or webhook signing secret. |
| `banking.webhook_event.read` | banking | webhook_event | read | no | Inspect received gateway webhook events and their status. |
| `banking.webhook_event.replay` | banking | webhook_event | replay | **yes** | Replay a failed webhook event through the handler. |
| `banking.bank_account.create` | banking | bank_account | create | no | Add a bank or cash account. |
| `banking.bank_account.read` | banking | bank_account | read | no | View bank accounts and balances. |
| `banking.bank_account.update` | banking | bank_account | update | no | Edit bank account details. |
| `banking.bank_account.archive` | banking | bank_account | archive | no | Archive a closed bank account. |
| `banking.statement.import` | banking | statement | import | no | Import a bank statement file or sync a feed. |
| `banking.statement.read` | banking | statement | read | no | View imported bank statements and their lines. |
| `banking.reconciliation.create` | banking | reconciliation | create | no | Open a reconciliation session for a period. |
| `banking.reconciliation.read` | banking | reconciliation | read | no | View reconciliation sessions and matches. |
| `banking.reconciliation.match` | banking | reconciliation | match | no | Match or unmatch a statement line to a ledger line. |
| `banking.reconciliation.complete` | banking | reconciliation | complete | **yes** | Finalise a reconciliation and lock its matches. |

### Expenses & Purchasing (`purchasing`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `purchasing.expense.create` | purchasing | expense | create | no | Record an expense claim. |
| `purchasing.expense.read` | purchasing | expense | read | no | View expense claims. |
| `purchasing.expense.update` | purchasing | expense | update | no | Edit an expense claim that is still a draft. |
| `purchasing.expense.submit` | purchasing | expense | submit | no | Submit an expense claim for approval. |
| `purchasing.expense.approve` | purchasing | expense | approve | **yes** | Approve a submitted expense claim. |
| `purchasing.expense.reject` | purchasing | expense | reject | no | Reject a submitted expense claim with a reason. |
| `purchasing.expense.reimburse` | purchasing | expense | reimburse | **yes** | Reimburse an approved claim and post the payment. |
| `purchasing.bill.create` | purchasing | bill | create | no | Enter a vendor bill. |
| `purchasing.bill.read` | purchasing | bill | read | no | View vendor bills. |
| `purchasing.bill.update` | purchasing | bill | update | no | Edit a bill before it is posted. |
| `purchasing.bill.approve` | purchasing | bill | approve | **yes** | Approve a bill for payment. |
| `purchasing.bill.post` | purchasing | bill | post | **yes** | Post an approved bill to accounts payable. |
| `purchasing.bill.void` | purchasing | bill | void | **yes** | Void a posted bill and reverse it. |
| `purchasing.vendor.create` | purchasing | vendor | create | no | Create a vendor record. |
| `purchasing.vendor.read` | purchasing | vendor | read | no | View vendors and their balances. |
| `purchasing.vendor.update` | purchasing | vendor | update | **yes** | Edit vendor details and bank instructions. |
| `purchasing.vendor.archive` | purchasing | vendor | archive | no | Archive a vendor. |
| `purchasing.category.read` | purchasing | category | read | no | View expense categories and their default accounts. |
| `purchasing.category.manage` | purchasing | category | manage | no | Create or edit an expense category. |

### Inventory (`inventory`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `inventory.item.create` | inventory | item | create | no | Create a stock or service item. |
| `inventory.item.read` | inventory | item | read | no | View items, SKUs and stock on hand. |
| `inventory.item.update` | inventory | item | update | no | Edit item details. |
| `inventory.item.archive` | inventory | item | archive | no | Archive an item. |
| `inventory.item.update_cost` | inventory | item | update_cost | **yes** | Override an item's standard or average cost. |
| `inventory.warehouse.read` | inventory | warehouse | read | no | View warehouses and locations. |
| `inventory.warehouse.manage` | inventory | warehouse | manage | no | Create or edit a warehouse. |
| `inventory.stock_movement.create` | inventory | stock_movement | create | no | Record a receipt, issue or transfer. |
| `inventory.stock_movement.read` | inventory | stock_movement | read | no | View stock movement history. |
| `inventory.stock_movement.post` | inventory | stock_movement | post | **yes** | Post a stock movement and its inventory journal entry. |
| `inventory.adjustment.create` | inventory | adjustment | create | no | Raise a stock adjustment (count variance, write-off). |
| `inventory.adjustment.read` | inventory | adjustment | read | no | View stock adjustments. |
| `inventory.adjustment.approve` | inventory | adjustment | approve | **yes** | Approve and post a stock adjustment. |
| `inventory.price_list.read` | inventory | price_list | read | no | View price lists. |
| `inventory.price_list.manage` | inventory | price_list | manage | no | Create or edit a price list and its entries. |

### Projects (`projects`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `projects.project.create` | projects | project | create | no | Create a project. |
| `projects.project.read` | projects | project | read | no | View projects, budgets and profitability. |
| `projects.project.update` | projects | project | update | no | Edit project details and budget. |
| `projects.project.archive` | projects | project | archive | no | Archive a project. |
| `projects.project.close` | projects | project | close | **yes** | Close a project to further time and cost. |
| `projects.task.create` | projects | task | create | no | Create a task on a project. |
| `projects.task.read` | projects | task | read | no | View tasks. |
| `projects.task.update` | projects | task | update | no | Edit a task. |
| `projects.task.assign` | projects | task | assign | no | Assign a task to a team member. |
| `projects.timesheet_entry.create` | projects | timesheet_entry | create | no | Log time against a project or task. |
| `projects.timesheet_entry.read` | projects | timesheet_entry | read | no | View timesheet entries. |
| `projects.timesheet_entry.update` | projects | timesheet_entry | update | no | Edit an unsubmitted timesheet entry. |
| `projects.timesheet_entry.submit` | projects | timesheet_entry | submit | no | Submit a timesheet for approval. |
| `projects.timesheet_entry.approve` | projects | timesheet_entry | approve | **yes** | Approve a submitted timesheet, making it billable. |

### Human Resources (`hr`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `hr.employee.create` | hr | employee | create | no | Create an employee record. |
| `hr.employee.read` | hr | employee | read | no | View employee records excluding compensation. |
| `hr.employee.update` | hr | employee | update | no | Edit employee personal and job details. |
| `hr.employee.terminate` | hr | employee | terminate | **yes** | Terminate an employee and trigger final settlement. |
| `hr.employee.read_compensation` | hr | employee | read_compensation | **yes** | View salary, bank details and compensation history. |
| `hr.employee.export` | hr | employee | export | **yes** | Export employee records containing personal data. |
| `hr.department.create` | hr | department | create | no | Create a department in the org chart. |
| `hr.department.read` | hr | department | read | no | View the org chart. |
| `hr.department.update` | hr | department | update | **yes** | Rename or re-parent a department. |
| `hr.department.archive` | hr | department | archive | no | Archive a department with no active employees. |
| `hr.document.read` | hr | document | read | no | View employee documents (contracts, certificates, IDs). |
| `hr.document.manage` | hr | document | manage | no | Upload, replace or delete an employee document. |
| `hr.attendance.create` | hr | attendance | create | no | Record a clock-in / clock-out or a manual attendance row. |
| `hr.attendance.read` | hr | attendance | read | no | View attendance records. |
| `hr.attendance.update` | hr | attendance | update | no | Correct an attendance record. |
| `hr.attendance.approve` | hr | attendance | approve | no | Approve corrected or overtime attendance. |
| `hr.leave_request.create` | hr | leave_request | create | no | Request leave. |
| `hr.leave_request.read` | hr | leave_request | read | no | View leave requests. |
| `hr.leave_request.update` | hr | leave_request | update | no | Edit a pending leave request. |
| `hr.leave_request.cancel` | hr | leave_request | cancel | no | Cancel a leave request and restore the balance. |
| `hr.leave_request.approve` | hr | leave_request | approve | **yes** | Approve a leave request and consume the balance. |
| `hr.leave_request.reject` | hr | leave_request | reject | no | Reject a leave request with a reason. |
| `hr.leave_type.read` | hr | leave_type | read | no | View leave types and their accrual policy. |
| `hr.leave_type.manage` | hr | leave_type | manage | **yes** | Create or edit a leave type and its accrual rules. |
| `hr.leave_balance.read` | hr | leave_balance | read | no | View leave balances. |
| `hr.leave_balance.adjust` | hr | leave_balance | adjust | **yes** | Manually adjust a leave balance. |

### Payroll (`payroll`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `payroll.component.read` | payroll | component | read | no | View salary components (earnings, deductions, benefits). |
| `payroll.component.manage` | payroll | component | manage | **yes** | Create or edit a salary component and its formula. |
| `payroll.payroll_run.create` | payroll | payroll_run | create | no | Open a payroll run for a pay period. |
| `payroll.payroll_run.read` | payroll | payroll_run | read | no | View payroll runs and their totals. |
| `payroll.payroll_run.calculate` | payroll | payroll_run | calculate | **yes** | Calculate gross-to-net for every employee in the run. |
| `payroll.payroll_run.approve` | payroll | payroll_run | approve | **yes** | Approve a calculated payroll run, releasing it for payment. |
| `payroll.payroll_run.post` | payroll | payroll_run | post | **yes** | Post an approved payroll run to the ledger. |
| `payroll.payroll_run.pay` | payroll | payroll_run | pay | **yes** | Generate the payment file / disburse an approved run. |
| `payroll.payroll_run.void` | payroll | payroll_run | void | **yes** | Void a payroll run and reverse its posting. |
| `payroll.payslip.read` | payroll | payslip | read | no | View payslips. |
| `payroll.payslip.publish` | payroll | payslip | publish | **yes** | Publish payslips to the employee self-service portal. |
| `payroll.payslip.export` | payroll | payslip | export | **yes** | Export payslips or a bank transfer file. |
| `payroll.tax_bracket.read` | payroll | tax_bracket | read | no | View income-tax brackets and social-insurance ceilings. |
| `payroll.tax_bracket.manage` | payroll | tax_bracket | manage | **yes** | Edit income-tax brackets and statutory ceilings. |

### Reporting (`reporting`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `reporting.profit_loss.read` | reporting | profit_loss | read | no | Run the profit & loss statement. |
| `reporting.balance_sheet.read` | reporting | balance_sheet | read | no | Run the balance sheet. |
| `reporting.trial_balance.read` | reporting | trial_balance | read | no | Run the trial balance. |
| `reporting.cash_flow.read` | reporting | cash_flow | read | no | Run the cash flow statement. |
| `reporting.tax_summary.read` | reporting | tax_summary | read | no | Run the VAT / tax summary for a filing period. |
| `reporting.aging.read` | reporting | aging | read | no | Run AR and AP ageing reports. |
| `reporting.payroll_register.read` | reporting | payroll_register | read | **yes** | Run the payroll register for a pay period. |
| `reporting.report.export` | reporting | report | export | **yes** | Export any permitted report to CSV / XLSX / PDF. |

### Settings (`settings`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `settings.organisation.read` | settings | organisation | read | no | View organisation profile, fiscal year and base currency. |
| `settings.organisation.update` | settings | organisation | update | **yes** | Edit organisation profile and tax registration. |
| `settings.branding.read` | settings | branding | read | no | View document templates and branding. |
| `settings.branding.manage` | settings | branding | manage | no | Edit document templates, logo and email footers. |
| `settings.sequence.read` | settings | sequence | read | no | View document numbering sequences. |
| `settings.sequence.manage` | settings | sequence | manage | **yes** | Change a document numbering sequence. |
| `settings.integration.read` | settings | integration | read | no | View connected third-party integrations. |
| `settings.integration.manage` | settings | integration | manage | **yes** | Connect, configure or disconnect an integration. |
| `settings.notification.read` | settings | notification | read | no | View notification and reminder rules. |
| `settings.notification.manage` | settings | notification | manage | no | Edit notification and reminder rules. |
| `settings.audit_log.read` | settings | audit_log | read | no | Read the tenant audit log. |
| `settings.audit_log.export` | settings | audit_log | export | **yes** | Export the tenant audit log. |
| `settings.export.create` | settings | export | create | **yes** | Request a full-tenant data export. |

### Access control (`iam`)

| Codename | Domain | Resource | Action | Sensitive? | Description |
| --- | --- | --- | --- | :---: | --- |
| `iam.user.invite` | iam | user | invite | no | Invite a user to the tenant. |
| `iam.user.read` | iam | user | read | no | View users and their membership status. |
| `iam.user.update` | iam | user | update | no | Edit a user's profile within this tenant. |
| `iam.user.deactivate` | iam | user | deactivate | **yes** | Deactivate a membership, revoking access immediately. |
| `iam.user.reset_password` | iam | user | reset_password | **yes** | Force a password reset / MFA re-enrolment. |
| `iam.user.impersonate` | iam | user | impersonate | **yes** | Impersonate a user for support. Always audit-logged. |
| `iam.role.create` | iam | role | create | **yes** | Clone a system role into a custom tenant role. |
| `iam.role.read` | iam | role | read | no | View roles and the permissions they bundle. |
| `iam.role.update` | iam | role | update | **yes** | Edit a custom role's permissions or scope rules. |
| `iam.role.delete` | iam | role | delete | **yes** | Delete an unassigned custom role. |
| `iam.membership.read` | iam | membership | read | no | View memberships and their role assignments. |
| `iam.membership.assign_role` | iam | membership | assign_role | **yes** | Assign a role to a membership. |
| `iam.membership.revoke_role` | iam | membership | revoke_role | **yes** | Revoke a role assignment. |
| `iam.membership.transfer_ownership` | iam | membership | transfer_ownership | **yes** | Transfer tenant ownership to another membership. |
| `iam.api_key.create` | iam | api_key | create | **yes** | Create a machine API key. Plaintext shown once. |
| `iam.api_key.read` | iam | api_key | read | no | List API keys by prefix and last-used timestamp. |
| `iam.api_key.revoke` | iam | api_key | revoke | **yes** | Revoke an API key immediately. |

<!-- END GENERATED: catalogue -->

---

## 4. Role × permission matrix

`✓` = the codename is in that role's `permissions[]`. `—` = absent, and the
request is refused by `HasPermission` before any queryset is built.

Read this table together with §5: a `✓` answers "may the actor perform this
action", never "on which rows". `employee` holds `payroll.payslip.read` and
still cannot read anyone else's payslip.

<!-- BEGIN GENERATED: matrix -->

### Accounting (`accounting`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `accounting.account.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `accounting.account.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| `accounting.account.update` | ✓ | ✓ | ✓ | — | — | — | — |
| `accounting.account.archive` | ✓ | — | ✓ | — | — | — | — |
| `accounting.journal_entry.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `accounting.journal_entry.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `accounting.journal_entry.update` | ✓ | ✓ | ✓ | — | — | — | — |
| `accounting.journal_entry.post` | ✓ | — | ✓ | — | — | — | — |
| `accounting.journal_entry.void` | ✓ | — | ✓ | — | — | — | — |
| `accounting.journal_entry.reverse` | ✓ | — | ✓ | — | — | — | — |
| `accounting.fiscal_period.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `accounting.fiscal_period.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| `accounting.fiscal_period.close` | ✓ | — | ✓ | — | — | — | — |
| `accounting.fiscal_period.reopen` | ✓ | — | — | — | — | — | — |
| `accounting.period.post_to_soft_closed` | ✓ | — | ✓ | — | — | — | — |
| `accounting.tax_rate.read` | ✓ | ✓ | ✓ | ✓ | — | ✓ | — |
| `accounting.tax_rate.manage` | ✓ | — | ✓ | — | — | — | — |
| `accounting.journal.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `accounting.journal.manage` | ✓ | — | ✓ | — | — | — | — |
| `accounting.exchange_rate.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `accounting.exchange_rate.manage` | ✓ | — | ✓ | — | — | — | — |

### Sales (`sales`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `sales.customer.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `sales.customer.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `sales.customer.update` | ✓ | ✓ | ✓ | — | — | — | — |
| `sales.customer.archive` | ✓ | ✓ | ✓ | — | — | — | — |
| `sales.invoice.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `sales.invoice.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `sales.invoice.update` | ✓ | ✓ | ✓ | — | — | — | — |
| `sales.invoice.issue` | ✓ | — | ✓ | — | — | — | — |
| `sales.invoice.send` | ✓ | ✓ | ✓ | — | — | — | — |
| `sales.invoice.void` | ✓ | — | ✓ | — | — | — | — |
| `sales.invoice.write_off` | ✓ | — | ✓ | — | — | — | — |
| `sales.credit_note.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `sales.credit_note.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `sales.credit_note.issue` | ✓ | — | ✓ | — | — | — | — |
| `sales.credit_note.apply` | ✓ | — | ✓ | — | — | — | — |
| `sales.recurring_profile.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `sales.recurring_profile.manage` | ✓ | ✓ | ✓ | — | — | — | — |
| `sales.recurring_profile.run_now` | ✓ | — | ✓ | — | — | — | — |

### Payments & Banking (`banking`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `banking.payment.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `banking.payment.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `banking.payment.void` | ✓ | — | ✓ | — | — | — | — |
| `banking.payment.allocate` | ✓ | — | ✓ | — | — | — | — |
| `banking.refund.create` | ✓ | — | ✓ | — | — | — | — |
| `banking.refund.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `banking.refund.approve` | ✓ | ✓ | — | — | — | — | — |
| `banking.gateway_config.read` | ✓ | ✓ | — | — | — | — | — |
| `banking.gateway_config.manage` | ✓ | — | — | — | — | — | — |
| `banking.gateway_config.rotate_secret` | ✓ | — | — | — | — | — | — |
| `banking.webhook_event.read` | ✓ | ✓ | — | — | — | ✓ | — |
| `banking.webhook_event.replay` | ✓ | ✓ | — | — | — | — | — |
| `banking.bank_account.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `banking.bank_account.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `banking.bank_account.update` | ✓ | ✓ | ✓ | — | — | — | — |
| `banking.bank_account.archive` | ✓ | — | ✓ | — | — | — | — |
| `banking.statement.import` | ✓ | ✓ | ✓ | — | — | — | — |
| `banking.statement.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `banking.reconciliation.create` | ✓ | — | ✓ | — | — | — | — |
| `banking.reconciliation.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `banking.reconciliation.match` | ✓ | — | ✓ | — | — | — | — |
| `banking.reconciliation.complete` | ✓ | — | ✓ | — | — | — | — |

### Expenses & Purchasing (`purchasing`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `purchasing.expense.create` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `purchasing.expense.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `purchasing.expense.update` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `purchasing.expense.submit` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `purchasing.expense.approve` | ✓ | ✓ | — | — | ✓ | — | — |
| `purchasing.expense.reject` | ✓ | ✓ | — | — | ✓ | — | — |
| `purchasing.expense.reimburse` | ✓ | — | ✓ | — | — | — | — |
| `purchasing.bill.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `purchasing.bill.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `purchasing.bill.update` | ✓ | ✓ | ✓ | — | — | — | — |
| `purchasing.bill.approve` | ✓ | ✓ | — | — | — | — | — |
| `purchasing.bill.post` | ✓ | — | ✓ | — | — | — | — |
| `purchasing.bill.void` | ✓ | — | ✓ | — | — | — | — |
| `purchasing.vendor.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `purchasing.vendor.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `purchasing.vendor.update` | ✓ | — | ✓ | — | — | — | — |
| `purchasing.vendor.archive` | ✓ | ✓ | ✓ | — | — | — | — |
| `purchasing.category.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `purchasing.category.manage` | ✓ | ✓ | ✓ | — | — | — | — |

### Inventory (`inventory`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `inventory.item.create` | ✓ | ✓ | ✓ | — | — | — | — |
| `inventory.item.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `inventory.item.update` | ✓ | ✓ | ✓ | — | — | — | — |
| `inventory.item.archive` | ✓ | ✓ | ✓ | — | — | — | — |
| `inventory.item.update_cost` | ✓ | — | ✓ | — | — | — | — |
| `inventory.warehouse.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `inventory.warehouse.manage` | ✓ | ✓ | ✓ | — | — | — | — |
| `inventory.stock_movement.create` | ✓ | ✓ | ✓ | — | ✓ | — | — |
| `inventory.stock_movement.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `inventory.stock_movement.post` | ✓ | — | ✓ | — | — | — | — |
| `inventory.adjustment.create` | ✓ | ✓ | ✓ | — | ✓ | — | — |
| `inventory.adjustment.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `inventory.adjustment.approve` | ✓ | — | ✓ | — | — | — | — |
| `inventory.price_list.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `inventory.price_list.manage` | ✓ | ✓ | ✓ | — | — | — | — |

### Projects (`projects`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `projects.project.create` | ✓ | ✓ | ✓ | — | ✓ | — | — |
| `projects.project.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | ✓ |
| `projects.project.update` | ✓ | ✓ | ✓ | — | ✓ | — | — |
| `projects.project.archive` | ✓ | ✓ | ✓ | — | — | — | — |
| `projects.project.close` | ✓ | ✓ | — | — | ✓ | — | — |
| `projects.task.create` | ✓ | ✓ | — | — | ✓ | — | ✓ |
| `projects.task.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | ✓ |
| `projects.task.update` | ✓ | ✓ | — | — | ✓ | — | ✓ |
| `projects.task.assign` | ✓ | ✓ | — | — | ✓ | — | — |
| `projects.timesheet_entry.create` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `projects.timesheet_entry.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `projects.timesheet_entry.update` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `projects.timesheet_entry.submit` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `projects.timesheet_entry.approve` | ✓ | ✓ | — | — | ✓ | — | — |

### Human Resources (`hr`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `hr.employee.create` | ✓ | ✓ | — | ✓ | — | — | — |
| `hr.employee.read` | ✓ | ✓ | — | ✓ | ✓ | ✓ | — |
| `hr.employee.update` | ✓ | ✓ | — | ✓ | — | — | — |
| `hr.employee.terminate` | ✓ | — | — | ✓ | — | — | — |
| `hr.employee.read_compensation` | ✓ | — | — | ✓ | — | — | — |
| `hr.employee.export` | ✓ | — | — | ✓ | — | — | — |
| `hr.department.create` | ✓ | ✓ | — | ✓ | — | — | — |
| `hr.department.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `hr.department.update` | ✓ | — | — | ✓ | — | — | — |
| `hr.department.archive` | ✓ | — | — | ✓ | — | — | — |
| `hr.document.read` | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| `hr.document.manage` | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| `hr.attendance.create` | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| `hr.attendance.read` | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| `hr.attendance.update` | ✓ | ✓ | — | ✓ | ✓ | — | — |
| `hr.attendance.approve` | ✓ | ✓ | — | ✓ | ✓ | — | — |
| `hr.leave_request.create` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `hr.leave_request.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `hr.leave_request.update` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `hr.leave_request.cancel` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `hr.leave_request.approve` | ✓ | — | — | ✓ | ✓ | — | — |
| `hr.leave_request.reject` | ✓ | — | — | ✓ | ✓ | — | — |
| `hr.leave_type.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `hr.leave_type.manage` | ✓ | — | — | ✓ | — | — | — |
| `hr.leave_balance.read` | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| `hr.leave_balance.adjust` | ✓ | — | — | ✓ | — | — | — |

### Payroll (`payroll`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `payroll.component.read` | ✓ | ✓ | — | ✓ | — | ✓ | — |
| `payroll.component.manage` | ✓ | — | — | ✓ | — | — | — |
| `payroll.payroll_run.create` | ✓ | ✓ | — | ✓ | — | — | — |
| `payroll.payroll_run.read` | ✓ | ✓ | ✓ | ✓ | — | ✓ | — |
| `payroll.payroll_run.calculate` | ✓ | — | ✓ | ✓ | — | — | — |
| `payroll.payroll_run.approve` | ✓ | — | — | ✓ | — | — | — |
| `payroll.payroll_run.post` | ✓ | — | ✓ | — | — | — | — |
| `payroll.payroll_run.pay` | ✓ | — | ✓ | — | — | — | — |
| `payroll.payroll_run.void` | ✓ | — | — | — | — | — | — |
| `payroll.payslip.read` | ✓ | ✓ | — | ✓ | — | ✓ | ✓ |
| `payroll.payslip.publish` | ✓ | — | — | ✓ | — | — | — |
| `payroll.payslip.export` | ✓ | — | — | ✓ | — | ✓ | — |
| `payroll.tax_bracket.read` | ✓ | ✓ | ✓ | ✓ | — | ✓ | — |
| `payroll.tax_bracket.manage` | ✓ | — | — | — | — | — | — |

### Reporting (`reporting`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `reporting.profit_loss.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `reporting.balance_sheet.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `reporting.trial_balance.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `reporting.cash_flow.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `reporting.tax_summary.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `reporting.aging.read` | ✓ | ✓ | ✓ | — | ✓ | ✓ | — |
| `reporting.payroll_register.read` | ✓ | — | ✓ | ✓ | — | ✓ | — |
| `reporting.report.export` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |

### Settings (`settings`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `settings.organisation.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| `settings.organisation.update` | ✓ | ✓ | — | — | — | — | — |
| `settings.branding.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `settings.branding.manage` | ✓ | ✓ | — | — | — | — | — |
| `settings.sequence.read` | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `settings.sequence.manage` | ✓ | — | — | — | — | — | — |
| `settings.integration.read` | ✓ | ✓ | — | — | — | ✓ | — |
| `settings.integration.manage` | ✓ | ✓ | — | — | — | — | — |
| `settings.notification.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| `settings.notification.manage` | ✓ | ✓ | — | — | — | — | — |
| `settings.audit_log.read` | ✓ | ✓ | — | — | — | ✓ | — |
| `settings.audit_log.export` | ✓ | ✓ | — | — | — | ✓ | — |
| `settings.export.create` | ✓ | — | — | — | — | — | — |

### Access control (`iam`)

| Permission | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `iam.user.invite` | ✓ | ✓ | — | ✓ | — | — | — |
| `iam.user.read` | ✓ | ✓ | — | ✓ | ✓ | ✓ | — |
| `iam.user.update` | ✓ | ✓ | — | — | — | — | — |
| `iam.user.deactivate` | ✓ | ✓ | — | — | — | — | — |
| `iam.user.reset_password` | ✓ | ✓ | — | — | — | — | — |
| `iam.user.impersonate` | ✓ | — | — | — | — | — | — |
| `iam.role.create` | ✓ | ✓ | — | — | — | — | — |
| `iam.role.read` | ✓ | ✓ | — | ✓ | ✓ | ✓ | — |
| `iam.role.update` | ✓ | ✓ | — | — | — | — | — |
| `iam.role.delete` | ✓ | ✓ | — | — | — | — | — |
| `iam.membership.read` | ✓ | ✓ | — | ✓ | ✓ | ✓ | — |
| `iam.membership.assign_role` | ✓ | ✓ | — | ✓ | — | — | — |
| `iam.membership.revoke_role` | ✓ | ✓ | — | ✓ | — | — | — |
| `iam.membership.transfer_ownership` | ✓ | — | — | — | — | — | — |
| `iam.api_key.create` | ✓ | ✓ | — | — | — | — | — |
| `iam.api_key.read` | ✓ | ✓ | — | — | — | — | — |
| `iam.api_key.revoke` | ✓ | ✓ | — | — | — | — | — |

**Totals** — 187 permissions in the catalogue.

| | Owner | Admin | Accountant | HR Manager | Department Manager | Read-Only Auditor | Employee |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Permissions held | 187 | 132 | 117 | 58 | 56 | 57 | 25 |
<!-- END GENERATED: matrix -->

---

## 5. ABAC scope rules

A `ScopeRule` is attached to a `(role, resource)` pair — unique, per
`uq_scope_rule_role_resource` — and is compiled into an ORM `Q` object by
`build_scope_q(user, resource)` in `apps/iam/permissions.py`.

`strategy` is a closed enum, never free-form expression text. An expression
evaluator in an authorisation layer means one injection bug is a full data
breach; a fixed enum means every possible predicate was reviewed once, in code
review, and can never be authored by a tenant admin at runtime.

### 5.1 The eight strategies

| `ScopeRule.Strategy` | Compiles to | Notes |
| --- | --- | --- |
| `all` | `Q()` | Every row in the tenant. The tenant filter is still applied — `all` means "all of *this* tenant". |
| `own_record` | `Q(employee_id=...)` / `Q(user_id=...)` / `Q(created_by_id=...)` | The actor's own rows, resolved through `TenantMembership.employee`. |
| `own_department` | `Q(department_id=...)` | Exactly the actor's department node, no descendants. |
| `department_subtree` | `Q(department__path__startswith=<my path>)` | The actor's department **and everything below it**, via the materialised `Department.path`. |
| `assigned_projects` | `Q(project_id__in=<my project ids>)` | Projects the actor is a member of. |
| `managed_employees` | `Q(employee__manager_id=<my employee id>)` | Direct reports only, via the `Employee.manager` FK — not the org-chart subtree. |
| `scoped_department` | `Q(department__path__startswith=<assignment path>)` | The department named on **`RoleAssignment.department`**, not the actor's own. This is what makes "HR Manager, Alexandria branch only" a scoped assignment of one role instead of a second role. |
| `none` | `Q(pk__in=[])` | No rows, ever. An explicit deny, not an absent rule. |

**Why `department_subtree` must use the `path` prefix and not recursion.** A
recursive CTE or an `id__in=descendant_ids()` Python walk issues one query per
level and is O(depth) round trips on every list request; worse, it is
unindexable, so a manager over a 4 000-person division full-scans the employee
table. The materialised `path` (`/root_uuid/child_uuid/…`) turns the whole
subtree into a single `LIKE '<prefix>%'` that a `varchar_pattern_ops` B-tree
serves. A department move rewrites the affected paths in one `UPDATE`; that
cost is paid on reorganisation, which happens monthly, instead of on every
request, which happens continuously.

**Why `none` is an explicit row and not a missing row.** `build_scope_q()`
fails closed — a resource with no matching `ScopeRule` also yields no rows. But
an explicit `none` records *intent*: "Admin deliberately cannot read payslips"
is a reviewed decision that shows up in the matrix, whereas a missing rule is
indistinguishable from someone forgetting to add one.

### 5.2 System role scopes

<!-- BEGIN GENERATED: scopes -->

| Role | Resource | `ScopeRule.strategy` | `parameters` | Plain-English meaning |
| --- | --- | --- | --- | --- |
| Owner | `payroll_run` | `all` | `{"exclude_self_prepared": true, "break_glass": true, "audit_action": "payroll_approved"}` | Owner may approve a run they prepared, but only as an explicit break-glass action that writes `TenantAuditLog.Action.PAYROLL_APPROVED` with `break_glass=true`. Needed because a one-person tenant would otherwise be unable to run payroll at all. |
| Admin | `payroll_run` | `all` | `{"exclude_self_prepared": true}` | Admin may approve any run except one they themselves calculated. |
| Admin | `expense` | `all` | `{"max_amount": "50000.00", "exclude_self_prepared": true}` | Admin approves claims up to 50 000 base currency, never their own. |
| Admin | `refund` | `all` | `{"max_amount": "50000.00"}` | Admin approves refunds up to 50 000 base currency. |
| Admin | `bill` | `all` | `{"max_amount": "50000.00", "exclude_self_prepared": true}` | Admin approves bills up to 50 000 base currency, never one they entered. |
| Admin | `payslip` | `none` | `{}` | Admin sees no payslips at all: workspace administration is not a reason to see anyone's pay. |
| Admin | `employee` | `all` | `{"hide_compensation": true}` | Admin sees every employee record with compensation fields stripped by the serialiser. |
| Accountant | `payroll_run` | `all` | `{"approve": false}` | Accountant sees and posts every run but the `approve` transition is not in their permission set — the segregation-of-duties split. |
| Accountant | `payslip` | `none` | `{}` | Accountant never reads an individual payslip; they work from `reporting.payroll_register`, which is aggregated. |
| Accountant | `employee` | `all` | `{"hide_compensation": true}` | Accountant sees employees for cost allocation, with compensation stripped. |
| Accountant | `journal_entry` | `all` | `{}` | Full ledger access across the tenant. |
| Accountant | `invoice` | `all` | `{}` | Full sales sub-ledger access across the tenant. |
| Accountant | `payment` | `all` | `{"max_amount": "250000.00"}` | May record and allocate payments up to 250 000 base currency per transaction; above that an Owner must act. |
| Accountant | `expense` | `all` | `{"exclude_self_prepared": true}` | Reimburses any claim except one they submitted. |
| Accountant | `leave_request` | `own_record` | `{}` | Their own leave only — an Accountant is an employee too. |
| Accountant | `timesheet_entry` | `own_record` | `{}` | Their own timesheets only. |
| HR Manager | `employee` | `all` | `{}` | Whole-tenant employee master file, compensation included. |
| HR Manager | `payslip` | `all` | `{}` | Every payslip in the tenant. |
| HR Manager | `payroll_run` | `all` | `{"exclude_self_prepared": true}` | Approves any run except one they calculated themselves. |
| HR Manager | `leave_request` | `all` | `{"exclude_self_prepared": true}` | Approves any leave request except their own. |
| HR Manager | `leave_balance` | `all` | `{}` | Whole-tenant leave balances, including manual adjustment. |
| HR Manager | `attendance` | `all` | `{}` | Whole-tenant attendance. |
| HR Manager | `document` | `all` | `{}` | Every employee document in the tenant. |
| HR Manager | `department` | `all` | `{}` | Whole org chart. |
| HR Manager | `expense` | `own_record` | `{}` | Their own expense claims only. |
| HR Manager | `timesheet_entry` | `own_record` | `{}` | Their own timesheets only. |
| Department Manager | `employee` | `department_subtree` | `{}` | Employees in their department and every department beneath it, matched on the materialised `Department.path` prefix. |
| Department Manager | `department` | `department_subtree` | `{}` | Their department node and its descendants. |
| Department Manager | `leave_request` | `department_subtree` | `{"exclude_self_prepared": true}` | Approves leave for the whole subtree, never their own request. |
| Department Manager | `attendance` | `department_subtree` | `{}` | Attendance for the subtree. |
| Department Manager | `leave_balance` | `department_subtree` | `{}` | Leave balances for the subtree. |
| Department Manager | `document` | `own_record` | `{}` | Their own documents only — being a manager is not a reason to read a report's ID scan. |
| Department Manager | `expense` | `department_subtree` | `{"max_amount": "5000.00", "exclude_self_prepared": true}` | Approves subtree claims up to 5 000 base currency; anything larger escalates to Admin. Never their own. |
| Department Manager | `timesheet_entry` | `department_subtree` | `{"exclude_self_prepared": true}` | Approves subtree timesheets, never their own. |
| Department Manager | `project` | `assigned_projects` | `{}` | Projects they are a member of. |
| Department Manager | `task` | `assigned_projects` | `{}` | Tasks on projects they are a member of. |
| Department Manager | `payslip` | `none` | `{}` | No payslips. |
| Department Manager | `invoice` | `assigned_projects` | `{}` | Invoices linked to their projects only. |
| Department Manager | `customer` | `assigned_projects` | `{}` | Customers of their projects only. |
| Read-Only Auditor | `journal_entry` | `all` | `{"read_only": true}` | Reads the whole ledger; every write permission is absent from the role, and `read_only` makes the intent explicit to the policy layer. |
| Read-Only Auditor | `invoice` | `all` | `{"read_only": true}` | Reads every invoice, writes none. |
| Read-Only Auditor | `payment` | `all` | `{"read_only": true}` | Reads every payment, writes none. |
| Read-Only Auditor | `employee` | `all` | `{"read_only": true, "hide_compensation": true}` | Reads employee records with compensation stripped — an auditor verifying controls does not need individual salaries. |
| Read-Only Auditor | `payslip` | `none` | `{}` | No payslips. |
| Read-Only Auditor | `document` | `none` | `{}` | No employee documents (contracts and ID scans are PII with no audit value). |
| Read-Only Auditor | `leave_request` | `all` | `{"read_only": true}` | Reads leave for accrual testing. |
| Employee | `payslip` | `own_record` | `{}` | Their own payslips only — the canonical `own_record` case. |
| Employee | `leave_request` | `own_record` | `{}` | Their own leave requests. |
| Employee | `leave_balance` | `own_record` | `{}` | Their own balances. |
| Employee | `attendance` | `own_record` | `{}` | Their own attendance. |
| Employee | `expense` | `own_record` | `{}` | Their own expense claims. |
| Employee | `timesheet_entry` | `own_record` | `{}` | Their own timesheets. |
| Employee | `document` | `own_record` | `{}` | Their own documents. |
| Employee | `employee` | `own_record` | `{}` | Their own employee record. |
| Employee | `department` | `own_department` | `{}` | Their own department node, for the team directory. |
| Employee | `project` | `assigned_projects` | `{}` | Projects they are assigned to. |
| Employee | `task` | `assigned_projects` | `{}` | Tasks on projects they are assigned to. |
| Employee | `payroll_run` | `none` | `{}` | Nothing — an employee never sees a run, only their slip. |
| Employee | `journal_entry` | `none` | `{}` | Nothing. |

<!-- END GENERATED: scopes -->

### 5.3 Strategies reserved for custom roles

`managed_employees` and `scoped_department` are used by **no system role** and
therefore do not appear in `permissions.json`, which seeds system roles only.
They exist because `RoleAssignment` carries `department` and `project` FKs, and
those FKs are pointless unless some strategy reads them. These are the two
canonical custom roles a tenant clones:

| Custom role | Resource | `strategy` | `parameters` | Meaning |
| --- | --- | --- | --- | --- |
| Team Lead (clone of `employee`, rank 40) | `timesheet_entry` | `managed_employees` | `{"exclude_self_prepared": true}` | Approves timesheets for their **direct reports** only. Narrower than `department_subtree`: a team lead inside a large department approves their six people, not the department's ninety. |
| Team Lead | `leave_request` | `managed_employees` | `{"max_days": "3"}` | Approves short absences for direct reports; anything longer escalates to the Department Manager. |
| Branch Payroll Officer (clone of `hr_manager`, rank 25) | `employee` | `scoped_department` | `{}` | Sees only the department subtree named on **their own `RoleAssignment.department`**. Assigning the same role twice with two different departments gives one person two branches without creating a second role. |
| Branch Payroll Officer | `payslip` | `scoped_department` | `{}` | Payslips of that branch only. |
| Project Billing Approver (clone of `accountant`, rank 25) | `invoice` | `assigned_projects` | `{"max_amount": "25000.00"}` | Issues invoices only for the projects named on `RoleAssignment.project`, up to 25 000. |

### 5.4 The four scope rules that matter most

**"An Employee sees only their own payslip."**
`employee` × `payslip` × `own_record`. The employee → payslip link is resolved
through `TenantMembership.employee`, not through `User`, because
`TenantMembership.employee` is the only place the two identities are joined
(`uq_membership_tenant_employee`). A user with no linked employee row gets
`Q(pk__in=[])` and an empty list — not an error, and not everyone's payslips.

**"A Department Manager approves leave for their department subtree."**
`department_manager` × `leave_request` × `department_subtree` with
`{"exclude_self_prepared": true}`. Two properties matter. First, the subtree,
not the node: a manager over Engineering approves for Engineering → Platform →
Payments without three role assignments. Second, the exclusion: a manager who
is themselves in the subtree would otherwise approve their own holiday, which
is the most commonly exploited hole in every HR system that ships without it.
Their own request routes upward to their manager's manager.

**"An Auditor reads everything and writes nothing."**
This is enforced at RBAC level, not ABAC. The `auditor` role's `permissions[]`
contains only codenames whose `action` is `read` or `export`; there is no
`post`, `approve`, `issue` or `update` anywhere in it. The `{"read_only": true}`
parameter on the scope rules is belt-and-braces for the policy layer and makes
the intent greppable. Relying on ABAC alone here would be wrong: a scope rule
narrows *rows*, it does not forbid *verbs*, so `all` + `journal_entry.post`
would be a posting auditor.

**"An Accountant cannot approve the payroll run they calculated."**
This is enforced twice, at two different layers, because segregation of duties
is the control auditors test first.

1. **RBAC.** `payroll.payroll_run.approve` is simply not in the `accountant`
   role. An Accountant calculates (`calculate`), and posts and pays the run
   *after* approval (`post`, `pay`). The approval itself belongs to
   `hr_manager` and `admin`.
2. **ABAC.** Those roles carry `payroll_run` × `all` ×
   `{"exclude_self_prepared": true}`, which appends
   `~Q(calculated_by_id=<actor employee/user id>)`. So an HR Manager who
   pressed *Calculate* cannot press *Approve* on the same run either.

The residual case is a tenant with exactly one human. `owner` carries
`{"exclude_self_prepared": true, "break_glass": true}`: the approve succeeds,
and the service writes `TenantAuditLog` with
`action = TenantAuditLog.Action.PAYROLL_APPROVED` and `payload.break_glass =
true`. The control is not "impossible", it is "impossible to do quietly" —
which is the only version of the control that survives contact with a
five-person company.

---

## 6. Privilege-escalation guards

### 6.1 The rank rule

> A user may grant, revoke or modify a `RoleAssignment` only for roles whose
> `rank` is **strictly greater** than the minimum rank the actor holds in that
> tenant.

Implemented in `apps.iam.services.assignment.assign_role()` and asserted again
in the `HasPermission` check for `iam.membership.assign_role`.

```python
actor_rank = min(a.role.rank for a in actor_assignments if a.is_currently_valid)
if target_role.rank <= actor_rank:
    raise PermissionDenied("Cannot grant a role at or above your own rank.")
```

Three failures this prevents:

* **Self-promotion.** An Admin (10) cannot grant themselves Owner (0), and
  cannot grant Owner to a confederate.
* **Lateral escalation between peers.** Accountant and HR Manager are both
  rank 20, so `20 <= 20` fails and neither can grant the other. Without
  *strictly*, an HR Manager would grant themselves Accountant, and the payroll
  segregation of duties in §5.4 evaporates in one API call.
* **Ratchet via custom roles.** A custom role's rank is validated on creation
  by the same rule — an Admin cloning `owner` must give the clone a rank > 10,
  so they cannot manufacture an Owner-equivalent at rank 5.

`min()` and not `max()`: a user with both `admin` (10) and `employee` (50)
acts at rank 10. Taking the weakest assignment would let anyone de-escalate
themselves into a grant they are not entitled to make.

### 6.2 The last Owner cannot be revoked

`TenantMembership.is_owner` is documented in the model as *"Billing owner;
cannot be removed by others."* Two rules implement it:

1. **No self-revocation of the last Owner.** Before deleting a `RoleAssignment`
   for the `owner` role, or clearing `is_owner`, the service counts remaining
   owners inside the same transaction with `SELECT … FOR UPDATE`:

   ```python
   remaining = (TenantMembership.objects
                .select_for_update()
                .filter(tenant_id=tenant_id, is_owner=True, is_active=True)
                .exclude(pk=membership.pk).count())
   if remaining == 0:
       raise PermissionDenied("A tenant must always have at least one active owner.")
   ```

   The row lock is not decoration: two concurrent revocations both read
   `remaining == 1`, both pass a naive check, and both commit. The tenant is
   then **permanently unadministrable** — no one can grant a role, close a
   period, or re-invite an owner, and recovery requires a platform admin with
   `platform_admin_context()`, which is an incident.

2. **Ownership transfers, it does not vanish.**
   `iam.membership.transfer_ownership` is a single sensitive operation that
   sets the new owner and clears the old one in one transaction. There is no
   "remove owner" endpoint at all, so the dangerous state is not reachable
   through the API surface even before the count check runs.

An Owner may still *downgrade themselves* once a second Owner exists. The rule
is "the set of owners must never be empty", not "you are trapped".

### 6.3 Re-authentication for `is_sensitive` permissions

Any endpoint whose required permission has `is_sensitive = true` demands a
**fresh** authentication factor, independent of the JWT's validity:

* The client sends `X-Reauth-Token`, obtained from `POST /api/v1/auth/reauth`
  by re-presenting the password or a TOTP code from `User.mfa_secret`.
* The token is single-tenant, single-user, and valid for **5 minutes**
  (`REAUTH_TTL_SECONDS = 300`), held in Redis under
  `reauth:{tenant_id}:{user_id}:{jti}`.
* Missing or expired → `403` with error code `reauth_required`, so the client
  can prompt inline rather than logging the user out.
* Implemented by the `@require_reauth` decorator in `apps/iam/permissions.py`.

The failure it prevents is the stolen-session case, which is the realistic one.
An access token lifted from a laptop left unlocked, or from an XSS payload in a
third-party widget, is enough to read the books — that is the price of a
bearer token. It should not also be enough to approve a payroll run, rotate a
gateway signing secret, or reopen a closed period. Re-auth converts "holds a
token" into "is at the keyboard right now, with the second factor".

TOTP is required rather than password-only when `User.mfa_enabled` is true; a
password is replayable from the same XSS that took the token, a TOTP code is
not.

### 6.4 Guards that are not about rank

| Guard | Rule | Failure prevented |
| --- | --- | --- |
| System roles are immutable | `Role.is_system = True` rows reject `update`/`delete` at the service layer, matching `uq_role_system_code` | A product release adding a permission silently grants it to a customer-edited "Accountant". |
| API keys inherit, never exceed | `ApiKey.role` is `PROTECT`; the key's effective permissions are that role's, and creating a key for a role above the creator's rank fails the §6.1 check | An Admin minting an Owner-ranked machine key and using it to escalate. |
| Assignments expire | `RoleAssignment.valid_until` is checked per request via `is_currently_valid`, not only at grant time | The quarter-end auditor who still has access in March. |
| Membership deactivation is immediate | The refresh path re-reads `TenantMembership.is_active` rather than trusting the JWT claim | A fired employee whose 15-minute access token is still alive; refresh fails, and the effective-permission cache is invalidated on the membership write. |
| Platform admin is never implicit | `is_platform_admin` grants nothing inside a tenant unless code explicitly enters `platform_admin_context()`, which is audit-logged as `TenantAuditLog.Action.IMPERSONATION` | Support staff browsing customer books without a trace. |

---

## 7. Seeding and drift

`permissions.json` is applied by `python manage.py sync_permissions`, which:

1. Upserts every `Permission` row by `codename` (the PK).
2. **Fails loudly** on any codename present in the database but absent from the
   file, rather than deleting it — a deleted permission silently widens every
   view that referenced it, because `HasPermission` on an unknown codename must
   never degrade to "allow".
3. Rebuilds `RolePermission` and `ScopeRule` for system roles only, never
   touching `Role.tenant IS NOT NULL` rows.
4. Flushes every `perms:v1:{tenant_id}:{user_id}` cache key, because a role's
   permission set just changed underneath cached sets.

CI runs the same validation as the verification script in §8 of the API
contract: valid JSON, and every codename in `roles[].permissions` present in
`permissions[]`. A dangling codename in a role is a permission that silently
does nothing — the role looks correct in the admin UI and denies in production.
