# 06 — REST API contract

**Status:** binding. Every endpoint below names the `Permission.codename` it
requires and the `ScopeRule.strategy` that narrows its rows. Those names come
from `docs/05-permission-matrix.md` and `backend/config/permissions.json`; if
an endpoint here names a codename that is not in the catalogue, the endpoint
is the bug.

---

## 1. Conventions

### 1.1 Base path and versioning

```
https://{tenant}.app.example.com/api/v1/
https://api.example.com/api/v1/            # apex host + X-Tenant-ID
```

The version is in the path, not in a header. A URL is what appears in a log,
a bug report, a curl command and a customer's integration code; a version
negotiated in an `Accept` header is invisible in all five and produces support
tickets that cannot be reproduced.

`v1` is frozen on additive changes only. Removing a field, narrowing a type, or
tightening a validation is `v2`. Adding an optional field is not a breaking
change and clients must ignore unknown keys.

### 1.2 Tenant resolution — the JWT claim wins

Resolution order, implemented by `TenantResolutionMiddleware` in
`apps/iam/permissions.py`:

| Priority | Source | Trusted because |
| :---: | --- | --- |
| 1 | `tid` claim inside the access token | It is **signed**. The client cannot alter it without the signing key. |
| 2 | `X-Tenant-ID: <uuid>` header | Only consulted when the token carries no `tid` (API keys, the tenant-switch endpoint). If a `tid` claim exists and the header disagrees, the request is rejected with `400 tenant_unresolved` — never silently resolved to one of them. |
| 3 | `Host` — a verified `TenantDomain`, then the sub-domain label matched against `Tenant.slug` | Convenience for browser sessions. Lowest priority because `Host` is rewritten by corporate proxies and CDNs. |

**Why the claim and not the header.** If the header were consulted first, an
attacker holding a valid token for tenant A would send `X-Tenant-ID: <B>` and
the entire request — ORM manager, RLS session variable, permission cache key —
would run bound to tenant B. The only thing standing between them and B's
ledger would be the membership lookup, a single `if` statement. Trusting the
signed claim means the attack requires forging a signature.

**Why the header exists at all.** A browser cannot set an arbitrary sub-domain
on an XHR to an apex API host without CORS gymnastics, mobile clients have no
sub-domain at all, and the tenant-switch flow needs to name a tenant the
current token was not minted for. The header is the escape hatch, ranked last.

Membership is re-read from `TenantMembership` on **every** request rather than
trusted from the token. A signed claim is not authorisation: `is_active` flips
to `False` the moment someone is offboarded, and a 15-minute access token
minted before that must stop working now.

A caller who is not a member of the resolved tenant gets **404**, not 403.
Confirming that a workspace exists to a non-member discloses that a named
company is a customer.

### 1.3 Authentication headers

```http
Authorization: Bearer <access-jwt>          # 15 min, RS256
X-Tenant-ID: 6f1e…                          # only when the token is tenant-agnostic
X-Reauth-Token: <opaque>                    # required for is_sensitive permissions
Idempotency-Key: <client-uuid>              # required on money-moving POSTs
If-Match: "<etag>"                          # required on state transitions
X-Request-ID: <client-uuid>                 # echoed in every error envelope
```

| Credential | Lifetime | Rotation |
| --- | --- | --- |
| Access JWT | 15 min | none — it expires |
| Refresh token | 30 days, **rotating** | Every refresh returns a new refresh token and revokes the old one. Presenting a revoked refresh token revokes the entire family and forces re-login: that pattern means the token was replayed, i.e. stolen. |
| `X-Reauth-Token` | 5 min, **single use** | Issued by `POST /auth/reauth`. Consumed by the first sensitive call. A replayable re-auth token is just a longer session token. |
| API key (`ApiKey`) | until `expires_at` / `revoked_at` | `Authorization: ApiKey <prefix>.<secret>`. Only the hash is stored; the plaintext is shown once at creation. Its permissions are `ApiKey.role`'s and can never exceed the rank of the user who minted it. |

### 1.4 `Idempotency-Key`

**Required** on every POST that moves money or allocates a document number:
invoice issue, payment record, refund, payroll run approve/pay, bill post,
journal entry post, expense reimburse. Optional elsewhere; honoured if sent.

* Value: a client-generated UUIDv4, unique per logical operation, ≤ 128 chars.
* The server stores `(tenant_id, endpoint, key) → (status, response_body, request_hash)` for **24 hours**.
* **Replay with the same body** → the original response, byte for byte, with `Idempotency-Replayed: true`. The status code is the original status, including `201`.
* **Replay with a different body** → `409 duplicate_idempotency_key`. Reusing a key for a different payload is a client bug, and silently returning the first response would make the second, different, payment vanish without an error.
* **Concurrent replay while the first is in flight** → `409 idempotency_in_progress`, `Retry-After: 1`. The key row is inserted with `SELECT … FOR UPDATE` inside the same transaction as the business write, so the two cannot interleave.
* Missing on a required endpoint → `400 idempotency_key_required`.

The failure this prevents is not exotic. A mobile client on a train records a
payment, the response is lost to a dead cell, the user taps again, and the
customer's account is credited twice. Reconciling that costs more than the
payment. `JournalEntry.idempotency_key` and `uq_entry_idempotency` are the
database-level half of the same guard.

### 1.5 Pagination — cursor, not offset

```http
GET /api/v1/journal-entries?limit=100&cursor=eyJlIjoiMjAyNi0wMy0xMSIsImkiOiI5ZjNj…
```

```json
{
  "data": [ … ],
  "meta": {
    "limit": 100,
    "has_more": true,
    "next_cursor": "eyJlIjoiMjAyNi0wMy0xMSIsImkiOiI5ZjNj…",
    "prev_cursor": null
  }
}
```

The cursor is base64 of the ordering tuple of the last row — for the ledger,
`(entry_date, id)`. `limit` defaults to 50, maximum 200.

**Why not offset.** Two reasons, both of which bite on exactly the tables that
matter most here.

1. **Correctness.** `OFFSET 5000` re-evaluates the whole ordering on every
   page. A ledger is being written to while an accountant pages through it;
   a journal entry posted between page 50 and page 51 shifts every subsequent
   row by one, so one entry is shown twice and one is never shown at all. An
   export built by paging is then quietly missing a transaction, and the
   trial balance the customer reconciles against does not tie out. A cursor
   is anchored to a value, so an insert before the cursor is simply not in the
   result set — a well-defined, explainable snapshot.
2. **Cost.** `OFFSET n` makes PostgreSQL fetch and discard `n` rows. On a
   ten-million-line `accounting_journal_line`, page 40 000 is a sequential
   scan. `WHERE (entry_date, id) < (:d, :i) ORDER BY entry_date DESC, id DESC
   LIMIT 100` is an index range scan on `ix_entry_status` and costs the same
   for page 1 and page 40 000.

Offset paging is available **only** on endpoints with a hard-bounded row count
(`/fiscal-periods`, `/leave-types`, `/roles`) where the tradeoff does not
exist. Everything else is cursor-only, and `?page=` returns `400`.

### 1.6 Filtering and sorting grammar

```
?status=posted                     # equality
?status__in=posted,voided          # set membership
?entry_date__gte=2026-01-01        # range
?entry_date__lt=2026-04-01
?total_debit__gt=1000.00           # decimal as string
?memo__icontains=refund            # only on whitelisted text fields
?customer=6f1e…                    # FK by id
?is_archived=false
?sort=-entry_date,number           # comma list, leading '-' = DESC
```

Rules that are not negotiable:

* **Whitelist, never reflect.** Each viewset declares `filterset_fields` and
  `ordering_fields` explicitly. Passing user input to `.filter(**params)` lets
  a caller traverse relations (`?created_by__email__icontains=`) and turn a
  list endpoint into a user-enumeration oracle across the whole tenant.
* **Sorting is whitelisted for the same reason plus a different one:** an
  unindexed `ORDER BY` on a ten-million-row table is a denial of service that
  any authenticated user can trigger from the address bar.
* Every sort is stabilised with `, id` appended. Without a tiebreaker, two rows
  with the same `entry_date` can swap between pages and the cursor skips one.
* Unknown filter or sort keys → `400 validation_error`, never ignored. Silently
  ignoring `?status=postd` returns the full unfiltered ledger and looks like it
  worked.

### 1.7 Sparse fieldsets and expansion

```
?fields=id,number,total_debit,status         # top-level projection
?expand=lines,customer                       # inline related objects
?fields[lines]=account,debit,credit          # projection inside an expansion
```

`expand` is whitelisted per endpoint with a maximum depth of 2 and a maximum
of 3 expansions per request. Unbounded expansion is how a single innocuous
`GET /invoices?expand=customer.invoices.customer` becomes an exponential query.

Expanded objects are **still scope-filtered**. Expanding `invoice.project` for
a Department Manager whose `project` scope is `assigned_projects` returns
`null` for projects outside their scope, not the object. An expansion is a
different route to the same rows, so it goes through the same
`build_scope_q()`.

### 1.8 ETag / `If-Match` optimistic concurrency

Every detail representation carries an ETag derived from `(id, updated_at,
status)`:

```http
ETag: "a3f9c1e0-2026-03-11T09:14:02.115Z-draft"
```

`If-Match` is **required** on:

* every `PATCH`/`PUT` to a mutable document, and
* every state-transition sub-resource (`/issue`, `/void`, `/approve`, `/post`,
  `/reverse`, `/close`).

Missing → `428 precondition_required`. Stale → `412 concurrent_modification`.

The failure it prevents is the two-approver race. Two managers open the same
payroll run in two tabs. Both see `status = calculated`. Both click Approve.
Without `If-Match`, the second request re-reads the row, finds it already
approved, and either double-posts or throws an opaque 500. With it, the second
request carries an ETag containing `calculated` while the row now says
`approved`, and gets a clean `412` telling the client to reload. This is the
API-level twin of `JournalEntry.ALLOWED_TRANSITIONS`: the model refuses the
illegal transition, and the ETag makes the *client* see why.

`GET` supports `If-None-Match` → `304`, which matters for the mobile client
polling invoice status on a metered connection.

### 1.9 Money is always a JSON string

```json
{ "total_amount": "1234567890.123456", "currency": "EGP" }
```

Never `1234567890.123456` as a bare JSON number. Three reasons, in order of how
expensively each one fails:

1. **`JSON.parse` silently corrupts large decimals.** JSON numbers are parsed
   into IEEE-754 doubles by every mainstream client — browser, Node, Python's
   default `json.loads`, Go's `interface{}`. A double has 53 bits of mantissa,
   about 15–16 significant decimal digits. `MoneyField` is `numeric(19, 6)`:
   19 significant digits. `12345678901234.567890` round-trips through
   `JSON.parse` as `12345678901234.568`. No exception is raised. The number is
   simply wrong, and it is wrong in the direction of a broken trial balance.
2. **`0.1 + 0.2 !== 0.3`.** A client that sums line amounts to display a
   subtotal produces a figure that differs from the server's by 1e-17. In a
   currency field that renders as a mismatched total the user reports as a
   bug, and in a posting payload it is a
   `ck_entry_balanced` constraint violation that rolls back the transaction.
3. **Trailing-zero significance is lost.** `1.50` and `1.5` are the same
   double. For a KWD amount (3 minor units) or a unit price of `0.000125`, the
   scale carries meaning that a double discards.

The server parses inbound amounts with `Decimal` (`json.loads(..., parse_float=Decimal)`
then `apps.core.fields.to_money`, which **raises** on a `float` — a float
reaching that function means a JSON number was parsed without the hook, which
is a programming error, not a user error). It renders outbound amounts as
strings at full `numeric(19,6)` scale.

`currency` accompanies every amount, ISO-4217 alpha-3 from `Currency.choices`.
An amount without a currency is not a monetary value.

Dates are ISO-8601 (`2026-03-11`); timestamps are ISO-8601 UTC with `Z`
(`2026-03-11T09:14:02.115Z`). Never a Unix epoch: a leap-second-adjacent epoch
integer and a naive local timestamp are indistinguishable in a payload, and
payroll periods are defined in the tenant's `timezone`.

---

## 2. Error envelope

Every non-2xx response, without exception, is this shape. A client that has to
branch on "is this the DRF default shape or ours" will get it wrong.

```json
{
  "error": {
    "code": "unbalanced_entry",
    "message": "Journal entry does not balance.",
    "detail": "Debits total 1500.000000, credits total 1450.000000.",
    "field_errors": {
      "lines[2].credit": ["Must be 50.000000 to balance the entry."]
    },
    "request_id": "0a7f1c2e-9b44-4d0e-b1f0-1b0f6b0f8a11",
    "documentation_url": "https://docs.example.com/errors/unbalanced_entry"
  }
}
```

`code` is a stable machine-readable string and is the only field a client may
branch on. `message` is human-readable and may be reworded or localised at any
time; treating it as an identifier is how integrations break on a copy edit.
`request_id` echoes `X-Request-ID`, or is generated, and appears in the server
log — it is the first thing support asks for.

| `code` | HTTP | Meaning and the client's correct response |
| --- | :---: | --- |
| `validation_error` | 400 | Malformed or invalid input; see `field_errors`. Fix and retry. |
| `idempotency_key_required` | 400 | A money-moving POST arrived without `Idempotency-Key`. |
| `tenant_unresolved` | 400 | No tenant could be determined, or `X-Tenant-ID` contradicted the token's `tid`. |
| `unauthenticated` | 401 | Missing/expired access token. Refresh, then retry once. |
| `invalid_token` | 401 | Signature or `iss`/`aud` mismatch. Do not retry; re-login. |
| `reauth_required` | 403 | The permission is `is_sensitive`. Prompt for password/TOTP, call `POST /auth/reauth`, retry with `X-Reauth-Token`. |
| `permission_denied` | 403 | RBAC refusal. The actor's role lacks the codename. Not retryable. |
| `tenant_suspended` | 403 | `Tenant.status` is `suspended`/`closed`. Reads still succeed; writes do not. |
| `approval_limit_exceeded` | 403 | Amount exceeds the `max_amount` in the actor's `ScopeRule.parameters`. Escalate to a higher role. |
| `segregation_of_duties` | 403 | `exclude_self_prepared`: you prepared this document, so you may not approve it. |
| `not_found` | 404 | The row does not exist **or** is outside the actor's ABAC scope. The two are deliberately indistinguishable. |
| `method_not_allowed` | 405 | Frequently means you `PATCH`ed `status`; use the transition sub-resource (§3.1). |
| `period_closed` | 409 | The target `FiscalPeriod` is `closed`/`soft_closed`. Post to an open period or reverse in the current one. |
| `unbalanced_entry` | 409 | `SUM(debit) != SUM(credit)`. `ck_entry_balanced` would have refused it. |
| `illegal_transition` | 409 | Not in `ALLOWED_TRANSITIONS` — e.g. voiding an already-voided entry. |
| `insufficient_stock` | 409 | Not enough on hand at the warehouse for the movement. |
| `duplicate_idempotency_key` | 409 | Same key, different body. |
| `idempotency_in_progress` | 409 | Same key, first request still running. `Retry-After: 1`. |
| `concurrent_modification` | 409 | Reserved for conflicts detected without `If-Match` (e.g. a service-layer version clash). |
| `sequence_exhausted` | 409 | `DocumentSequence` could not allocate; almost always a locking timeout. Retry. |
| `precondition_required` | 428 | `If-Match` is mandatory on this route and was absent. |
| `precondition_failed` / `concurrent_modification` | 412 | `If-Match` ETag is stale. Reload and re-present. |
| `payload_too_large` | 413 | Body or upload over the limit (10 MB JSON, 25 MB file). |
| `unsupported_media_type` | 415 | Send `application/json` or `multipart/form-data`. |
| `rate_limited` | 429 | See §7. Honour `Retry-After`. |
| `gateway_error` | 502 | The payment gateway failed or timed out. **Do not** retry without the same `Idempotency-Key` — the charge may have succeeded. |
| `internal_error` | 500 | Report `request_id`. |
| `service_unavailable` | 503 | Maintenance or shedding. `Retry-After` present. |

Two codes deserve their reasoning stated. `not_found` covers "outside your
scope" because a `403` on an existing row is an enumeration oracle: it confirms
that invoice `9f3c…` exists in this tenant. And `gateway_error` is a `502` with
an explicit do-not-blind-retry note because the single most expensive class of
production bug in a payments integration is a client that retries a timeout
without an idempotency key and charges the customer twice.

---

## 3. Endpoints

Notation: **Perm** is the `Permission.codename` from
`docs/05-permission-matrix.md`. **Scope** is the `ScopeRule.resource` that
`ScopedQuerysetMixin` applies — the actual strategy depends on the caller's
role (see the matrix §5). **Idem** means `Idempotency-Key` is required.

Standard CRUD shape, unless stated otherwise:

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/{collection}` | Cursor-paginated list |
| `POST` | `/{collection}` | Create |
| `GET` | `/{collection}/{id}` | Detail, returns `ETag` |
| `PATCH` | `/{collection}/{id}` | Partial update, requires `If-Match` |
| `DELETE` | `/{collection}/{id}` | Only where the model permits it; otherwise 405 |

### 3.1 Why `status` is not a writable field

Every state change is a **sub-resource action** — `POST /invoices/{id}/issue`,
`POST /invoices/{id}/void`, `POST /payroll-runs/{id}/approve` — and never
`PATCH {"status": "issued"}`.

Exposing `status` as a writable field is an authorisation hole, not a style
preference, and it fails in four separate ways at once. **First, it collapses
distinct permissions into one.** `PATCH /invoices/{id}` requires
`sales.invoice.update`, an everyday grant. Issuing an invoice requires
`sales.invoice.issue`, which is `is_sensitive`, allocates a gapless number from
`DocumentSequence`, and posts to the ledger. If `status` is in the serialiser,
anyone holding `update` performs `issue`, and the permission catalogue becomes
decorative — the guard is on the verb, but the verb no longer distinguishes the
actions. **Second, it destroys the audit answer.** `TenantAuditLog` needs to
record *"user X approved payroll run Y"*, mapped to
`TenantAuditLog.Action.PAYROLL_APPROVED`. A generic PATCH records "row updated"
with a JSON diff, and six months later nobody can prove who released the money.
**Third, it makes the transition graph unenforceable at the edge.**
`JournalEntry.ALLOWED_TRANSITIONS` and `transition()` exist precisely so that
`voided → posted` is impossible; a PATCH bypasses the method and assigns the
field, which is exactly what CONVENTIONS §4 forbids ("Never assign `.status =`
directly from a view"). **Fourth, transitions are not field writes.** Issuing an
invoice allocates a number under a row lock, builds a `JournalEntryDraft`, calls
`post_entry()`, and checks the period is open — a transaction with four
failure modes that each need their own error code (`period_closed`,
`unbalanced_entry`, `sequence_exhausted`, `illegal_transition`). A PATCH
handler has one place to put all of that, and it turns into a 500.

Sub-resource actions also give each transition its own rate-limit bucket, its
own `Idempotency-Key` requirement, and its own re-auth prompt. `status` is
therefore **read-only in every serialiser in the codebase**, and a serialiser
test asserts it.

### 3.2 Accounting

| Method | Path | Description | Perm | Scope | Idem |
| --- | --- | --- | --- | --- | :---: |
| GET | `/accounts` | Chart of accounts (tree via `?expand=children`) | `accounting.account.read` | — | — |
| POST | `/accounts` | Create an account | `accounting.account.create` | — | — |
| PATCH | `/accounts/{id}` | Rename / re-parent | `accounting.account.update` | — | — |
| POST | `/accounts/{id}/archive` | Deactivate; refuses if `cached_balance != 0` | `accounting.account.archive` | — | — |
| GET | `/accounts/{id}/ledger` | Account ledger with running balance | `accounting.account.read` | — | — |
| GET | `/journals` | Books of original entry | `accounting.journal.read` | — | — |
| POST | `/journals` | Create a journal | `accounting.journal.manage` | — | — |
| GET | `/journal-entries` | List entries | `accounting.journal_entry.read` | `journal_entry` | — |
| POST | `/journal-entries` | Create a **draft** entry | `accounting.journal_entry.create` | `journal_entry` | — |
| PATCH | `/journal-entries/{id}` | Edit a draft only | `accounting.journal_entry.update` | `journal_entry` | — |
| POST | `/journal-entries/{id}/post` | Post to the ledger | `accounting.journal_entry.post` | `journal_entry` | **yes** |
| POST | `/journal-entries/{id}/void` | Void within an open period | `accounting.journal_entry.void` | `journal_entry` | **yes** |
| POST | `/journal-entries/{id}/reverse` | Create the mirror entry | `accounting.journal_entry.reverse` | `journal_entry` | **yes** |
| GET | `/fiscal-years` | Fiscal years (offset paging) | `accounting.fiscal_period.read` | — | — |
| POST | `/fiscal-years/{id}/periods` | Generate the year's periods | `accounting.fiscal_period.create` | — | — |
| GET | `/fiscal-periods` | Periods and statuses | `accounting.fiscal_period.read` | — | — |
| POST | `/fiscal-periods/{id}/soft-close` | Stop operational posting | `accounting.fiscal_period.close` | — | **yes** |
| POST | `/fiscal-periods/{id}/close` | Lock the period | `accounting.fiscal_period.close` | — | **yes** |
| POST | `/fiscal-periods/{id}/reopen` | Break-glass reopen | `accounting.fiscal_period.reopen` | — | **yes** |
| GET | `/tax-rates` | VAT / sales-tax definitions | `accounting.tax_rate.read` | — | — |
| POST/PATCH | `/tax-rates[/{id}]` | Create / amend | `accounting.tax_rate.manage` | — | — |
| GET | `/exchange-rates` | FX table | `accounting.exchange_rate.read` | — | — |
| POST | `/exchange-rates` | Enter a rate | `accounting.exchange_rate.manage` | — | — |

Posting into a `soft_closed` period additionally requires
`accounting.period.post_to_soft_closed`; without it the response is
`409 period_closed`.

### 3.3 Sales

| Method | Path | Description | Perm | Scope | Idem |
| --- | --- | --- | --- | --- | :---: |
| GET | `/customers` | List customers | `sales.customer.read` | `customer` | — |
| POST | `/customers` | Create | `sales.customer.create` | `customer` | — |
| PATCH | `/customers/{id}` | Update terms / credit limit | `sales.customer.update` | `customer` | — |
| POST | `/customers/{id}/archive` | Archive | `sales.customer.archive` | `customer` | — |
| GET | `/customers/{id}/statement` | Customer statement | `sales.customer.read` | `customer` | — |
| GET | `/invoices` | List invoices | `sales.invoice.read` | `invoice` | — |
| POST | `/invoices` | Create a **draft** | `sales.invoice.create` | `invoice` | — |
| GET | `/invoices/{id}` | Detail (`ETag`) | `sales.invoice.read` | `invoice` | — |
| PATCH | `/invoices/{id}` | Edit a draft | `sales.invoice.update` | `invoice` | — |
| POST | `/invoices/{id}/issue` | Allocate number, post to GL | `sales.invoice.issue` | `invoice` | **yes** |
| POST | `/invoices/{id}/send` | Email / e-invoice | `sales.invoice.send` | `invoice` | **yes** |
| POST | `/invoices/{id}/void` | Void and reverse | `sales.invoice.void` | `invoice` | **yes** |
| POST | `/invoices/{id}/write-off` | Bad-debt write-off | `sales.invoice.write_off` | `invoice` | **yes** |
| GET | `/invoices/{id}/pdf` | Rendered PDF | `sales.invoice.read` | `invoice` | — |
| GET | `/credit-notes` | List | `sales.credit_note.read` | `credit_note` | — |
| POST | `/credit-notes` | Create a draft | `sales.credit_note.create` | `credit_note` | — |
| POST | `/credit-notes/{id}/issue` | Issue and post | `sales.credit_note.issue` | `credit_note` | **yes** |
| POST | `/credit-notes/{id}/apply` | Apply to an invoice | `sales.credit_note.apply` | `credit_note` | **yes** |
| GET | `/recurring-profiles` | List | `sales.recurring_profile.read` | — | — |
| POST/PATCH | `/recurring-profiles[/{id}]` | Create / edit / pause | `sales.recurring_profile.manage` | — | — |
| POST | `/recurring-profiles/{id}/run` | Generate the next invoice now | `sales.recurring_profile.run_now` | — | **yes** |

### 3.4 Payments and banking

| Method | Path | Description | Perm | Scope | Idem |
| --- | --- | --- | --- | --- | :---: |
| GET | `/payments` | List payments | `banking.payment.read` | `payment` | — |
| POST | `/payments` | Record a receipt or a payment | `banking.payment.create` | `payment` | **yes** |
| GET | `/payments/{id}` | Detail | `banking.payment.read` | `payment` | — |
| POST | `/payments/{id}/allocate` | Allocate across documents | `banking.payment.allocate` | `payment` | **yes** |
| POST | `/payments/{id}/void` | Void and reverse | `banking.payment.void` | `payment` | **yes** |
| GET | `/refunds` | List refunds | `banking.refund.read` | `refund` | — |
| POST | `/refunds` | Refund to the original method | `banking.refund.create` | `refund` | **yes** |
| POST | `/refunds/{id}/approve` | Approve above the limit | `banking.refund.approve` | `refund` | **yes** |
| GET | `/bank-accounts` | List | `banking.bank_account.read` | — | — |
| POST/PATCH | `/bank-accounts[/{id}]` | Create / edit | `banking.bank_account.create` / `.update` | — | — |
| POST | `/bank-accounts/{id}/statements` | Import a statement (CSV/OFX/MT940) | `banking.statement.import` | — | **yes** |
| GET | `/statements/{id}/lines` | Statement lines | `banking.statement.read` | — | — |
| POST | `/reconciliations` | Open a session | `banking.reconciliation.create` | — | — |
| POST | `/reconciliations/{id}/match` | Match / unmatch a line | `banking.reconciliation.match` | — | — |
| POST | `/reconciliations/{id}/complete` | Finalise and lock | `banking.reconciliation.complete` | — | **yes** |
| GET | `/gateways` | Gateway configuration (never the secret) | `banking.gateway_config.read` | — | — |
| POST/PATCH | `/gateways[/{id}]` | Connect / configure | `banking.gateway_config.manage` | — | — |
| POST | `/gateways/{id}/rotate-secret` | Rotate the webhook signing secret | `banking.gateway_config.rotate_secret` | — | **yes** |
| GET | `/webhook-events` | Received gateway events | `banking.webhook_event.read` | — | — |
| POST | `/webhook-events/{id}/replay` | Re-run the handler | `banking.webhook_event.replay` | — | **yes** |

### 3.5 Expenses and purchasing

| Method | Path | Description | Perm | Scope | Idem |
| --- | --- | --- | --- | --- | :---: |
| GET | `/expenses` | List claims | `purchasing.expense.read` | `expense` | — |
| POST | `/expenses` | Create a claim | `purchasing.expense.create` | `expense` | — |
| PATCH | `/expenses/{id}` | Edit a draft claim | `purchasing.expense.update` | `expense` | — |
| POST | `/expenses/{id}/submit` | Submit for approval | `purchasing.expense.submit` | `expense` | — |
| POST | `/expenses/{id}/approve` | Approve (honours `max_amount`) | `purchasing.expense.approve` | `expense` | **yes** |
| POST | `/expenses/{id}/reject` | Reject with a reason | `purchasing.expense.reject` | `expense` | — |
| POST | `/expenses/{id}/reimburse` | Pay and post | `purchasing.expense.reimburse` | `expense` | **yes** |
| GET | `/bills` | List vendor bills | `purchasing.bill.read` | `bill` | — |
| POST | `/bills` | Enter a bill | `purchasing.bill.create` | `bill` | — |
| POST | `/bills/{id}/approve` | Approve for payment | `purchasing.bill.approve` | `bill` | **yes** |
| POST | `/bills/{id}/post` | Post to accounts payable | `purchasing.bill.post` | `bill` | **yes** |
| POST | `/bills/{id}/void` | Void and reverse | `purchasing.bill.void` | `bill` | **yes** |
| GET | `/vendors` | List vendors | `purchasing.vendor.read` | `vendor` | — |
| POST | `/vendors` | Create | `purchasing.vendor.create` | `vendor` | — |
| PATCH | `/vendors/{id}` | Edit (bank details are `is_sensitive`) | `purchasing.vendor.update` | `vendor` | — |
| GET | `/expense-categories` | Categories | `purchasing.category.read` | — | — |
| POST/PATCH | `/expense-categories[/{id}]` | Create / edit | `purchasing.category.manage` | — | — |

### 3.6 Inventory

| Method | Path | Description | Perm | Scope | Idem |
| --- | --- | --- | --- | --- | :---: |
| GET | `/items` | List items and stock on hand | `inventory.item.read` | `item` | — |
| POST | `/items` | Create | `inventory.item.create` | `item` | — |
| PATCH | `/items/{id}` | Edit | `inventory.item.update` | `item` | — |
| POST | `/items/{id}/cost` | Override standard/average cost | `inventory.item.update_cost` | `item` | **yes** |
| GET | `/items/{id}/stock` | Per-warehouse quantities | `inventory.item.read` | `item` | — |
| GET | `/warehouses` | List | `inventory.warehouse.read` | — | — |
| POST/PATCH | `/warehouses[/{id}]` | Create / edit | `inventory.warehouse.manage` | — | — |
| GET | `/stock-movements` | Movement history | `inventory.stock_movement.read` | `stock_movement` | — |
| POST | `/stock-movements` | Receipt / issue / transfer | `inventory.stock_movement.create` | `stock_movement` | **yes** |
| POST | `/stock-movements/{id}/post` | Post the inventory journal entry | `inventory.stock_movement.post` | `stock_movement` | **yes** |
| GET | `/adjustments` | List adjustments | `inventory.adjustment.read` | `adjustment` | — |
| POST | `/adjustments` | Raise a count variance / write-off | `inventory.adjustment.create` | `adjustment` | — |
| POST | `/adjustments/{id}/approve` | Approve and post | `inventory.adjustment.approve` | `adjustment` | **yes** |
| GET | `/price-lists` | List | `inventory.price_list.read` | — | — |
| POST/PATCH | `/price-lists[/{id}]` | Create / edit | `inventory.price_list.manage` | — | — |

`POST /stock-movements` that would drive on-hand negative returns
`409 insufficient_stock` with the shortfall in `detail`. Whether negative stock
is permitted is a per-tenant setting; the error code is the same either way so
clients need one branch.

### 3.7 Projects

| Method | Path | Description | Perm | Scope | Idem |
| --- | --- | --- | --- | --- | :---: |
| GET | `/projects` | List projects | `projects.project.read` | `project` | — |
| POST | `/projects` | Create | `projects.project.create` | `project` | — |
| PATCH | `/projects/{id}` | Edit budget / details | `projects.project.update` | `project` | — |
| POST | `/projects/{id}/close` | Close to further time and cost | `projects.project.close` | `project` | **yes** |
| GET | `/projects/{id}/profitability` | Revenue vs cost | `reporting.profit_loss.read` | `project` | — |
| GET | `/tasks` | List tasks | `projects.task.read` | `task` | — |
| POST | `/tasks` | Create | `projects.task.create` | `task` | — |
| POST | `/tasks/{id}/assign` | Assign to a member | `projects.task.assign` | `task` | — |
| GET | `/timesheet-entries` | List | `projects.timesheet_entry.read` | `timesheet_entry` | — |
| POST | `/timesheet-entries` | Log time | `projects.timesheet_entry.create` | `timesheet_entry` | — |
| POST | `/timesheet-entries/{id}/submit` | Submit for approval | `projects.timesheet_entry.submit` | `timesheet_entry` | — |
| POST | `/timesheet-entries/{id}/approve` | Approve, making it billable | `projects.timesheet_entry.approve` | `timesheet_entry` | **yes** |

### 3.8 HR

| Method | Path | Description | Perm | Scope | Idem |
| --- | --- | --- | --- | --- | :---: |
| GET | `/employees` | List employees (no compensation) | `hr.employee.read` | `employee` | — |
| POST | `/employees` | Create | `hr.employee.create` | `employee` | — |
| PATCH | `/employees/{id}` | Edit personal / job details | `hr.employee.update` | `employee` | — |
| GET | `/employees/{id}/compensation` | Salary, bank details, history | `hr.employee.read_compensation` | `employee` | — |
| POST | `/employees/{id}/terminate` | Terminate + final settlement | `hr.employee.terminate` | `employee` | **yes** |
| POST | `/employees/export` | Export employee data (PII) | `hr.employee.export` | `employee` | — |
| GET | `/departments` | Org chart (`?expand=children`) | `hr.department.read` | `department` | — |
| POST | `/departments` | Create a node | `hr.department.create` | `department` | — |
| PATCH | `/departments/{id}` | Rename / re-parent (rewrites `path`) | `hr.department.update` | `department` | — |
| GET | `/employees/{id}/documents` | Documents | `hr.document.read` | `document` | — |
| POST | `/employees/{id}/documents` | Upload (multipart) | `hr.document.manage` | `document` | — |
| GET | `/attendance` | Attendance records | `hr.attendance.read` | `attendance` | — |
| POST | `/attendance` | Clock in/out or manual row | `hr.attendance.create` | `attendance` | — |
| POST | `/attendance/{id}/approve` | Approve overtime / correction | `hr.attendance.approve` | `attendance` | — |
| GET | `/leave-requests` | List | `hr.leave_request.read` | `leave_request` | — |
| POST | `/leave-requests` | Request leave | `hr.leave_request.create` | `leave_request` | — |
| POST | `/leave-requests/{id}/approve` | Approve, consume balance | `hr.leave_request.approve` | `leave_request` | **yes** |
| POST | `/leave-requests/{id}/reject` | Reject with a reason | `hr.leave_request.reject` | `leave_request` | — |
| POST | `/leave-requests/{id}/cancel` | Cancel, restore balance | `hr.leave_request.cancel` | `leave_request` | **yes** |
| GET | `/leave-types` | Types and accrual policy | `hr.leave_type.read` | — | — |
| POST/PATCH | `/leave-types[/{id}]` | Create / edit | `hr.leave_type.manage` | — | — |
| GET | `/leave-balances` | Balances | `hr.leave_balance.read` | `leave_balance` | — |
| POST | `/leave-balances/{id}/adjust` | Manual adjustment | `hr.leave_balance.adjust` | `leave_balance` | **yes** |

`GET /employees` never serialises compensation fields, whatever the caller's
role. Salary lives behind its own sub-resource and its own `is_sensitive`
permission, so a widened list serialiser cannot leak it by accident.

### 3.9 Payroll

| Method | Path | Description | Perm | Scope | Idem |
| --- | --- | --- | --- | --- | :---: |
| GET | `/payroll/components` | Earnings, deductions, benefits | `payroll.component.read` | — | — |
| POST/PATCH | `/payroll/components[/{id}]` | Create / edit a formula | `payroll.component.manage` | — | — |
| GET | `/payroll-runs` | List runs | `payroll.payroll_run.read` | `payroll_run` | — |
| POST | `/payroll-runs` | Open a run for a pay period | `payroll.payroll_run.create` | `payroll_run` | — |
| POST | `/payroll-runs/{id}/calculate` | Gross-to-net (async) | `payroll.payroll_run.calculate` | `payroll_run` | **yes** |
| POST | `/payroll-runs/{id}/approve` | Approve; SoD-guarded | `payroll.payroll_run.approve` | `payroll_run` | **yes** |
| POST | `/payroll-runs/{id}/post` | Post to the ledger | `payroll.payroll_run.post` | `payroll_run` | **yes** |
| POST | `/payroll-runs/{id}/pay` | Generate the bank file | `payroll.payroll_run.pay` | `payroll_run` | **yes** |
| POST | `/payroll-runs/{id}/void` | Void and reverse | `payroll.payroll_run.void` | `payroll_run` | **yes** |
| GET | `/payslips` | List payslips | `payroll.payslip.read` | `payslip` | — |
| GET | `/payslips/{id}` | Detail | `payroll.payslip.read` | `payslip` | — |
| GET | `/payslips/{id}/pdf` | Rendered payslip | `payroll.payslip.read` | `payslip` | — |
| POST | `/payroll-runs/{id}/publish` | Publish slips to self-service | `payroll.payslip.publish` | `payslip` | **yes** |
| POST | `/payslips/export` | Bank transfer / register file | `payroll.payslip.export` | `payslip` | — |
| GET | `/payroll/tax-brackets` | Brackets and ceilings | `payroll.tax_bracket.read` | — | — |
| POST/PATCH | `/payroll/tax-brackets[/{id}]` | Edit statutory tables | `payroll.tax_bracket.manage` | — | — |

`GET /payslips` for an Employee returns exactly their own slips. There is no
`?employee=` filter that widens that — the filter is applied *after*
`build_scope_q()`, so passing someone else's id returns an empty list rather
than a 403 (which would confirm the employee exists).

### 3.10 Reporting

All reports accept `?from=`, `?to=`, `?period=`, `?currency=`, `?compare=`,
`?department=`, `?project=`, and `?format=json|csv|xlsx|pdf`. Non-JSON formats
additionally require `reporting.report.export`.

| Method | Path | Description | Perm |
| --- | --- | --- | --- |
| GET | `/reports/profit-loss` | P&L for a period | `reporting.profit_loss.read` |
| GET | `/reports/balance-sheet` | Balance sheet as at a date | `reporting.balance_sheet.read` |
| GET | `/reports/trial-balance` | Trial balance | `reporting.trial_balance.read` |
| GET | `/reports/cash-flow` | Cash flow statement | `reporting.cash_flow.read` |
| GET | `/reports/tax-summary` | VAT/tax for a filing period | `reporting.tax_summary.read` |
| GET | `/reports/aging?type=ar\|ap` | Receivable / payable ageing | `reporting.aging.read` |
| GET | `/reports/payroll-register` | Payroll register | `reporting.payroll_register.read` |
| POST | `/reports/{slug}/export` | Async export → job id | `reporting.report.export` |
| GET | `/exports/{job_id}` | Job status + signed download URL | *(the requester's own job)* |

Report figures are always aggregated from `JournalLine`, never from
`Account.cached_balance` — the cache exists so the dashboard does not aggregate
ten million rows per page load, and the model docstring is explicit that
reports must not trust it.

### 3.11 Settings and IAM

| Method | Path | Description | Perm | Idem |
| --- | --- | --- | --- | :---: |
| GET | `/settings/organisation` | Profile, fiscal year, base currency | `settings.organisation.read` | — |
| PATCH | `/settings/organisation` | Edit profile / tax registration | `settings.organisation.update` | — |
| GET/PATCH | `/settings/branding` | Templates, logo, email footers | `settings.branding.read` / `.manage` | — |
| GET/PATCH | `/settings/sequences` | Document numbering | `settings.sequence.read` / `.manage` | — |
| GET/POST | `/settings/integrations` | Third-party connections | `settings.integration.read` / `.manage` | — |
| GET/PATCH | `/settings/notifications` | Reminder rules | `settings.notification.read` / `.manage` | — |
| GET | `/audit-log` | `TenantAuditLog`, cursor-paginated | `settings.audit_log.read` | — |
| POST | `/audit-log/export` | Export the audit log | `settings.audit_log.export` | — |
| POST | `/exports/tenant` | Full-tenant data export | `settings.export.create` | **yes** |
| GET | `/users` | Members of this tenant | `iam.user.read` | — |
| POST | `/users/invite` | Invite a user | `iam.user.invite` | — |
| POST | `/memberships/{id}/deactivate` | Revoke access immediately | `iam.user.deactivate` | **yes** |
| POST | `/users/{id}/reset-password` | Force reset / MFA re-enrol | `iam.user.reset_password` | **yes** |
| GET | `/roles` | Roles and their permissions | `iam.role.read` | — |
| POST | `/roles` | Clone a system role | `iam.role.create` | — |
| PATCH | `/roles/{id}` | Edit a **custom** role only | `iam.role.update` | — |
| GET | `/memberships` | Memberships + assignments | `iam.membership.read` | — |
| POST | `/memberships/{id}/roles` | Assign a role (rank rule applies) | `iam.membership.assign_role` | **yes** |
| DELETE | `/memberships/{id}/roles/{assignment_id}` | Revoke an assignment | `iam.membership.revoke_role` | — |
| POST | `/memberships/{id}/transfer-ownership` | Transfer ownership | `iam.membership.transfer_ownership` | **yes** |
| GET | `/api-keys` | Keys by prefix + last used | `iam.api_key.read` | — |
| POST | `/api-keys` | Create (plaintext shown once) | `iam.api_key.create` | **yes** |
| POST | `/api-keys/{id}/revoke` | Revoke immediately | `iam.api_key.revoke` | **yes** |
| GET | `/permissions` | The catalogue (static) | *(authenticated)* | — |
| GET | `/me` | Profile, memberships, effective permissions | *(authenticated)* | — |
| POST | `/auth/reauth` | Exchange password/TOTP for `X-Reauth-Token` | *(authenticated)* | — |
| POST | `/auth/switch-tenant` | Mint a token for another membership | *(authenticated)* | — |

`POST /memberships/{id}/roles` enforces the rank rule: the target role's `rank`
must be **strictly greater** than the actor's minimum rank, or the response is
`403 permission_denied`. Revoking the last Owner is refused with
`409 illegal_transition` — see permission matrix §6.2.

---

## 4. Worked examples

All amounts are JSON strings (§1.9).

### 4.1 Create an invoice

```http
POST /api/v1/invoices HTTP/1.1
Authorization: Bearer eyJhbGciOiJSUzI1NiIs…
X-Tenant-ID: 6f1e9c34-6f2a-4a5e-8f1b-3e0a9d1c7b22
Content-Type: application/json
X-Request-ID: 0a7f1c2e-9b44-4d0e-b1f0-1b0f6b0f8a11
```
```json
{
  "customer": "b21c4f88-1d2e-4a90-9c3f-77e1a2b4c6d0",
  "issue_date": "2026-03-11",
  "due_date": "2026-04-10",
  "currency": "EGP",
  "exchange_rate": "1.000000",
  "project": "3a5e77b1-0c2d-4e6f-8a91-2b3c4d5e6f70",
  "reference": "PO-4471",
  "notes": "Q1 platform subscription.",
  "lines": [
    {
      "item": "9c1d2e3f-4a5b-4c6d-8e7f-0a1b2c3d4e5f",
      "description": "Platform licence — 40 seats",
      "quantity": "40.000000",
      "unit_price": "1250.000000",
      "discount_rate": "0.050000",
      "tax_rate": "5d6e7f80-1a2b-4c3d-9e8f-7a6b5c4d3e2f"
    },
    {
      "description": "Implementation services",
      "quantity": "12.500000",
      "unit_price": "900.000000",
      "tax_rate": "5d6e7f80-1a2b-4c3d-9e8f-7a6b5c4d3e2f"
    }
  ]
}
```

`201 Created`

```http
ETag: "7e2b91a4-2026-03-11T09:14:02.115Z-draft"
Location: /api/v1/invoices/7e2b91a4-3c5d-4e6f-9a8b-1c2d3e4f5a6b
```
```json
{
  "data": {
    "id": "7e2b91a4-3c5d-4e6f-9a8b-1c2d3e4f5a6b",
    "number": null,
    "status": "draft",
    "customer": {
      "id": "b21c4f88-1d2e-4a90-9c3f-77e1a2b4c6d0",
      "name": "Nile Logistics S.A.E."
    },
    "issue_date": "2026-03-11",
    "due_date": "2026-04-10",
    "currency": "EGP",
    "exchange_rate": "1.000000",
    "subtotal_amount": "58750.000000",
    "discount_amount": "2500.000000",
    "tax_amount": "8225.000000",
    "total_amount": "66975.000000",
    "paid_amount": "0.000000",
    "balance_amount": "66975.000000",
    "lines": [
      {
        "id": "1f2e3d4c-5b6a-4978-8695-a4b3c2d1e0f9",
        "line_number": 1,
        "description": "Platform licence — 40 seats",
        "quantity": "40.000000",
        "unit_price": "1250.000000",
        "discount_rate": "0.050000",
        "line_subtotal": "47500.000000",
        "tax_amount": "6650.000000",
        "line_total": "54150.000000"
      },
      {
        "id": "2a3b4c5d-6e7f-4081-9203-b4c5d6e7f809",
        "line_number": 2,
        "description": "Implementation services",
        "quantity": "12.500000",
        "unit_price": "900.000000",
        "discount_rate": "0.000000",
        "line_subtotal": "11250.000000",
        "tax_amount": "1575.000000",
        "line_total": "12825.000000"
      }
    ],
    "journal_entry": null,
    "created_at": "2026-03-11T09:14:02.115Z",
    "updated_at": "2026-03-11T09:14:02.115Z"
  }
}
```

`number` is `null` and `journal_entry` is `null` on purpose. A draft has no
number, because an abandoned draft that burned a number leaves a gap in the
sequence, and tax authorities in Egypt, KSA and the EU treat gaps as prima
facie evidence of deleted invoices (see `models_sequence.py`).

### 4.2 Issue the invoice

```http
POST /api/v1/invoices/7e2b91a4-3c5d-4e6f-9a8b-1c2d3e4f5a6b/issue HTTP/1.1
Authorization: Bearer eyJhbGciOiJSUzI1NiIs…
If-Match: "7e2b91a4-2026-03-11T09:14:02.115Z-draft"
Idempotency-Key: 5f4e3d2c-1b0a-4998-8776-6554433221100
X-Reauth-Token: 9d8c7b6a5f4e3d2c1b0a99887766554433
Content-Type: application/json
```
```json
{ "send_email": false, "posting_date": "2026-03-11" }
```

`200 OK`

```http
ETag: "7e2b91a4-2026-03-11T09:16:41.902Z-issued"
```
```json
{
  "data": {
    "id": "7e2b91a4-3c5d-4e6f-9a8b-1c2d3e4f5a6b",
    "number": "INV-2026-000042",
    "status": "issued",
    "issued_at": "2026-03-11T09:16:41.902Z",
    "total_amount": "66975.000000",
    "balance_amount": "66975.000000",
    "currency": "EGP",
    "journal_entry": {
      "id": "c4d5e6f7-8a9b-40c1-92d3-e4f5a6b7c8d9",
      "number": "SAL-2026-000118",
      "status": "posted",
      "entry_date": "2026-03-11",
      "period": { "id": "aa11bb22-cc33-4d44-8e55-ff6600112233", "name": "2026-03", "status": "open" },
      "total_debit": "66975.000000",
      "total_credit": "66975.000000",
      "lines": [
        { "line_number": 1, "account": { "code": "1200", "name": "Accounts Receivable" }, "debit": "66975.000000", "credit": "0.000000" },
        { "line_number": 2, "account": { "code": "4000", "name": "Sales Revenue" },       "debit": "0.000000",     "credit": "58750.000000" },
        { "line_number": 3, "account": { "code": "2300", "name": "VAT Payable" },          "debit": "0.000000",     "credit": "8225.000000" }
      ]
    }
  }
}
```

Failure cases, all `409`: `period_closed` if March is closed,
`illegal_transition` if the invoice is already issued, `sequence_exhausted` if
the `DocumentSequence` row lock times out. Stale `If-Match` → `412
concurrent_modification`. Missing `X-Reauth-Token` → `403 reauth_required`,
because `sales.invoice.issue` is `is_sensitive`.

### 4.3 Record a payment

```http
POST /api/v1/payments HTTP/1.1
Idempotency-Key: c0ffee00-1111-4222-8333-444455556666
X-Reauth-Token: 3e2d1c0b9a8f7e6d5c4b3a2918070605
```
```json
{
  "direction": "inbound",
  "customer": "b21c4f88-1d2e-4a90-9c3f-77e1a2b4c6d0",
  "bank_account": "e5f6a7b8-9c0d-4e1f-8a2b-3c4d5e6f7a8b",
  "payment_date": "2026-03-18",
  "method": "bank_transfer",
  "currency": "EGP",
  "amount": "40000.000000",
  "reference": "TRF-99213",
  "allocations": [
    { "invoice": "7e2b91a4-3c5d-4e6f-9a8b-1c2d3e4f5a6b", "amount": "40000.000000" }
  ]
}
```

`201 Created`

```json
{
  "data": {
    "id": "d1e2f3a4-b5c6-4778-9a8b-0c1d2e3f4a5b",
    "number": "PMT-2026-000311",
    "status": "recorded",
    "direction": "inbound",
    "payment_date": "2026-03-18",
    "currency": "EGP",
    "amount": "40000.000000",
    "unallocated_amount": "0.000000",
    "allocations": [
      {
        "invoice": { "id": "7e2b91a4-3c5d-4e6f-9a8b-1c2d3e4f5a6b", "number": "INV-2026-000042" },
        "amount": "40000.000000",
        "invoice_balance_after": "26975.000000"
      }
    ],
    "journal_entry": {
      "id": "f0e1d2c3-b4a5-4697-8879-6a5b4c3d2e1f",
      "number": "CSH-2026-000204",
      "status": "posted",
      "total_debit": "40000.000000",
      "total_credit": "40000.000000",
      "lines": [
        { "line_number": 1, "account": { "code": "1010", "name": "Bank — CIB EGP" },   "debit": "40000.000000", "credit": "0.000000" },
        { "line_number": 2, "account": { "code": "1200", "name": "Accounts Receivable" }, "debit": "0.000000",  "credit": "40000.000000" }
      ]
    }
  }
}
```

A replay of the same `Idempotency-Key` with the same body returns this exact
`201` again with `Idempotency-Replayed: true`. The same key with `"amount":
"45000.000000"` returns `409 duplicate_idempotency_key`.

Partial allocation is allowed and leaves `unallocated_amount` positive; the
residue sits on the customer's account until
`POST /payments/{id}/allocate`. Over-allocation returns `400 validation_error`
with `field_errors["allocations[0].amount"]`. Allocation splits use
`apps.core.fields.allocate()`, whose largest-remainder method guarantees the
parts sum exactly to the total — naive proportional splitting invents or loses
minor units and breaks the trial balance.

### 4.4 Stripe webhook receipt

```http
POST /api/v1/webhooks/stripe HTTP/1.1
Host: 6f1e9c34.app.example.com
Stripe-Signature: t=1773824102,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd
Content-Type: application/json
```
```json
{
  "id": "evt_1PqR2sABCDEfghIJ",
  "type": "payment_intent.succeeded",
  "created": 1773824102,
  "livemode": true,
  "data": {
    "object": {
      "id": "pi_3PqR2sABCDEfghIJ0KLmnoPQ",
      "object": "payment_intent",
      "amount": 4000000,
      "currency": "egp",
      "status": "succeeded",
      "metadata": {
        "tenant_id": "6f1e9c34-6f2a-4a5e-8f1b-3e0a9d1c7b22",
        "invoice_id": "7e2b91a4-3c5d-4e6f-9a8b-1c2d3e4f5a6b"
      }
    }
  }
}
```

`202 Accepted`

```json
{
  "data": {
    "webhook_event_id": "aa00bb11-cc22-4d33-8e44-ff5566778899",
    "provider": "stripe",
    "provider_event_id": "evt_1PqR2sABCDEfghIJ",
    "status": "queued",
    "received_at": "2026-03-18T11:35:02.441Z"
  }
}
```

Six rules, each preventing a specific incident:

1. **Verify the signature before parsing the body**, using the raw bytes and a
   constant-time compare against the tenant's gateway secret. Reject with `400
   invalid_signature`. Parsing first means an attacker's malformed JSON reaches
   your deserialiser before authentication does.
2. **Reject a timestamp skew over 300 s.** `t=` is inside the signed payload,
   so this is what stops replay of a genuine, correctly signed event.
3. **`202`, not `200`, and store-then-process.** The handler writes a
   `WebhookEvent` row and enqueues a Celery task. Doing the ledger posting
   inline means a slow `post_entry()` blows Stripe's 20-second timeout, Stripe
   retries, and you have two payments.
4. **The tenant comes from the endpoint and from `metadata.tenant_id`, and they
   must agree.** A signature valid for tenant A's secret on a body claiming
   tenant B is rejected. Trusting `metadata` alone would let a tenant with a
   valid gateway inject events into another tenant's ledger.
5. **`provider_event_id` is uniquely constrained per tenant.** Gateways deliver
   at-least-once and *will* send `evt_…` twice. The unique index turns the
   duplicate into a no-op `202`, not a second payment.
6. **Amounts are converted from the gateway's minor units to `Decimal` using
   `CURRENCY_MINOR_UNITS`.** Stripe sends `4000000` for EGP (2 minor units) —
   but 3 minor units for KWD and 0 for JPY. Dividing by a hard-coded 100 is a
   1000× error in Kuwait.

Non-2xx from us makes the gateway retry; that is intended for a genuine outage
and is why signature failures are `400` (permanent, no retry) while a database
error is `500` (retry).

### 4.5 Calculate a payroll run

```http
POST /api/v1/payroll-runs/8b7a6959-4838-4727-9616-05a4b3c2d1e0/calculate HTTP/1.1
If-Match: "8b7a6959-2026-03-25T07:02:11.004Z-draft"
Idempotency-Key: 11112222-3333-4444-8555-666677778888
X-Reauth-Token: aabbccddeeff00112233445566778899
```
```json
{ "recalculate": false, "include_departments": ["4c5d6e7f-8a9b-40c1-92d3-e4f5a6b7c8d9"] }
```

`202 Accepted`

```json
{
  "data": {
    "id": "8b7a6959-4838-4727-9616-05a4b3c2d1e0",
    "status": "calculating",
    "job": {
      "id": "job_2026_03_payroll_calc_8b7a6959",
      "status": "queued",
      "poll_url": "/api/v1/jobs/job_2026_03_payroll_calc_8b7a6959",
      "channel": "tenant.6f1e9c34.payroll_run.8b7a6959"
    }
  }
}
```

Then `GET /api/v1/payroll-runs/8b7a6959-…` once the job reports `succeeded`:

```json
{
  "data": {
    "id": "8b7a6959-4838-4727-9616-05a4b3c2d1e0",
    "period": { "id": "aa11bb22-cc33-4d44-8e55-ff6600112233", "name": "2026-03" },
    "pay_date": "2026-03-28",
    "status": "calculated",
    "currency": "EGP",
    "employee_count": 214,
    "totals": {
      "gross_amount": "3184500.000000",
      "tax_amount": "441220.500000",
      "social_insurance_employee": "222915.000000",
      "social_insurance_employer": "382140.000000",
      "other_deductions": "68300.000000",
      "net_amount": "2452064.500000",
      "employer_cost": "3566640.000000"
    },
    "calculated_at": "2026-03-25T07:04:38.219Z",
    "calculated_by": { "id": "0b1c2d3e-4f50-4617-8829-3a4b5c6d7e8f", "full_name": "Mona Fahmy" },
    "approved_at": null,
    "approved_by": null,
    "journal_entry": null,
    "warnings": [
      { "code": "missing_bank_details", "employee_count": 2,
        "detail": "2 employees have no bank account; they will be excluded from the transfer file." }
    ]
  }
}
```

`202` and a job, not `200` and a result: gross-to-net for 214 employees across
progressive `tax_bracket` bands is seconds of work, and holding an HTTP
connection open for it means a proxy timeout leaves the client unable to tell a
failed calculation from a slow one. `calculated_by` is recorded here precisely
so that the approve step can refuse the same person (§4.6).

### 4.6 Approve a leave request

```http
POST /api/v1/leave-requests/6a5b4c3d-2e1f-4009-8817-263544536271/approve HTTP/1.1
If-Match: "6a5b4c3d-2026-03-20T13:01:55.700Z-pending"
Idempotency-Key: 99998888-7777-6666-8555-444433332222
X-Reauth-Token: ffeeddccbbaa99887766554433221100
```
```json
{ "comment": "Approved. Handover to Karim agreed." }
```

`200 OK`

```json
{
  "data": {
    "id": "6a5b4c3d-2e1f-4009-8817-263544536271",
    "status": "approved",
    "employee": {
      "id": "7b6c5d4e-3f20-411a-9b8c-7d6e5f4a3b2c",
      "code": "EMP-00417",
      "full_name": "Yasmine Adel",
      "department": { "id": "4c5d6e7f-8a9b-40c1-92d3-e4f5a6b7c8d9", "name": "Platform Engineering" }
    },
    "leave_type": { "id": "1a2b3c4d-5e6f-4708-8919-2a3b4c5d6e7f", "name": "Annual leave", "is_paid": true },
    "start_date": "2026-04-06",
    "end_date": "2026-04-10",
    "working_days": "5.000000",
    "approved_at": "2026-03-20T13:04:12.338Z",
    "approved_by": { "id": "0b1c2d3e-4f50-4617-8829-3a4b5c6d7e8f", "full_name": "Mona Fahmy" },
    "balance_after": {
      "leave_type": "1a2b3c4d-5e6f-4708-8919-2a3b4c5d6e7f",
      "entitled_days": "21.000000",
      "taken_days": "9.000000",
      "pending_days": "0.000000",
      "remaining_days": "12.000000"
    },
    "comment": "Approved. Handover to Karim agreed."
  }
}
```

Refusals, and which layer produces each:

* `403 permission_denied` — the actor lacks `hr.leave_request.approve` (RBAC, `HasPermission`).
* `404 not_found` — the employee is outside the actor's `department_subtree` (ABAC, `build_scope_q`). A 404 rather than a 403 so the manager cannot enumerate employees in a department they do not own.
* `403 segregation_of_duties` — the request is the approver's own, refused by `exclude_self_prepared`.
* `409 illegal_transition` — already approved or cancelled.
* `412 concurrent_modification` — two managers approved simultaneously; the second sees this instead of double-consuming the balance.

`balance_after` is returned inline because the client's next render needs it,
and a follow-up `GET /leave-balances` would race the approval it just made.

---

## 5. Webhooks

### 5.1 Received (gateway → platform)

| Provider | Endpoint | Signature scheme | Verification |
| --- | --- | --- | --- |
| Stripe | `POST /api/v1/webhooks/stripe` | `Stripe-Signature: t=…,v1=HMAC-SHA256(t + "." + raw_body, secret)` | Constant-time compare, skew ≤ 300 s |
| Paymob | `POST /api/v1/webhooks/paymob` | `hmac` query param, SHA-512 over the documented concatenated field order | Fields concatenated in the fixed order, never in dict order |
| PayPal | `POST /api/v1/webhooks/paypal` | `PayPal-Transmission-Sig`, SHA256withRSA over `transmission_id\|time\|webhook_id\|crc32(body)` | Certificate fetched from the pinned `api.paypal.com` host and cached by URL |
| Fawry | `POST /api/v1/webhooks/fawry` | `signature`: SHA-256 over ordered fields + merchant secret | Same |
| Bank feed (Plaid-style) | `POST /api/v1/webhooks/bankfeed` | JWT `ES256`, `kid` resolved against the provider JWKS | JWKS cached 24 h; unknown `kid` triggers one refetch, then rejects |

Common rules: raw-body verification (any middleware that re-serialises the body
before the signature check breaks it and, worse, breaks it *intermittently* on
key ordering); per-tenant secrets stored encrypted and rotatable via
`POST /gateways/{id}/rotate-secret`; **both** the old and new secret accepted
for 24 hours after a rotation, because otherwise every in-flight event is
dropped at the moment of rotation.

Every accepted event becomes a `webhook_event` row with `provider`,
`provider_event_id` (unique per tenant), `payload`, `status`
(`queued|processing|processed|failed|ignored`) and `attempts`. Failures are
inspectable at `GET /webhook-events` and re-runnable with
`POST /webhook-events/{id}/replay`.

### 5.2 Emitted (platform → tenant systems)

`POST` to the tenant's configured URL:

```http
POST https://erp.customer.example/hooks/accounting HTTP/1.1
Content-Type: application/json
X-Webhook-Id: 9f8e7d6c-5b4a-4392-8180-7f6e5d4c3b2a
X-Webhook-Timestamp: 1773824102
X-Webhook-Signature: v1=6f2c4d…            # HMAC-SHA256(timestamp + "." + raw_body, endpoint_secret)
X-Webhook-Attempt: 1
```
```json
{
  "id": "9f8e7d6c-5b4a-4392-8180-7f6e5d4c3b2a",
  "type": "invoice.issued",
  "api_version": "v1",
  "tenant_id": "6f1e9c34-6f2a-4a5e-8f1b-3e0a9d1c7b22",
  "created_at": "2026-03-11T09:16:41.902Z",
  "data": {
    "id": "7e2b91a4-3c5d-4e6f-9a8b-1c2d3e4f5a6b",
    "number": "INV-2026-000042",
    "status": "issued",
    "currency": "EGP",
    "total_amount": "66975.000000"
  }
}
```

Event types: `invoice.issued`, `invoice.paid`, `invoice.voided`,
`invoice.overdue`, `credit_note.issued`, `payment.recorded`, `payment.failed`,
`refund.completed`, `bill.approved`, `expense.approved`, `expense.rejected`,
`stock.low`, `stock.movement_posted`, `journal_entry.posted`,
`period.closed`, `payroll_run.approved`, `payroll_run.paid`,
`payslip.published`, `leave_request.approved`, `leave_request.rejected`,
`employee.onboarded`, `employee.terminated`, `project.closed`.

**Retry and backoff.** Any response other than 2xx, or a timeout over 10 s, is
a failure. Retries at **1 min, 5 min, 30 min, 2 h, 6 h, 12 h, 24 h** — 7
attempts over ~45 h — each with **±20 % jitter**. The jitter is not cosmetic:
without it, a customer endpoint that goes down for ten minutes brings back
every queued webhook from every tenant in one synchronised thundering herd at
the same instant, which knocks it over again.

Delivery is at-least-once, so `X-Webhook-Id` is stable across retries and
consumers must deduplicate on it. After the 7th failure the endpoint is marked
`failing`; after 24 h in that state it is auto-disabled and the tenant's Admins
are emailed. Events remain replayable for 30 days.

The signature scheme is the same HMAC construction we *require* of gateways,
for the same reason and with the same 300-second skew window. `POST
/settings/integrations/{id}/rotate-secret` supports overlapping secrets, and
the payload carries only identifiers and headline figures — never bank details,
never salary — because a webhook URL is a plaintext HTTP endpoint the customer
controls and we cannot audit.

---

## 6. Real-time channels

WebSocket at `wss://{tenant}.app.example.com/ws/v1/`, with an SSE fallback at
`GET /api/v1/events/stream` for corporate proxies that terminate WebSocket
upgrades.

| Channel | Payload | Who subscribes |
| --- | --- | --- |
| `tenant.{tid}.stock.{warehouse_id}` | `{item_id, quantity_on_hand, quantity_available, reorder_point, updated_at}` | POS and order-entry screens |
| `tenant.{tid}.invoice.{invoice_id}` | `{status, paid_amount, balance_amount, updated_at}` | Invoice detail view |
| `tenant.{tid}.invoices` | Status changes across the tenant, throttled to 1 msg/s | Invoice list view |
| `tenant.{tid}.user.{user_id}.notifications` | `{unread_count, latest: {...}}` | Notification badge |
| `tenant.{tid}.payroll_run.{run_id}` | `{status, progress_pct, processed, total}` | Payroll calculation progress |
| `tenant.{tid}.job.{job_id}` | `{status, progress_pct, result_url}` | Report exports, imports |
| `tenant.{tid}.reconciliation.{id}` | `{matched_count, unmatched_count, difference_amount}` | Reconciliation screen |

**Channel authorisation.** Every channel name begins with `tenant.{tenant_id}`,
and subscription is checked at the moment of `SUBSCRIBE`, not only at connect:

1. The connection authenticates with the same access JWT (`?token=` on the
   handshake, since browsers cannot set headers on a WebSocket upgrade). An
   expired token closes the socket with `4401`.
2. `{tid}` in the requested channel **must equal** the `tid` in the token.
   Anything else is refused with `4403` and logged as a security event — a
   client asking for another tenant's channel is either broken or probing.
3. The subscriber must hold the channel's read permission — `inventory.item.read`
   for stock, `sales.invoice.read` for invoices, `payroll.payroll_run.read` for
   payroll progress — resolved from the same cached set as HTTP.
4. Object-level channels are re-checked through `build_scope_q()`: a Department
   Manager subscribing to `tenant.{tid}.invoice.{id}` for an invoice outside
   their `assigned_projects` scope is refused. Real-time is a second read path
   to the same rows, so it goes through the same gate — this is the most common
   place ABAC gets forgotten, because the socket layer usually lives in
   different code from the viewsets.
5. `…user.{user_id}.notifications` requires `user_id == token.sub` exactly. No
   permission grants access to someone else's badge.
6. Re-authorisation runs on **every** token refresh pushed over the socket, and
   subscriptions the refreshed token no longer satisfies are dropped. Without
   this, a role revoked at 10:00 keeps streaming to a socket opened at 09:00
   until the user reloads the page.

Messages are fan-out only; the client never mutates over the socket. A
mutation over a WebSocket bypasses the middleware chain, the idempotency
store, the ETag check and the rate limiter — every guard in this document.

Ordering is per channel with a monotonic `seq`; a client that sees a gap
re-fetches over HTTP rather than assuming. At-most-once delivery is accepted
deliberately: the socket is a cache-invalidation hint, never a source of truth.

---

## 7. Rate limiting

Buckets are keyed `(tenant_id, user_id, endpoint_class)` in Redis using a
sliding window. Responses carry `RateLimit-Limit`, `RateLimit-Remaining`,
`RateLimit-Reset`; a `429 rate_limited` carries `Retry-After`.

### 7.1 Per endpoint class

| Class | Examples | Limit (per user) | Why |
| --- | --- | --- | --- |
| `auth` | `/auth/token`, `/auth/reauth` | 10 / 5 min, then exponential lockout on failures | Credential stuffing. `User.failed_login_count` and `locked_until` are the durable half. |
| `read` | Any `GET` | 600 / min | Generous; reads are cheap and cursor-paged. |
| `write` | `POST`/`PATCH` on non-financial resources | 120 / min | — |
| `financial` | `/issue`, `/post`, `/payments`, `/refunds`, `/approve`, `/pay` | 30 / min | These take row locks on `DocumentSequence` and `FiscalPeriod`. Unbounded concurrency here is lock contention, not throughput. |
| `report` | `/reports/*` with `format=json` | 20 / min | Each is an aggregation over `accounting_journal_line`. |
| `export` | `/reports/*/export`, `/exports/*` | 10 / hour | Async and expensive; also the exfiltration path, so the limit doubles as a control. |
| `bulk` | `/imports/*`, `/statements` import | 5 / hour | — |
| `webhook_in` | `/webhooks/{provider}` | 1000 / min per tenant | Must not throttle a legitimate gateway burst; signature failures are counted separately and banned by IP at 50 / min. |

### 7.2 Per role multiplier

Applied to the `read`, `write` and `report` classes.

| Role | Multiplier | Rationale |
| --- | :---: | --- |
| Owner | 2.0× | Never rate-limit the person trying to fix an incident. |
| Admin | 2.0× | Bulk administration is their job. |
| Accountant | 1.5× | Month-end is genuinely burst-shaped. |
| HR Manager | 1.5× | Payroll cycle, same shape. |
| Department Manager | 1.0× | — |
| Read-Only Auditor | 1.0× read, **0.5× export** | An auditor's job is reading; the export limit is where data leaves, and a temporary external account is exactly the credential most likely to be shared or lost. |
| Employee | 0.5× | Self-service is a handful of rows. A compromised employee account then cannot scrape the tenant at speed. |
| API key (`ApiKey`) | 3.0× on `read`/`write`, 1.0× on `financial` | Machine integrations are legitimately chatty, but a runaway retry loop must not be able to post a thousand journal entries a minute. |

Per-tenant ceilings by `Subscription.plan` sit above the per-user limits
(Starter 10k req/h, Standard 50k, Professional 200k, Enterprise negotiated), so
one runaway integration cannot consume a shared worker pool and degrade every
other tenant. Exceeding the tenant ceiling returns `429` with
`detail: "workspace rate limit"` so support can tell the two cases apart
immediately.

---

## 8. Contract verification

`backend/config/permissions.json` is validated in CI by
`scripts/validate_permissions.py`, which asserts that the file parses, that
every codename referenced by a role exists in `permissions[]`, that every
`domain` is one of `Permission.Domain`'s eleven values, and that every
`strategy` is one of `ScopeRule.Strategy`'s eight. Latest run:

```
$ python3 -m json.tool backend/config/permissions.json > /dev/null && echo VALID
VALID
$ python3 scripts/validate_permissions.py
OK    /root/proj/backend/config/permissions.json
OK    version=1.0.0  permissions=187  roles=7
OK    all 632 role->permission references resolve to a defined codename
OK    all 59 scope rules use a valid ScopeRule.Strategy value
OK    68 permissions marked is_sensitive
```

A dangling codename in a role is the failure this catches: the role renders
correctly in the admin UI, `sync_permissions` skips the unknown name, and the
endpoint denies in production for a reason nobody can find.
