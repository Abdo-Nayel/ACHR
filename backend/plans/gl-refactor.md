# General Ledger refactor + reference-chart port + frontend

Branch `feature/gl-refactor-and-frontend` off `refactor/backend-clean-code`.
Evolves ACHR's GL in place (keeps `post_entry()` and the nine integrations),
adopts the reference GL's 5-level coded chart in English, and rebuilds the GL
screens in ACHR's frontend with ACHR's own theme.

## Backend phases (done, each tested + pushed)

| Phase | What | Proof |
|---|---|---|
| G0 | GL service facade (`apps/accounting/services/__init__.py`) — the six external callers import the public GL from here, not `services.posting`. Fiscal-period state machine extracted to `services/periods.py`. | 327 tests |
| G1 | `Account` gains `level`/`segment_code`/`full_code` (positional coding), `income_category`, `requires_party`, `normal_balance_override` — one additive migration. `services/coding.py`: widths (4,4,3,2,4), `compute_full_code`/`max_sibling_code`, the hierarchy rules (postable ⇔ level 5), `allocate_account` under a parent lock. | 340 tests (+13 coding) |
| G2 | English 5-level chart (`apps/accounting/chart/english_chart.py`) — ported from the reference `default_eg.json`, translated, extended to carry all 30 `system_key` roles. `seed_chart_of_accounts` rewritten to build it; ~200 lines of the old country-code machinery removed. | 343 tests (+3), `seed_demo_tenant` posts end-to-end, 29/29 core invariants |
| G3 | Add-account API: `POST /accounts/` allocates the segmented code server-side (client sends parent/name/side, never a code); `code`/`level`/`full_code`/section/postability read-only; no re-parenting; `/accounts/stats/`. | 349 tests (+6) |

The GL cycle is proven working on the new chart: `seed_demo_tenant` posts an
invoice, payment, expense and payroll run; `verify_core_invariants` passes 29/29
as the non-owner `erp_app` role.

## Frontend phases (G4–G6, in `frontend/app.js` + `index.html`)

Built with ACHR's existing patterns (`api`/`go`/`V`/`simple`/`openForm`, the
`VIEWS` registry) and design tokens only (indigo `--acc`, `--panel*`, `--ok/
--warn/--dang`, `--r`, `--sh*`, Inter) — English/LTR, light+dark.

- **G4 Chart of accounts** — a hierarchical tree (`/accounts/tree/`): `L{level}`
  badges, dotted codes, dr/cr tags on leaves, expand/collapse, search; add-account
  modal (name + side, server-allocated code) mirroring the reference's top-down
  coding; edit/archive; per-level stats bar.
- **G5 Manual journal** — a debit/credit line grid (account/party/currency/rate/
  debit/credit), live balance that gates *Post*, draft → post → reverse via the
  `journal-entries` API.
- **G6 Reports** — Trial balance, Income statement, Balance sheet (ACHR's
  `/reporting/*` endpoints) and per-account General ledger.

## G7 — hardening + end-to-end
- Verified the ledger is sound on the new chart (349 tests + 29 invariants).
- End-to-end browser walk: seed → chart tree → add account → post a manual
  journal → see it in the four reports, in ACHR's theme, in English.
- **Deferred (documented, not done):** decomposing `post_entry` into named
  collaborators is a readability-only refactor of an already-tested core; left
  out under "ship the smallest correct thing". The draft-line `base_debit` in
  `serializers._write_lines` is transient — the `post` action rebuilds the draft
  through `post_entry` (correct FX/quantization) and voids the draft — so it is
  not a posted-ledger drift.
