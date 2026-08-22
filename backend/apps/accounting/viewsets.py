"""
General-ledger endpoints.

Everything that changes the state of the ledger is a POST to a sub-resource
that calls :mod:`apps.accounting.services.posting`. No handler in this module
contains accounting logic; they translate HTTP into a service call and the
service's exceptions back into the API error vocabulary. That separation is
what lets the same transitions run from a Celery task and a management
command, where there is no request to read a header from.

Idempotency
-----------
``post``, ``void`` and ``reverse`` all honour the ``Idempotency-Key`` header.
A replay returns the object the first call produced, with
``Idempotency-Replayed: true``, instead of running the service again. Without
it, a mobile client whose request times out after the server committed will
retry and post the entry twice — and a duplicated journal entry is not
something the user can undo, it is something an accountant has to reverse.

Note the replay is keyed on the *result*, not on the request object. Reversing
an entry produces a **different** entry (the mirror), so a replay must return
the mirror; a check that insisted the result equal the object the action was
invoked on would 409 on every legitimate reverse retry.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounting.models import (
    Account,
    ExchangeRate,
    FiscalPeriod,
    FiscalYear,
    Journal,
    JournalEntry,
    JournalLine,
    TaxRate,
)
from apps.accounting.serializers import (
    AccountSerializer,
    AccountTreeSerializer,
    ExchangeRateSerializer,
    FiscalPeriodSerializer,
    FiscalYearSerializer,
    JournalEntrySerializer,
    JournalSerializer,
    LedgerLineSerializer,
    TaxRateSerializer,
)
from apps.core.exceptions import DomainError, IllegalTransitionError
from apps.core.fields import ZERO
from apps.core.pagination import LedgerCursorPagination, SmallPagePagination
from apps.core.serializers import (
    ReasonRequiredTransitionSerializer,
    TransitionSerializer,
)
from apps.accounting.services.periods import transition_period
from apps.core.viewsets import (
    IdempotentActionMixin,
    ReadOnlyTenantViewSet,
    TenantModelViewSet,
)
from apps.iam.permissions import require_reauth

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------

class AccountViewSet(IdempotentActionMixin, TenantModelViewSet):
    """The chart of accounts, its tree view, and per-account ledgers."""

    permission_domain = "accounting"
    resource = "account"
    queryset = Account.objects.all()
    serializer_class = AccountSerializer
    select_related = ("parent",)
    # Page numbers, not cursors: the chart of accounts is a bounded set a user
    # expects a page count for, and it does not grow with transaction volume.
    pagination_class = SmallPagePagination
    filterset_fields = ("type", "is_active", "is_postable", "is_reconcilable", "parent")
    search_fields = ("code", "name", "description")
    ordering_fields = ("code", "name", "type", "created_at")
    ordering = ("code",)
    extra_permissions = {
        "tree": ["accounting.account.read"],
        "stats": ["accounting.account.read"],
        "ledger": ["accounting.account.read"],
        "archive": ["accounting.account.archive"],
    }

    # -- stats --------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        """Per-level account counts, for the chart UI's level bar."""
        from django.db.models import Count

        rows = (
            self.get_queryset()
            .values("level")
            .order_by("level")
            .annotate(count=Count("id"))
        )
        return Response({"levels": {str(r["level"]): r["count"] for r in rows}})

    # -- tree ---------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path="tree")
    def tree(self, request):
        """The chart of accounts as a nested tree, rooted at the parentless nodes.

        Deliberately unpaginated. A tree cut in half at an arbitrary row is not
        a tree — the client cannot render a partial hierarchy, and paging by
        row would split children away from their parent. The set is bounded
        (charts run to hundreds of accounts, not millions), which is exactly
        the condition that makes returning it whole safe.
        """
        queryset = self.filter_queryset(self.get_queryset())
        # One prefetch level per tree level would be a query per level; a
        # single prefetch of ``children`` plus the recursion in the serializer
        # is what keeps this at a bounded number of queries.
        roots = queryset.filter(parent__isnull=True).prefetch_related(
            "children__children__children"
        )
        serializer = AccountTreeSerializer(
            roots, many=True, context=self.get_serializer_context()
        )
        return Response({"results": serializer.data})

    # -- ledger -------------------------------------------------------------

    @action(detail=True, methods=["get"], url_path="ledger",
            pagination_class=LedgerCursorPagination)
    def ledger(self, request, pk=None):
        """Posted journal lines for this account, with a running balance.

        Only POSTED entries appear. Draft entries are unbalanced by design and
        carry no number; including them would produce a ledger whose closing
        balance does not equal ``cached_balance`` and cannot be tied to the
        trial balance.

        The running balance is computed for the page, not for the row
        ---------------------------------------------------------------
        A running balance is a property of a sequence. Computing it per row
        would mean re-summing the account's whole history once per row; naively
        restarting at zero on page two would give every page a balance that
        disagrees with the previous one. So: sum everything strictly older than
        the first row of the page once (one aggregate query), then walk the
        page accumulating. ``opening_balance`` and ``closing_balance`` are
        returned alongside the rows so a reader can check the page adds up.

        The "strictly older" comparison is the full ``(entry_date, created_at,
        id)`` tuple, matching the paginator's ordering exactly. Comparing on
        the date alone would double-count or drop the lines that share the
        boundary date.
        """
        account = self.get_object()
        queryset = (
            JournalLine.objects.filter(
                account=account, entry__status=JournalEntry.Status.POSTED
            )
            .select_related("entry", "entry__journal")
            # The annotation name must equal ``LedgerCursorPagination.ordering[0]``
            # (``entry_date``): the paginator reads that attribute off each
            # instance to build the cursor, and a mismatch fails at request time.
            .annotate(entry_date=F("entry__entry_date"))
        )

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            queryset = queryset.filter(entry__entry_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(entry__entry_date__lte=date_to)

        page = self.paginate_queryset(queryset)
        rows = list(page if page is not None else queryset)
        # The paginator orders newest-first; a running balance accumulates
        # oldest-first.
        oldest_first = list(reversed(rows))

        opening = self._opening_balance(account, queryset, oldest_first)
        balances: dict[Any, Decimal] = {}
        running = opening
        for line in oldest_first:
            running += self._movement(account, line)
            balances[line.pk] = running

        context = self.get_serializer_context()
        context["running_balances"] = balances
        serializer = LedgerLineSerializer(rows, many=True, context=context)

        if page is None:
            payload: dict[str, Any] = {"results": serializer.data}
            response = Response(payload)
        else:
            response = self.paginator.get_paginated_response(serializer.data)

        response.data["account"] = {
            "id": str(account.id),
            "code": account.code,
            "name": account.name,
            "normal_balance": account.normal_balance,
        }
        response.data["opening_balance"] = f"{opening:f}"
        response.data["closing_balance"] = f"{running:f}"
        return response

    @staticmethod
    def _movement(account: Account, line: JournalLine) -> Decimal:
        """Signed effect of a line on its account's balance.

        Direction comes from the account *type* via ``NORMAL_BALANCE``, never
        from the caller: an expense account and a liability account with the
        same debit move in opposite directions, and asking a caller to say
        which is asking for a sign error.
        """
        if account.increases_on_debit:
            return (line.debit or ZERO) - (line.credit or ZERO)
        return (line.credit or ZERO) - (line.debit or ZERO)

    def _opening_balance(self, account: Account, queryset, oldest_first: list) -> Decimal:
        if not oldest_first:
            return ZERO
        boundary = oldest_first[0]
        older = queryset.filter(
            Q(entry__entry_date__lt=boundary.entry.entry_date)
            | Q(entry__entry_date=boundary.entry.entry_date,
                created_at__lt=boundary.created_at)
            | Q(entry__entry_date=boundary.entry.entry_date,
                created_at=boundary.created_at, id__lt=boundary.id)
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        debit = older["debit"] or ZERO
        credit = older["credit"] or ZERO
        return (debit - credit) if account.increases_on_debit else (credit - debit)

    # -- archive ------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None):
        """Deactivate an account so nothing further can be posted to it.

        Not a DELETE and not a writable ``is_active`` flag, for two different
        reasons. Deleting is impossible — journal lines reference the account
        with ``PROTECT``, and they must, because a ledger whose accounts can
        vanish cannot be reprinted. And archiving is refused while the account
        still carries a balance: an archived account with a non-zero balance
        disappears from the account picker but keeps appearing in the trial
        balance, and nobody can work out where the figure comes from. Move the
        balance out with a journal entry first.

        System accounts (those with a ``system_key``) are refused outright:
        automated postings resolve them by key, and archiving one breaks
        invoice issue, payroll posting and bank reconciliation at once, at the
        moment they next run rather than here where the cause is visible.
        """
        account = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        if account.system_key:
            raise DomainError(
                f"Account {account.code} is a system account "
                f"('{account.system_key}') wired into automated postings and "
                f"cannot be archived. Re-point the system key to another "
                f"account first."
            )
        if not account.is_active:
            return Response(self.get_serializer(account).data)
        if account.cached_balance != ZERO:
            raise DomainError(
                f"Account {account.code} still carries a balance of "
                f"{account.cached_balance}. Post a journal entry moving it to "
                f"another account before archiving, or the balance will remain "
                f"in the trial balance under an account nobody can select."
            )
        if account.children.filter(is_active=True).exists():
            raise DomainError(
                f"Account {account.code} has active child accounts. Archive "
                f"them first — an active child under an archived parent is "
                f"unreachable in the account tree."
            )

        def run(_key: Optional[str]) -> Account:
            Account.all_tenants.filter(pk=account.pk).update(
                is_active=False,
                is_postable=False,
                updated_by_id=getattr(request.user, "id", None),
                updated_at=timezone.now(),
            )
            account.refresh_from_db()
            return account

        return self.run_idempotent(request, transition="archive", run=run)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

class TaxRateViewSet(TenantModelViewSet):
    """Tax rate definitions.

    Writes require ``accounting.tax_rate.manage``, which is a sensitive
    permission and therefore triggers re-authentication: changing a tax rate
    silently restates every future invoice, and a stolen session should not be
    able to do it.
    """

    permission_domain = "accounting"
    resource = "tax_rate"
    queryset = TaxRate.objects.all()
    serializer_class = TaxRateSerializer
    select_related = ("collected_account", "paid_account")
    pagination_class = SmallPagePagination
    filterset_fields = ("is_active", "is_compound", "is_recoverable")
    search_fields = ("name", "code")
    ordering_fields = ("code", "rate", "effective_from")
    ordering = ("code",)
    extra_permissions = {
        "create": ["accounting.tax_rate.manage"],
        "update": ["accounting.tax_rate.manage"],
        "partial_update": ["accounting.tax_rate.manage"],
    }


class FiscalYearViewSet(TenantModelViewSet):
    """Financial years.

    Guarded by the ``accounting.fiscal_period`` permissions: the catalogue in
    ``config/permissions.json`` has no separate ``fiscal_year`` resource, and
    inventing a codename here would produce a permission nobody can ever hold
    (``HasPermission`` denies on a codename that is not in the table).
    """

    permission_domain = "accounting"
    resource = "fiscal_period"
    queryset = FiscalYear.objects.all()
    serializer_class = FiscalYearSerializer
    pagination_class = SmallPagePagination
    filterset_fields = ("status",)
    ordering_fields = ("start_date", "name")
    ordering = ("-start_date",)


class FiscalPeriodViewSet(IdempotentActionMixin, TenantModelViewSet):
    """Fiscal periods and the three moves that lock and unlock the books.

    The moves themselves live in ``apps.accounting.services.periods``
    (``transition_period``); this viewset is a thin adapter over it. The two
    properties that matter, kept intact by the service:

    * ``SELECT ... FOR UPDATE`` on the period row. ``post_entry`` takes
      ``FOR SHARE`` on the same row, so a close and a concurrent posting are
      mutually exclusive. Without the lock, a close starting at T and a post
      starting at T+1ms both read ``status='open'`` and the entry lands in a
      period that is closed by the time it commits.
    * A refusal to close over unposted drafts. A draft in a closed period can
      never be posted (the period rejects it) and can never be corrected in
      place — it becomes a permanently stranded document.
    """

    permission_domain = "accounting"
    resource = "fiscal_period"
    queryset = FiscalPeriod.objects.all()
    serializer_class = FiscalPeriodSerializer
    select_related = ("fiscal_year", "closed_by")
    pagination_class = SmallPagePagination
    filterset_fields = ("status", "fiscal_year")
    ordering_fields = ("start_date", "name")
    ordering = ("start_date",)
    extra_permissions = {
        "close": ["accounting.fiscal_period.close"],
        "soft_close": ["accounting.fiscal_period.close"],
        "reopen": ["accounting.fiscal_period.reopen"],
    }

    # -- actions ------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None):
        """Lock the period. Nothing may be posted into it afterwards.

        The correction mechanism after a close is a reversing entry dated in
        the current open period — never an edit, and never a reopen unless a
        genuine error is being fixed under supervision.
        """
        period = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self.run_idempotent(
            request,
            transition="close",
            run=lambda _key: transition_period(
                period.pk, tenant_id=period.tenant_id, target=FiscalPeriod.Status.CLOSED,
                user_id=getattr(request.user, "id", None),
                reason=body.validated_reason(),
            ),
        )

    @action(detail=True, methods=["post"], url_path="soft-close")
    def soft_close(self, request, pk=None):
        """Stop ordinary posting while the accountant finishes adjustments.

        Only holders of ``accounting.period.post_to_soft_closed`` may still
        post. This state exists so that month-end does not have to be an
        instant, and so nobody leaves periods open "just in case" — which is
        how a prior period's filed figures silently change.
        """
        period = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self.run_idempotent(
            request,
            transition="soft_close",
            run=lambda _key: transition_period(
                period.pk, tenant_id=period.tenant_id, target=FiscalPeriod.Status.SOFT_CLOSED,
                user_id=getattr(request.user, "id", None),
                reason=body.validated_reason(),
            ),
        )

    @require_reauth
    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, pk=None):
        """Unlock a closed period. Sensitive: requires re-authentication.

        ``accounting.fiscal_period.reopen`` is flagged ``is_sensitive`` in the
        catalogue, so ``HasPermission`` already demands a fresh
        ``X-Reauth-Token``. The decorator repeats the requirement at the call
        site so a reviewer reading this handler can see it without having to
        cross-reference ``config/permissions.json``.

        A reason is mandatory. Reopening a closed period changes figures that
        have already been reported, and "why" is the only part of that event
        an auditor cannot reconstruct from the data.
        """
        period = self.get_object()
        body = ReasonRequiredTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self.run_idempotent(
            request,
            transition="reopen",
            run=lambda _key: transition_period(
                period.pk, tenant_id=period.tenant_id, target=FiscalPeriod.Status.OPEN,
                user_id=getattr(request.user, "id", None),
                reason=body.validated_reason(),
            ),
        )


class JournalViewSet(TenantModelViewSet):
    """Books of original entry."""

    permission_domain = "accounting"
    resource = "journal"
    queryset = Journal.objects.all()
    serializer_class = JournalSerializer
    select_related = ("default_account",)
    pagination_class = SmallPagePagination
    filterset_fields = ("kind", "is_active")
    search_fields = ("code", "name")
    ordering_fields = ("code", "name")
    ordering = ("code",)
    extra_permissions = {
        "create": ["accounting.journal.manage"],
        "update": ["accounting.journal.manage"],
        "partial_update": ["accounting.journal.manage"],
    }


class ExchangeRateViewSet(TenantModelViewSet):
    """Per-tenant daily FX rates."""

    permission_domain = "accounting"
    resource = "exchange_rate"
    queryset = ExchangeRate.objects.all()
    serializer_class = ExchangeRateSerializer
    filterset_fields = ("from_currency", "to_currency", "rate_date", "source")
    ordering_fields = ("rate_date",)
    ordering = ("-rate_date",)
    extra_permissions = {
        "create": ["accounting.exchange_rate.manage"],
        "update": ["accounting.exchange_rate.manage"],
        "partial_update": ["accounting.exchange_rate.manage"],
    }


# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------

class JournalEntryViewSet(IdempotentActionMixin, TenantModelViewSet):
    """Journal entries: draft, post, void, reverse.

    A DRAFT entry is an ordinary editable document. Posting it is a separate
    call because posting is not "setting a field": it validates the balance,
    takes a share lock on the fiscal period, allocates a gapless number and
    writes the account balances, all in one transaction. There is no request
    shape that does half of that.
    """

    permission_domain = "accounting"
    resource = "journal_entry"
    queryset = JournalEntry.objects.all()
    serializer_class = JournalEntrySerializer
    select_related = ("journal", "period", "posted_by")
    prefetch_related = ("lines", "lines__account")
    filterset_fields = ("status", "source", "journal", "period", "currency", "entry_date")
    search_fields = ("number", "memo")
    ordering_fields = ("entry_date", "number", "created_at")
    ordering = ("-entry_date", "-number")
    extra_permissions = {
        "post": ["accounting.journal_entry.post"],
        "void": ["accounting.journal_entry.void"],
        "reverse": ["accounting.journal_entry.reverse"],
    }

    # -- post ---------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="post")
    def post(self, request, pk=None):
        """Post a DRAFT entry to the ledger through ``post_entry``.

        Why this produces a *new* row
        -----------------------------
        ``post_entry`` takes an inert
        :class:`~apps.accounting.services.posting.JournalEntryDraft` and
        persists it — it is the single choke point where the balance check,
        the period lock, the number allocation and the FX conversion happen,
        and it deliberately accepts nothing that has a ``save()`` method. So
        posting a stored draft means rebuilding the draft from its lines and
        handing that over. The stored draft row is then voided with a pointer
        to the entry that superseded it, rather than deleted: the whole point
        of ``ImmutableFinancialModel`` is that the trail of what was intended
        survives alongside what was recorded.

        Idempotency
        -----------
        When the caller sends no ``Idempotency-Key``, one is derived —
        ``draft:{id}`` — and passed to ``post_entry``, which returns the
        existing entry rather than posting a second one when the key has
        already been consumed. That makes a retry of this endpoint safe by
        construction rather than by the caller remembering a header.
        """
        from apps.accounting.services.posting import (
            JournalEntryDraft,
            LineDraft,
            post_entry,
            void_entry,
        )

        entry = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        if entry.status != JournalEntry.Status.DRAFT:
            raise IllegalTransitionError(
                f"Journal entry {entry.number or entry.id} is "
                f"{entry.get_status_display().lower()}; only a draft can be "
                f"posted."
            )

        lines = list(entry.lines.all().order_by("line_number"))
        if len(lines) < 2:
            raise DomainError(
                "A journal entry needs at least two lines: a single-sided entry "
                "cannot balance."
            )

        tenant_id = getattr(request, "tenant_id", None) or entry.tenant_id
        user_id = getattr(request.user, "id", None)

        def run(key: Optional[str]) -> JournalEntry:
            draft = JournalEntryDraft(
                journal_code=entry.journal.code,
                entry_date=entry.entry_date,
                currency=entry.currency,
                memo=entry.memo,
                source=entry.source,
                source_document_type="journal_entry",
                source_document_id=entry.id,
                exchange_rate=entry.exchange_rate or Decimal("1"),
                idempotency_key=key or f"draft:{entry.id}",
            )
            for line in lines:
                draft.add(
                    LineDraft(
                        account_id=line.account_id,
                        debit=line.debit,
                        credit=line.credit,
                        description=line.description,
                        partner_type=line.partner_type,
                        partner_id=line.partner_id,
                        project_id=line.project_id,
                        department_id=line.department_id,
                        tax_rate_id=line.tax_rate_id,
                    )
                )
            posted = post_entry(draft, tenant_id=tenant_id, user_id=user_id)

            # Retire the draft. DRAFT -> VOIDED is a legal transition and
            # ``void_entry`` skips the cached-balance unwind for a row that was
            # never posted, so this is a pure bookkeeping move.
            fresh = JournalEntry.all_tenants.filter(pk=entry.pk).first()
            if fresh is not None and fresh.status == JournalEntry.Status.DRAFT:
                void_entry(
                    fresh,
                    reason=f"Superseded by posted entry {posted.number}.",
                    user_id=user_id,
                )
            return posted

        return self.run_idempotent(request, transition="post", run=run)

    # -- void ---------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="void")
    def void(self, request, pk=None):
        """Cancel an entry in place. Legal only while its period is still open.

        The row survives with ``status=VOIDED`` and keeps its number. Deleting
        it would leave a gap in the sequence, and a gap is the first thing a
        tax auditor looks for. A reason is required for the same reason: a
        voided document nobody can explain a year later is a finding.
        """
        from apps.accounting.services.posting import void_entry

        entry = self.get_object()
        body = ReasonRequiredTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        user_id = getattr(request.user, "id", None)

        return self.run_idempotent(
            request,
            transition="void",
            run=lambda _key: void_entry(
                entry, reason=body.validated_reason(), user_id=user_id
            ),
        )

    # -- reverse ------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request, pk=None):
        """Post a mirror entry that cancels this one in a later open period.

        This is the only legal correction once a period has closed: the
        original stays exactly as filed and the books show both the error and
        its fix. The response is the **mirror** entry, not the original —
        that is the document the user now needs to look at.

        ``reversal_date`` defaults to today. Backdating a reversal into the
        period being corrected would defeat the purpose; if that period is
        still open, a void is the right instrument instead.
        """
        from apps.accounting.services.posting import reverse_entry

        entry = self.get_object()
        body = ReasonRequiredTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        raw_date = (request.data or {}).get("reversal_date")
        reversal_date = parse_date(raw_date) if isinstance(raw_date, str) else None
        if raw_date and reversal_date is None:
            raise DomainError("reversal_date must be an ISO date (YYYY-MM-DD).")
        user_id = getattr(request.user, "id", None)

        return self.run_idempotent(
            request,
            transition="reverse",
            run=lambda _key: reverse_entry(
                entry,
                reversal_date=reversal_date,
                reason=body.validated_reason(),
                user_id=user_id,
            ),
        )


__all__ = [
    "AccountViewSet",
    "TaxRateViewSet",
    "FiscalYearViewSet",
    "FiscalPeriodViewSet",
    "JournalViewSet",
    "JournalEntryViewSet",
    "ExchangeRateViewSet",
    "ReadOnlyTenantViewSet",
]
