# 07 — Frontend Architecture (Web + Mobile)

Status: **blueprint**. Phase 4 (web) and Phase 5 (mobile) build against this
document. Nothing here is optional decoration: every rule below exists because
of a specific way accounting front-ends break.

The one-sentence version: **the client never computes money, never caches
across tenants, and never optimistically posts to the ledger.**

---

## 1. Monorepo layout

pnpm workspaces. One repo, one lockfile, one `pnpm build`.

```
frontend/
├── pnpm-workspace.yaml
├── package.json                 # scripts only; no runtime deps at the root
├── turbo.json                   # task graph + remote cache
├── apps/
│   ├── web/                     # Next.js 14, App Router, TypeScript
│   │   ├── app/                 # route segments (see §9)
│   │   ├── features/            # vertical slices: invoices/, payroll/, ...
│   │   ├── components/          # app-specific composition of packages/ui
│   │   └── lib/                 # query client, auth, i18n wiring
│   └── mobile/                  # React Native + Expo (SDK 51), TypeScript
│       ├── app/                 # expo-router file routes
│       ├── features/
│       ├── outbox/              # offline mutation queue (see §6)
│       └── lib/
└── packages/
    ├── domain/                  # ← THE CONTRACT. Types + money + Zod schemas
    ├── api-client/              # typed fetch layer, auth, retries, idempotency
    └── ui/                      # design system primitives, RTL-aware
```

### What lives where — and what must not

| Package | Contains | Never contains |
|---|---|---|
| `packages/domain` | Generated OpenAPI types, the `Money` type and its arithmetic, generated Zod schemas, enum unions, permission codename union, pure business predicates (`isOverdue`, `canPostEntry`) | React, fetch, storage, anything platform-specific |
| `packages/api-client` | `createClient()`, auth token plumbing, retry/backoff, `Idempotency-Key` injection, error normalisation, TanStack Query key factory | React components, business rules |
| `packages/ui` | Buttons, tables, `<MoneyText>`, `<VirtualTable>`, form controls, RTL-aware layout primitives | Data fetching, domain knowledge beyond formatting |
| `apps/web` | Routing, server components, layout, web-only auth (cookies) | Duplicated types, hand-written money maths |
| `apps/mobile` | Routing, native modules, biometrics, outbox, camera | Duplicated types, hand-written money maths |

`packages/domain` is **generated, not written**:

```bash
# backend
python manage.py spectacular --file ../frontend/packages/domain/openapi.json
# frontend
pnpm --filter @erp/domain generate   # openapi-typescript + openapi-zod-client
```

CI regenerates and fails on a diff. That check is the entire reason the
TypeScript types cannot drift from the API: a serializer field rename becomes a
red build in the web app on the same commit, not a runtime `undefined` three
weeks later.

---

## 2. THE MONEY RULE ON THE CLIENT

> **A monetary amount is NEVER a JavaScript `number`. Not in a variable, not in
> a prop, not in a form field, not in `JSON.parse`.**

### Why

A JS `number` is an IEEE-754 float64. It cannot represent `0.1`. The closest
value is `0.1000000000000000055511151231257827…`, so:

```js
0.1 + 0.2 === 0.30000000000000004   // true
1.005 * 100                          // 100.49999999999999  -> rounds to 100.49
```

Consequences in *this* system specifically:

1. **The totals row disagrees with the ledger.** The server sums
   `numeric(19,6)` in PostgreSQL and stores an invoice total of `10,000.00`.
   The client re-sums 37 float lines and renders `9,999.999999999998`, which
   `toFixed(2)` shows as `10,000.00` — until the day it shows `9,999.99` and a
   customer opens a ticket saying the invoice does not add up.
2. **Rounding drift compounds.** VAT on each line, computed in float and
   rounded, will differ from the server's largest-remainder allocation
   (`apps/core/fields.py::allocate`) by cents. Multiply by 4,000 invoices a
   month.
3. **Large values lose integer precision.** `Number.MAX_SAFE_INTEGER` is
   9,007,199,254,740,991. In minor units that is ~90 trillion — fine for USD,
   but IDR/VND/IRR balances plus 6 decimal places of unit price get there.
4. **`JSON.parse` destroys the value before you touch it.** The API sends
   `"1234.567890"` as a **string** (`COERCE_DECIMAL_TO_STRING = True` in
   `config/settings/base.py`) precisely so that parsing cannot silently
   lossy-convert it. If a field ever arrives as a bare JSON number, that is a
   backend bug — fix the serializer, do not paper over it on the client.

The rule extends to `<input>`: a money input is `type="text"` with
`inputMode="decimal"`, never `type="number"` (which hands you a float back
through `valueAsNumber` and, in some locales, mangles the decimal separator).

### The type

`packages/domain/src/money.ts`:

```ts
import Decimal from 'decimal.js';

/** ISO-4217 alpha-3. Union generated from the backend Currency choices. */
export type CurrencyCode = 'EGP' | 'USD' | 'EUR' | 'GBP' | 'SAR' | 'AED' | 'KWD';

/**
 * A monetary amount exactly as the API represents it.
 *
 * `amount` is a decimal STRING (e.g. "1234.567890"), never a number. The
 * branded type makes `money.amount * 2` a compile error rather than a
 * silently wrong total.
 */
export type Money = {
  readonly amount: string & { readonly __decimal: unique symbol };
  readonly currency: CurrencyCode;
};

/** Minor units per currency; mirrors CURRENCY_MINOR_UNITS in apps/core/fields.py. */
const MINOR_UNITS: Record<string, number> = {
  JPY: 0, KRW: 0, VND: 0, CLP: 0, ISK: 0,
  BHD: 3, IQD: 3, JOD: 3, KWD: 3, LYD: 3, OMR: 3, TND: 3,
};

export const minorUnits = (c: CurrencyCode): number => MINOR_UNITS[c] ?? 2;

// ROUND_HALF_UP matches the server's MONEY_CONTEXT. If the client rounded
// half-even and the server rounded half-up, every .005 amount would differ by
// one minor unit and nobody would be able to say which side was "right".
Decimal.set({ precision: 34, rounding: Decimal.ROUND_HALF_UP, toExpNeg: -9e15, toExpPos: 9e15 });
```

### The three helpers everything else is built from

```ts
/**
 * 1. Construct. The ONLY sanctioned way to make a Money.
 *    Rejects `number` at the type level *and* at runtime, because untyped
 *    JSON from a stale API version is the exact case types cannot catch.
 */
export function money(amount: string | Decimal, currency: CurrencyCode): Money {
  if (typeof amount === 'number') {
    throw new TypeError(
      'Refusing to build Money from a number: float64 cannot represent 0.1. ' +
      'Pass the API string through unchanged.',
    );
  }
  const d = new Decimal(amount);
  if (!d.isFinite()) throw new TypeError(`Invalid monetary amount: ${String(amount)}`);
  return { amount: d.toFixed(6) as Money['amount'], currency };
}

/**
 * 2. Add. Currency-checked, because "total" over a mixed-currency list is not
 *    a number, it is a bug. The server would reject the equivalent posting;
 *    the client must not display a plausible-looking sum it cannot post.
 */
export function addMoney(...values: Money[]): Money {
  if (values.length === 0) throw new RangeError('addMoney() needs at least one value');
  const currency = values[0].currency;
  const mixed = values.find((v) => v.currency !== currency);
  if (mixed) {
    throw new TypeError(
      `Cannot add ${currency} to ${mixed.currency}. Convert through ExchangeRate first.`,
    );
  }
  const total = values.reduce((acc, v) => acc.plus(new Decimal(v.amount)), new Decimal(0));
  return { amount: total.toFixed(6) as Money['amount'], currency };
}

/**
 * 3. Format. The single place a Money becomes human-readable. Rounds ONCE,
 *    at the presentation boundary, to the currency's minor units — mirroring
 *    the server's "quantize only at the posting/render boundary" policy.
 *    Uses Intl so Arabic locales get the right separators and currency
 *    placement without a hand-rolled table.
 */
export function formatMoney(
  value: Money,
  locale: string = 'en',
  opts: { showCurrency?: boolean } = {},
): string {
  const digits = minorUnits(value.currency);
  const rounded = new Decimal(value.amount).toFixed(digits, Decimal.ROUND_HALF_UP);
  return new Intl.NumberFormat(locale, {
    style: opts.showCurrency === false ? 'decimal' : 'currency',
    currency: value.currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    // Intl accepts a string here in modern runtimes; Decimal has already
    // reduced it to a fixed-point representation with no exponent.
  } as Intl.NumberFormatOptions).format(rounded as unknown as number);
}
```

Also in the module, built on the same three: `subtractMoney`, `multiplyMoney`
(by a `Decimal` rate, never a number), `compareMoney`, `isZero`, `negate`, and
`allocate(total, weights)` — a direct port of the backend's largest-remainder
algorithm so that a client-side preview of a tax split matches what the server
will actually post, to the minor unit.

> **Prefer server-computed totals.** Even with exact arithmetic, the client
> re-implementing the tax engine is a second source of truth. Invoice totals,
> payroll gross/net and every GL figure come from the API. Client-side
> arithmetic is for *previews and drafts only* — and the preview endpoint
> (`POST /api/v1/sales/invoices/preview/`) exists so even those come from the
> server when the stakes are high.

---

## 3. State management

Three stores, three jobs, no overlap.

### 3.1 Server state → TanStack Query (v5). Nothing else.

Server state is not application state. It is a **cache of someone else's data**
that is stale the moment it arrives, needs revalidation, deduplication,
background refetch, retry and garbage collection. That is a library, and it is
not Redux.

#### The key factory — tenantId FIRST, always

`packages/api-client/src/queryKeys.ts`:

```ts
export const qk = {
  all: (t: TenantId) => [t] as const,

  invoices: {
    all:    (t: TenantId) => [t, 'invoices'] as const,
    list:   (t: TenantId, f: InvoiceFilters) => [t, 'invoices', 'list', f] as const,
    detail: (t: TenantId, id: string)        => [t, 'invoices', 'detail', id] as const,
  },
  customers: {
    all:     (t: TenantId) => [t, 'customers'] as const,
    detail:  (t: TenantId, id: string) => [t, 'customers', 'detail', id] as const,
    balance: (t: TenantId, id: string) => [t, 'customers', 'balance', id] as const,
  },
  ledger: {
    trialBalance: (t: TenantId, p: PeriodId) => [t, 'ledger', 'trial-balance', p] as const,
    entries:      (t: TenantId, f: EntryFilters) => [t, 'ledger', 'entries', f] as const,
  },
  payroll: {
    run:      (t: TenantId, id: string) => [t, 'payroll', 'run', id] as const,
    payslips: (t: TenantId, runId: string) => [t, 'payroll', 'payslips', runId] as const,
  },
  reports: {
    arAging:   (t: TenantId, asOf: string) => [t, 'reports', 'ar-aging', asOf] as const,
    dashboard: (t: TenantId) => [t, 'reports', 'dashboard'] as const,
  },
} as const;
```

> ### ⚠ The tenantId-first rule
>
> **Omitting `tenantId` from the key serves one tenant's data to another.**
>
> The failure is concrete and it has shipped in real products. An outsourced
> accountant is a member of five tenants. They view Acme's invoice list — the
> cache now holds `['invoices','list',{status:'open'}] → Acme's invoices`. They
> switch to Globex. The component mounts, TanStack Query finds a key hit,
> and **renders Acme's invoices inside Globex's UI** while the background
> refetch is still in flight. For a few hundred milliseconds — or indefinitely
> if `staleTime` is long or the user is offline — one customer is looking at
> another customer's receivables. No backend control can prevent this: the API
> was never called.
>
> Two enforcements, because a convention is not a control:
> 1. An ESLint rule (`erp/query-key-tenant-first`) rejects any `useQuery`
>    whose `queryKey[0]` is not a `TenantId`.
> 2. Tenant switch calls `queryClient.clear()` anyway (§8). Belt and braces.

#### staleTime per data class

Set deliberately per class rather than globally, because "how wrong may this be
for how long" is a different answer for a currency list and for a trial balance.

| Data class | Examples | `staleTime` | `gcTime` | Why |
|---|---|---|---|---|
| Reference / static | currencies, countries, tax rates, chart of accounts, permission catalogue | **1 hour** (∞ for currencies) | 24 h | Changes monthly at most; refetching on every mount is pure waste |
| Org structure | departments, employees, customers, items | **5 min** | 30 min | Edited occasionally; 5 minutes of staleness is invisible |
| Operational documents | invoice list, expense list, leave requests | **60 s** | 10 min | Users expect a colleague's edit to show up "soon" |
| **Ledger / financial truth** | journal entries, trial balance, account balances, payslips | **0** (always stale) | 5 min | A stale balance is a wrong balance. Always refetch on mount and on focus; the cached value is only there to avoid a spinner |
| Dashboards / aggregates | KPI tiles, AR aging, cash-flow chart | **2–5 min** | 15 min | Expensive server aggregates; a 3-minute-old KPI is fine, a 3-minute-old GL is not |
| Realtime-backed | notifications, presence | **∞** + WebSocket push | 1 h | The socket is the invalidation signal (§7) |

```ts
// apps/web/lib/queryClient.ts
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,              // conservative default; per-hook overrides
      gcTime: 10 * 60_000,
      retry: (failureCount, err) =>
        // Never retry auth/permission/validation. Retrying a 403 just burns
        // the throttle budget and can trip the account lockout counter.
        isRetryableError(err) && failureCount < 3,
      retryDelay: (n) => Math.min(1000 * 2 ** n, 30_000),
      refetchOnWindowFocus: true,     // the accountant who left a tab open
      throwOnError: false,
    },
    mutations: { retry: 0 },          // see §5: never blind-retry a mutation
  },
});
```

### 3.2 Ephemeral UI state → Zustand

Only things that die with the session and belong to no server: sidebar
collapsed, active ledger column set, multi-select in a table, wizard step,
unsaved filter panel, theme, "compact rows" toggle.

```ts
export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      ledgerColumns: DEFAULT_LEDGER_COLUMNS,
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
    }),
    {
      name: 'erp-ui',
      // Persist only presentation preferences. Anything tenant-derived in
      // localStorage survives a tenant switch and a logout — the same leak
      // as an untenanted query key, with a longer lifetime.
      partialize: (s) => ({ sidebarCollapsed: s.sidebarCollapsed, ledgerColumns: s.ledgerColumns }),
    },
  ),
);
```

### 3.3 Forms → React Hook Form + Zod, schemas generated

```ts
// Generated from the OpenAPI schema — hand-written duplicates rot.
import { InvoiceCreateSchema } from '@erp/domain/schemas';

// Local refinement for rules the schema cannot express (cross-field, UX-only).
const formSchema = InvoiceCreateSchema.extend({
  lines: InvoiceCreateSchema.shape.lines.min(1, 'An invoice needs at least one line'),
}).refine((v) => !v.dueDate || v.dueDate >= v.issueDate, {
  message: 'Due date cannot precede the issue date',
  path: ['dueDate'],
});

const form = useForm<z.infer<typeof formSchema>>({
  resolver: zodResolver(formSchema),
  mode: 'onBlur',
});
```

Money fields are `z.string().regex(/^-?\d{1,13}(\.\d{1,6})?$/)` — **string, not
`z.number()`**. `z.coerce.number()` on a money field is the single most common
way the float bug re-enters a codebase that had banished it.

Client validation is UX. The server re-validates everything; the DB constraints
and triggers in `apps/accounting/migrations/0002_ledger_guards.py` are the
actual guarantee.

### 3.4 Why NOT Redux (or any global store) for server data

Not a style preference — four concrete costs:

1. **You reimplement a cache, badly.** Staleness, dedup, background refetch,
   retry with backoff, request cancellation, garbage collection, offline
   revalidation. TanStack Query ships all of it, tested. In Redux each is a
   hand-rolled thunk, and the half of them nobody gets around to writing show
   up as "the balance didn't update until I hard-refreshed".
2. **Normalisation is a second source of truth.** Once an invoice lives in
   `state.entities.invoices[id]`, every mutation must patch it *and* the
   customer balance *and* the AR aging *and* the dashboard tile. Miss one and
   the UI shows two different numbers for the same fact — in an accounting
   product, that is a support ticket that costs more than the feature.
3. **A global store outlives the tenant.** Query keys can be namespaced and the
   whole cache dropped in one call at tenant switch. A hand-rolled global store
   needs every slice to remember to reset, and the one slice that forgets is
   the cross-tenant leak described above.
4. **Ceremony without payoff.** Slice + thunk + selector + action types per
   endpoint, across ~120 endpoints, versus one `useQuery`.

Redux Toolkit is still fine for a genuinely global, client-owned, complex state
machine. This app has one candidate — the offline outbox on mobile — and even
there a small purpose-built reducer over SQLite is simpler (§6).

---

## 4. Caching and invalidation

Server state is a cache; **a mutation is an invalidation event**. The rule: a
mutation invalidates *everything the mutation could have changed*, including
aggregates the user is not currently looking at. Under-invalidating is how a
user posts a payment and the dashboard keeps showing the old AR total.

`invalidateQueries` with a **prefix** key invalidates all descendants — which
is why the key factory's hierarchy matters.

| Mutation | Invalidated query keys | Why these |
|---|---|---|
| Create/update **draft invoice** | `invoices.detail(id)`, `invoices.all` (list) | Not yet financial; no GL, no balances |
| **Issue invoice** (posts to GL) | `invoices.detail(id)`, `invoices.all`, `customers.balance(customerId)`, `reports.arAging`, `ledger.*`, `reports.dashboard`, `sequences` (next number burned) | Issuing writes AR + revenue + VAT journal lines; every receivable-derived figure moves |
| **Void / credit-note an invoice** | same as issue, plus `creditNotes.all`, `reports.vatReturn` | A reversing entry touches the same accounts |
| **Record payment** | `payments.all`, `invoices.detail(*)` for each applied invoice, `invoices.all`, `customers.balance`, `reports.arAging`, `banking.accounts`, `banking.transactions`, `ledger.*`, `reports.dashboard` | Cash + AR + possibly FX gain/loss |
| **Refund payment** | as payment, plus `payments.detail(id)` | |
| **Gateway webhook** (arrives over WS) | `payments.detail(id)`, `invoices.detail(*)`, `reports.dashboard` | §7 |
| **Approve expense** | `expenses.all`, `expenses.detail(id)`, `reports.dashboard` | Approval alone is workflow, not GL |
| **Post expense / bill** | `expenses.*`, `vendors.balance`, `reports.apAging`, `ledger.*`, `reports.dashboard` | |
| **Stock movement / receipt** | `inventory.stockLevels(itemId)`, `inventory.movements`, `items.detail(id)`, `ledger.*` (COGS/inventory), `reports.dashboard` | Perpetual inventory posts to the GL |
| **Approve leave request** | `leave.requests`, `leave.balance(employeeId)`, `hr.calendar`, `attendance.summary` | Balance is derived |
| **Attendance check-in/out** | `attendance.today(employeeId)`, `attendance.summary(period)` | |
| **Post payroll run** | `payroll.run(id)`, `payroll.payslips(runId)`, `payroll.all`, `hr.employees` (YTD), `leave.balance` (leave encashment), `ledger.*`, `reports.trialBalance`, `reports.dashboard`, `banking.pendingPayments` | The single largest fan-out in the system: salary expense, statutory liabilities, net-pay payable |
| **Close fiscal period** | `ledger.*`, `reports.*`, `periods.all`, and *every* form's "can I post?" gate | Everything downstream of the period is now frozen |
| **Post journal entry (manual)** | `ledger.*`, `reports.trialBalance`, `accounts.balance(*)`, `reports.dashboard` | |
| **Switch tenant** | **`queryClient.clear()`** — the entire cache | §8 |

Implementation note — one place, not sprinkled:

```ts
// packages/api-client/src/invalidation.ts
export const INVALIDATES: Record<MutationName, (t: TenantId, vars: any) => QueryKey[]> = {
  issueInvoice: (t, v) => [
    qk.invoices.detail(t, v.id),
    qk.invoices.all(t),
    qk.customers.balance(t, v.customerId),
    qk.reports.arAging(t, 'current'),
    [t, 'ledger'],           // prefix: everything ledger-shaped
    qk.reports.dashboard(t),
  ],
  postPayrollRun: (t, v) => [
    qk.payroll.run(t, v.id), qk.payroll.payslips(t, v.id), [t, 'payroll'],
    [t, 'ledger'], [t, 'reports'], qk.reports.dashboard(t),
  ],
  // ...
};
```

Keeping the map in one file means a reviewer can check "did you invalidate AR
aging?" by reading a table, instead of auditing twelve `onSuccess` callbacks.

---

## 5. Optimistic updates

Optimistic UI trades correctness for perceived speed. That trade is acceptable
only when a rollback is invisible and harmless.

### ✅ May be optimistic

| Action | Rollback consequence |
|---|---|
| Mark notification read / dismiss | Badge flickers back. Nobody cares |
| Toggle a UI preference synced to the server | Cosmetic |
| Add/edit/reorder a line on a **draft** invoice/expense | Draft, no GL effect, user is looking right at it |
| Save a draft's memo/reference field | Text reappears |
| Star/favourite a report, pin a dashboard tile | Cosmetic |
| Upload progress on an attachment | Progress bar resets |

```ts
const markRead = useMutation({
  mutationFn: api.notifications.markRead,
  onMutate: async (id) => {
    await queryClient.cancelQueries({ queryKey: qk.notifications.all(tenantId) });
    const previous = queryClient.getQueryData(qk.notifications.all(tenantId));
    queryClient.setQueryData(qk.notifications.all(tenantId), (old) => markReadLocally(old, id));
    return { previous };                                    // rollback context
  },
  onError: (_e, _id, ctx) =>
    queryClient.setQueryData(qk.notifications.all(tenantId), ctx?.previous),
  onSettled: () => queryClient.invalidateQueries({ queryKey: qk.notifications.all(tenantId) }),
});
```

### 🚫 MUST NEVER be optimistic

**Anything that posts to the general ledger, moves money, or is legally
significant.**

- Issue / void / reverse an invoice
- Record, apply, or refund a payment
- Post or reverse a journal entry
- Post a payroll run; approve payslips
- Post an expense or a vendor bill
- Any stock movement that generates COGS
- Close a fiscal period; close a fiscal year
- Grant or revoke a role

Why, concretely:

1. **The server can refuse, and its refusal is the truth.** The period lock
   trigger, the balance trigger, the sequence allocator, the period-close race
   (`SELECT … FOR SHARE` in `post_entry`), a subscription in `past_due`, an
   ABAC approval limit — all of these reject *after* the request leaves. An
   optimistic UI has already shown "Invoice INV-2026-0042 issued ✓".
2. **The number is server-allocated.** Invoice and journal numbers come from a
   locked counter row (`accounting_document_sequence`) precisely so there are
   no gaps. The client cannot guess the number; showing a made-up one and then
   swapping it is worse than a spinner.
3. **Rollback is not invisible here.** A user who saw "Payroll posted" and told
   the CFO, then sees it revert, no longer trusts any number in the product.
   Trust is the product.
4. **Double-submit risk.** Optimistic success removes the natural "it's still
   loading" signal that stops a user clicking Post twice. The
   `Idempotency-Key` (§6) protects the ledger, but the UI should not be
   inviting the collision.

The pattern for these is: **disable the button, show a pending state, wait for
the server, then render the server's response.** If the call is slow (payroll
for 900 employees), it returns `202 Accepted` with a job id and the UI polls or
listens on the WebSocket — a progress bar is honest, an optimistic number is
not.

---

## 6. Offline strategy (mobile)

Field reality: warehouse staff with no signal, sales reps on the road,
employees checking in from a basement car park. The app must be useful offline
without ever inventing a financial fact.

### Works offline

| Capability | Mechanism |
|---|---|
| View cached payslips (last 12 months) | Persisted query cache; PDFs pre-fetched to the encrypted FS |
| View cached leave balance, team calendar, employee directory | Persisted query cache |
| View recent invoices/customers (read-only) | Persisted query cache, watermarked "as of <time>" |
| **Draft** an expense + capture receipt photo | Local row + file, queued in the outbox |
| **Draft** a timesheet entry | Outbox |
| Attendance check-in / check-out with GPS | Outbox, with **device-captured timestamp + coordinates + accuracy** |
| Submit a leave request | Outbox |
| Read cached reference data (departments, expense categories, projects) | Long `staleTime`, persisted |

Persistence uses `@tanstack/query-async-storage-persister` over **MMKV with
encryption** (or expo-secure-store for the key). The cache is keyed by
`tenantId` and **wiped on logout and on tenant switch** — a payslip left in an
unencrypted cache after logout is a data-protection incident.

Every offline-rendered screen shows an explicit "Offline · data as of 14:03"
banner. Silently showing stale financial data as if it were live is worse than
showing nothing.

### The outbox / mutation queue

```ts
type OutboxEntry = {
  id: string;                   // local uuid, primary key in SQLite
  idempotencyKey: string;       // uuid v4 — generated ONCE, at enqueue time
  tenantId: string;
  endpoint: string;
  method: 'POST' | 'PATCH' | 'DELETE';
  body: unknown;                // money fields already strings
  attachments: string[];        // local file URIs, uploaded before the body
  createdAt: string;            // device clock
  attempts: number;
  nextAttemptAt: string;
  status: 'pending' | 'in_flight' | 'failed' | 'conflict' | 'done';
  lastError?: { code: string; message: string; httpStatus?: number };
};
```

Rules:

1. **`Idempotency-Key` is generated when the user taps Save, not when the
   request is sent, and it never changes across retries.** This is the whole
   safety property. Scenario without it: the phone sends an expense, the
   server commits it, the response is lost in a tunnel, the outbox retries →
   **two expenses**. With it, the server's idempotency table (TTL
   `IDEMPOTENCY_KEY_TTL_SECONDS`, one week — deliberately longer than any
   plausible offline window) recognises the replay and returns the *original*
   response. Regenerating the key per attempt reintroduces the duplicate,
   which is why it is stored in the row, not computed at send time.
2. **Strictly ordered, serial drain, FIFO per tenant.** Dependencies exist
   (check-in before check-out; expense before its attachment link). Parallel
   drain reorders them. One in-flight request at a time.
3. **Head-of-line blocking is a feature.** A 4xx on entry *n* stops the queue
   and surfaces a "needs attention" item rather than skipping ahead and
   applying entries out of order.
4. **Backoff:** 1s, 2s, 4s … capped at 5 min, with jitter. Drain is triggered
   by `NetInfo` reachability, app foreground, and a background task
   (`expo-task-manager`, best-effort — never the only trigger).
5. **Classify the failure:**
   - `408/429/5xx/network` → retry (transient)
   - `409 Conflict` → conflict resolution (below)
   - `400/422` → **stop, mark `failed`, show the entry for the user to fix**.
     A validation error will never succeed on retry; retrying it forever is
     how outboxes turn into infinite loops.
   - `401` → refresh token once, then retry; if refresh fails, pause the queue
     and prompt for login. **Never drop the entries.**
   - `403` → permissions changed while offline. Park as `failed` with an
     explanation; do not silently discard the user's work.
6. **Never auto-drop an entry.** The user's work is theirs. Only an explicit
   "discard" removes a row.
7. **Cap the queue** (e.g. 500 entries / 200 MB of attachments) and warn well
   before the ceiling.

### Conflict resolution

| Situation | Rule |
|---|---|
| Draft edited on both sides | **Server wins for posted/approved state; user is asked for content.** Show a diff sheet: "Your version / Server version". Never silently overwrite |
| Attendance check-in already recorded server-side for the same day/shift | Idempotent by `(employee, shift, date)`; the replay is absorbed, the device keeps the server's record |
| Leave request already approved/rejected while offline | Server wins. The queued edit is dropped **with an explicit notification** to the user |
| Expense category or project deleted while offline | Entry parks as `failed`; user re-picks. Do not guess a substitute |
| Approval limit reduced while offline | `403` → park; the approval is genuinely no longer permitted |
| Timestamp skew | Device time is recorded as `occurred_at_device` and the server records `received_at`. **The server never trusts device time for anything financial**; a device clock is trivially changed |

Optimistic local rendering *is* allowed for outbox entries — an offline expense
shows in the list immediately with a "pending sync" chip. That does not violate
§5: it is a local draft, clearly labelled, with no GL effect until the server
accepts it.

### 🚫 Online-only operations — no offline path, no queue, no exceptions

Attempting these offline shows "This requires a connection", not a queued item.

- Post / void / reverse **any journal entry**
- Issue, void, or credit-note an **invoice** (server allocates the number)
- Record, apply, or refund a **payment**; any card/gateway operation
- Anything in **payroll**: calculate, approve, post, pay, or view a *new*
  (uncached) payslip
- **Close or reopen** a fiscal period or year
- Bank reconciliation and statement import
- Any **role/permission** change
- Any **export** of ledger data

Rationale in one line: these allocate gapless sequence numbers, take row locks,
or must be refused by a period lock — all of which require the database at the
moment of the decision. A queued ledger posting is a promise the client has no
authority to make.

---

## 7. Real-time

- **Transport:** WebSocket (Django Channels) at `wss://api/ws/v1/`, one
  connection per authenticated session, subscribed to a **per-tenant channel**
  `tenant.<tenantId>` plus a personal channel `user.<userId>`.
- **Authentication:** short-lived ticket obtained over HTTPS
  (`POST /api/v1/auth/ws-ticket/`) and passed as a query param, then validated
  and discarded server-side. The access JWT is **not** put in the URL — URLs
  land in proxy logs, browser history and Sentry breadcrumbs.
- **Authorisation:** channel membership is re-checked on subscribe against
  `TenantMembership`, and the server filters events by the subscriber's
  permission set. A payroll event is not broadcast to a user without
  `payroll.payslip.read` — the socket is not a permission bypass.

### Reconnect / backoff

Exponential backoff with full jitter: `min(30s, 2^n * 500ms) * random()`, reset
on a successful `open` that survives 5 seconds (otherwise a server that accepts
and immediately closes produces a hot loop). Heartbeat ping every 30 s;
missing two pongs forces a reconnect. On mobile, the socket is closed on
background and reopened on foreground — iOS kills it anyway, and pretending
otherwise produces phantom "connected" states.

**After every reconnect, invalidate rather than trust.** Events that occurred
during the gap were missed. The client sends its `lastEventId`; the server
replays from a short (5-minute) buffer if it can, and otherwise responds
`resync_required`, on which the client calls
`queryClient.invalidateQueries({ queryKey: [tenantId] })`.

### Reconciling a push with the cache

Two event shapes, and the distinction is the important part:

```ts
type ServerEvent =
  | { kind: 'invalidate'; keys: QueryKey[]; eventId: string }   // preferred
  | { kind: 'patch'; key: QueryKey; payload: unknown; version: number; eventId: string };
```

- **`invalidate` is the default.** The payload is a *hint*, the API is the
  truth. Marking a key stale and refetching costs one request and cannot be
  wrong.
- **`patch` (writing the payload straight into the cache) is allowed only for
  non-financial, high-frequency data** — notification counts, presence, job
  progress. Never for balances or ledger figures: the socket payload is
  produced by a different code path than the serializer, and the day they
  disagree, the UI shows a number that exists nowhere in the database.
- Events carry a monotonic `version`; a `patch` with a version ≤ the cached
  one is dropped (out-of-order delivery).
- Debounce/coalesce: a bulk import emitting 4,000 events must collapse into one
  invalidation per key per ~500 ms window, or the client refetches itself into
  a stall.

Uses: payment/webhook settled, payroll run progress, import job progress,
period closed by a colleague (banner: "This period was just closed; your form
is now read-only"), another user editing the same document.

---

## 8. Auth flow

### Token storage — and why `localStorage` is wrong

| Platform | Access token | Refresh token |
|---|---|---|
| **Web** | In memory only (a module variable / React context). Lost on reload; recovered by a silent refresh | **httpOnly, Secure, SameSite=Strict cookie**, path-scoped to `/api/v1/auth/` |
| **Mobile** | In memory | **expo-secure-store** (iOS Keychain / Android Keystore), guarded by biometrics |

**`localStorage` is wrong for tokens.** It is readable by any JavaScript
running on the origin. One XSS — in your code, in a dependency, in an analytics
snippet, in a rich-text field that renders unsanitised HTML — and the attacker
does `localStorage.getItem('refresh')` and holds a 7-day credential for a
system containing payroll and bank details. They can exfiltrate it in a single
image request. An httpOnly cookie is not readable by JavaScript at all: the
same XSS can *act as* the user while the page is open (bad, bounded, and
detectable), but it cannot *steal a portable long-lived credential* (worse,
persistent, invisible). Scoping the cookie's `path` to the refresh endpoint
also keeps it off every ordinary API request, shrinking its exposure surface.

`sessionStorage` has the same JS-readability problem. `AsyncStorage` on mobile
is plain unencrypted files — on a rooted/jailbroken device, plain text.

CSRF is handled because the refresh cookie is `SameSite=Strict` **and** the
refresh endpoint additionally requires the double-submit CSRF token; a cookie
alone is never sufficient authority for a state-changing call.

### Silent refresh

- Access token lives **5 minutes** (`SIMPLE_JWT.ACCESS_TOKEN_LIFETIME`). The
  client proactively refreshes at ~60 s remaining, so a request rarely races
  an expiry.
- On `401`, the api-client interceptor: **queues all in-flight requests, issues
  exactly one refresh, then replays the queue.** A per-request refresh is the
  classic bug — twenty parallel 401s trigger twenty refreshes, nineteen of
  which present an already-rotated token, which the blacklist kills, logging
  the user out mid-work.
- Refresh tokens **rotate** and the old one is **blacklisted**
  (`ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION`). If a blacklisted
  token is presented, that is evidence of theft: the server kills the token
  family and the client hard-logs-out.
- A failed refresh clears everything and routes to login. It never retries in a
  loop.

### Tenant switching — **purge the entire query cache**

```ts
async function switchTenant(next: TenantId) {
  await api.auth.switchTenant(next);        // server re-issues tokens for `next`
  queryClient.cancelQueries();              // stop in-flight requests for the OLD tenant
  queryClient.clear();                      // drop every cached entry
  useUiStore.getState().resetTenantScoped();
  wsClient.resubscribe(next);
  router.replace(`/t/${next}/dashboard`);
}
```

**Why `clear()` and not selective invalidation:**

1. Invalidation keeps stale data *and renders it* while refetching. For the
   ~200 ms of a refetch — or forever, offline — the new tenant's screen shows
   the previous tenant's invoices. Disclosure of one customer's financial data
   to another is the single worst bug this product can have.
2. `clear()` is total and needs no maintenance. Selective purging depends on
   every key having been namespaced correctly; the one key someone forgot is
   exactly the one that leaks. The rule must not depend on remembering.
3. The cost is one loading state on a deliberate, infrequent user action.
4. In-flight requests must be **cancelled** too: a response for tenant A that
   lands after the switch would repopulate the cache under A's key, and any
   component still keyed to A would render it.

The same purge runs on **logout** (plus persisted-cache deletion and
secure-store wipe), and the WebSocket resubscribes to the new tenant channel.

Multi-tenant users get a tenant switcher fed by `GET /api/v1/auth/memberships/`
and the current tenant is a **URL segment** (`/t/:tenantId/...`) so that a
bookmarked or shared link is unambiguous and a second browser tab on a
different tenant does not fight the first.

### Biometric unlock (mobile)

- Refresh token stored in secure-store with
  `requireAuthentication: true` → Face ID / Touch ID / device passcode.
- App-resume after 5 minutes backgrounded → biometric prompt before the UI is
  revealed (screen contents are also blurred in the app switcher, so payslips
  do not appear in the OS task screenshot).
- Biometrics gate **local access to the stored token**; they are not an
  authentication factor by themselves — the server never sees "the fingerprint
  matched", only a valid refresh token.
- Fallback to full login on biometric failure, hardware change, or
  `expo-local-authentication` reporting new enrolled biometrics (a fingerprint
  added by someone else must not inherit the session).
- **Sensitive actions re-authenticate** regardless of session state: approving
  payroll, changing bank details, exporting the ledger — matching
  `Permission.is_sensitive` on the backend.

---

## 9. Navigation and route maps

### Web (Next.js App Router)

```
app/
├── (public)/
│   ├── login/                        sign in, MFA challenge
│   ├── forgot-password/  reset-password/
│   └── accept-invitation/[token]/
└── (app)/t/[tenantId]/               ← tenant is a route segment
    ├── layout.tsx                    shell: sidebar, tenant switcher, permission gate
    ├── dashboard/
    ├── sales/
    │   ├── customers/            [id]/  (overview | invoices | payments | statement)
    │   ├── invoices/             new/ | [id]/ | [id]/edit
    │   ├── credit-notes/
    │   └── recurring/
    ├── payments/                 [id]/ | reminders/
    ├── expenses/                 bills/ | categories/
    ├── inventory/                items/[id]/ | movements/ | stock-levels/
    ├── banking/                  accounts/[id]/ | reconcile/[accountId]/ | import/
    ├── projects/                 [id]/ (tasks | timesheets | profitability)
    ├── accounting/
    │   ├── chart-of-accounts/    journals/ | entries/ (new | [id])
    │   ├── periods/              tax-rates/ | exchange-rates/
    │   └── year-end/
    ├── hr/
    │   ├── employees/[id]/ (profile | contracts | documents | payslips)
    │   ├── departments/ | attendance/ | leave/ (requests | balances | calendar)
    ├── payroll/
    │   ├── runs/  runs/[id]/ (review | payslips | postings)
    │   └── components/ | statutory/
    ├── reports/
    │   ├── trial-balance/ | profit-loss/ | balance-sheet/ | cash-flow/
    │   ├── ar-aging/ | ap-aging/ | vat-return/ | payroll-register/
    └── settings/
        ├── organisation/ | users/ | roles/ | api-keys/ | audit-log/
        └── numbering/ | integrations/ | localisation/
```

### Mobile (expo-router) — deliberately a *subset*

Mobile is for people **away from a desk**, not a shrunken ERP. Bottom tabs:

```
(auth)/  login | mfa | biometric-unlock
(tabs)/
├── home/           my tasks, approvals awaiting me, today's attendance
├── attendance/     check-in/out (GPS), history, my shifts       [works offline]
├── expenses/       list | new (camera receipt) | [id]           [draft offline]
├── approvals/      leave, expenses, timesheets — with amount + policy context
└── me/             payslips (cached), leave balance, documents, profile, settings
(modals)/  scan-receipt | switch-tenant | approve-sheet | filter-sheet
```

Explicitly **not** on mobile: journal entry creation, period close, payroll
posting, bank reconciliation, the chart of accounts. Those need a large screen
and an undivided attention span; offering a cramped version invites mistakes in
the ledger.

### Role-aware menus, driven by the login permission set

`POST /api/v1/auth/login/` returns the effective permission codenames **and**
the ABAC scope summary for the active tenant. The client renders from that
list; it never hard-codes role names.

```ts
const NAV: NavItem[] = [
  { href: 'sales/invoices', label: 'nav.invoices', permission: 'sales.invoice.read' },
  { href: 'payroll/runs',   label: 'nav.payroll',  permission: 'payroll.run.read' },
  { href: 'accounting/entries', label: 'nav.journal', permission: 'accounting.journal_entry.read' },
];

// Hide the item, AND guard the route, AND guard the action button.
const visible = NAV.filter((i) => can(i.permission));
```

Three rules:

1. **Hiding a menu item is UX, not security.** Every route is guarded server-
   side; the client gate only stops users walking into a 403.
2. **Render permission-derived UI from the server's list, never from a role
   name.** `role === 'accountant'` breaks the moment a tenant clones a role.
3. **Empty states beat hidden features.** A manager without
   `payroll.run.approve` sees the payroll screen read-only with an explanation,
   not a mysteriously absent tab.

The permission set is refreshed on every token refresh, so a revoked role takes
effect within one access-token lifetime (5 minutes) rather than at next login.

---

## 10. Performance

### List virtualisation

Ledger, journal-line, trial-balance, stock-movement and payroll-register views
routinely render **10k–500k rows**. Mounting 50,000 `<tr>` elements costs
hundreds of MB and locks the main thread for seconds.

- Web: **TanStack Virtual** over a windowed, cursor-paginated query
  (`useInfiniteQuery`), fixed row height so the scrollbar is honest, sticky
  header + sticky totals row.
- Mobile: **FlashList** (`estimatedItemSize` set, `keyExtractor` stable).
- Cursor pagination (see `TenantCursorPagination`) is what makes deep scrolling
  viable: `OFFSET 400000` is a sequential scan, and rows shift under the user
  as new entries are posted.
- Filters are **server-side**. "Filter 200k rows in the browser" means shipping
  200k rows.

### Server-driven aggregates

**Totals, subtotals, balances, aging buckets, KPI tiles and chart series are
computed by the API, never in the client.** Three reasons, in order of
severity: (1) a client can only aggregate the page it has, so a footer total
over a virtualised list is *wrong*; (2) client arithmetic re-opens the float
question of §2; (3) the server has indexes and a read replica.

The list endpoints therefore return an `aggregates` block alongside `results`,
and heavy reports return a job id (`REPORT_SYNC_ROW_LIMIT = 20_000`) with the
result delivered as a file or over the WebSocket.

### Code splitting

- Route-level splitting per module (Next.js does this per segment).
- `dynamic(() => import(...), { ssr: false })` for heavy, rarely-used widgets:
  the charting library, the PDF viewer, the reconciliation matcher, the rich
  text editor, the CSV mapper.
- `decimal.js` is small (~32 kB) and used everywhere — keep it in the main
  bundle; lazy-loading the money library is a false economy.
- Server Components for read-only shells (report headers, static reference
  lists) so they never enter the client bundle.
- Budget enforced in CI: **initial JS ≤ 200 kB gzipped** per route.
- Mobile: Hermes, RAM bundles, `expo-updates` for OTA of JS-only fixes.

### i18n, Arabic and RTL

- Web: **next-intl** with message catalogues per locale and per module,
  loaded per route segment. Mobile: **i18next** + `expo-localization`.
- The document direction is set once, at the layout root, from the locale:
  `<html lang={locale} dir={isRtl(locale) ? 'rtl' : 'ltr'}>`. The server's
  `RTL_LANGUAGES` list (`config/settings/base.py`) is exposed via
  `/api/v1/i18n/` so the two clients cannot disagree about which locales mirror.
- Mobile: `I18nManager.forceRTL(true)` requires an **app reload** to take
  effect — the language switcher must say so and restart cleanly, or the user
  gets a half-mirrored screen.

**Layout mirroring rules:**

| Rule | Detail |
|---|---|
| Use logical properties everywhere | `margin-inline-start`, `padding-inline-end`, `inset-inline-start`, `text-align: start`. Never `margin-left` in a shared component. Tailwind: `ms-*`/`me-*`/`ps-*`/`pe-*`, never `ml-*`/`pl-*` |
| Mirror **direction**, not **meaning** | Back/forward arrows, chevrons, drawer slide-in, progress bars mirror. Clocks, media play/pause/seek, checkmarks, logos and **chart axes for time series** do NOT |
| **Numbers and amounts stay LTR** | Wrap in `<bdi>` / `direction: ltr; unicode-bidi: isolate`. Without isolation, `-1,234.56 EGP` renders with the minus sign on the wrong end in an Arabic paragraph — a *sign error on a financial figure* |
| Tables in RTL | Column order mirrors; the amount column stays right-aligned relative to reading order (`text-align: end` for labels, `start`/`end` chosen per column type) |
| Digits | ASCII digits in inputs and in transport. Arabic-Indic digits are a display-only choice (`numberingSystem` in Intl), never in a value that will be parsed |
| Fonts | An Arabic-capable face (Cairo / IBM Plex Sans Arabic) with correct shaping; test with tashkeel and long ligatures. Subset per locale |
| Dates | `Intl.DateTimeFormat` with the tenant's calendar preference (Gregorian / Umm al-Qura); **store and transmit ISO-8601 Gregorian UTC always** |
| Icons | Only the ~15 directional icons get an `rtl:-scale-x-100`; maintain an explicit allow-list rather than mirroring everything |
| Testing | Visual-regression snapshots run in both `ltr` and `rtl`. A pseudo-locale (`ar-XA`) with 40 % text expansion catches truncation before an Arabic speaker has to |

---

## 11. Testing strategy

| Layer | Tool | What it must prove | Fails the build on |
|---|---|---|---|
| **Money / domain (`packages/domain`)** | Vitest + **fast-check** (property-based) | `formatMoney` never loses a minor unit; `allocate` sums exactly to the total for any split; `addMoney` rejects mixed currencies; parsing an API string round-trips byte-for-byte. Golden vectors shared with the backend's hypothesis tests | Any mismatch with the backend fixtures |
| **Type contract** | `tsc --noEmit` + schema regeneration diff | Generated types match the committed OpenAPI schema | Any drift between API and client types |
| **api-client** | Vitest + **MSW** | Idempotency-Key is stable across retries; 401 triggers exactly one refresh under N parallel requests; retries stop on 4xx; errors normalise | — |
| **Query layer** | Vitest + MSW + React Testing Library | The invalidation map is honoured: "issue invoice → AR aging refetched" is an actual assertion, not a hope. **Tenant switch leaves zero cached entries** | A cache entry surviving a tenant switch |
| **Components (`packages/ui`)** | RTL + jest-axe + **Storybook** | Accessible names, keyboard paths, focus traps; every story renders in `ltr` and `rtl`, `en` and `ar` | Axe violations at "serious" or above |
| **Forms** | RTL | Zod errors render on the right field; money inputs never produce a `number`; submit is disabled while pending | — |
| **Visual regression** | Chromatic / Playwright screenshots | LTR + RTL, light/dark, `en` + `ar`, at 3 viewports | Unreviewed pixel diff |
| **E2E web** | **Playwright** against a seeded backend | The money-critical journeys end to end: create customer → issue invoice → record partial payment → verify AR aging **and the trial balance still balances**; run payroll → post → verify GL. Also: a user in two tenants switches and sees no data from the first | Any journey failure |
| **E2E mobile** | **Maestro** (+ Detox for gestures) | Offline expense capture → airplane mode → reconnect → **exactly one** expense on the server (the idempotency proof); biometric unlock; check-in with mocked GPS | Duplicate row after replay |
| **Offline/outbox** | Vitest with a fake clock + fake network | Ordering under partial failure; backoff; 4xx parks instead of looping; the queue survives a cold app restart | — |
| **Performance** | Lighthouse CI + a 50k-row render benchmark | Route budget ≤ 200 kB gz; virtualised list stays ≥ 55 fps while scrolling | Budget regression > 10 % |
| **Accessibility** | axe + manual screen-reader pass per release | WCAG 2.2 AA; the ledger table is navigable by keyboard alone | Serious/critical violations |

Coverage targets: **`packages/domain` ≥ 95 %** (it is pure logic and it is the
money), api-client ≥ 90 %, feature code ≥ 70 %. Coverage on UI components is a
weak signal — the Playwright journeys and the visual snapshots are what
actually protect the product.

---

## 12. Non-negotiables, one screen

1. Money is a **string** in transport, a `Money` in memory, `decimal.js` in
   arithmetic. Never a `number`.
2. **`tenantId` is the first segment of every query key**, and a tenant switch
   calls `queryClient.clear()`.
3. **Nothing that touches the ledger is optimistic, and nothing that touches
   the ledger works offline.**
4. Every offline mutation carries a **client-generated `Idempotency-Key`,
   created once and reused on every retry**.
5. Tokens: **httpOnly cookie** (web) / **secure-store** (mobile).
   Never `localStorage`.
6. **Types and Zod schemas are generated from the OpenAPI schema**; CI fails on
   drift.
7. **Totals come from the server.** The client displays; it does not decide.
8. Every shared component uses **logical CSS properties** and renders correctly
   in RTL.
