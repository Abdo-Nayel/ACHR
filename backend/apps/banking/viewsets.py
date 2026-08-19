"""
Banking viewsets: statements in, matches made, sessions closed.

ABAC note
---------
``config/permissions.json`` defines no ``ScopeRule`` for ``bank_account``,
``statement`` or ``reconciliation``, and ``build_scope_q`` fails closed — a
resource with no rule yields ``DENY_ALL``. Bank data has no per-actor
dimension: either a role may see the company's bank activity or it may not,
which is an RBAC question (``banking.reconciliation.read``). These viewsets
therefore opt out of the scope filter via :class:`RbacOnlyQuerysetMixin` and
keep the tenant boundary that ``TenantManager`` enforces.
"""

from __future__ import annotations

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.banking.models import (
    BankAccount,
    BankStatement,
    BankTransaction,
    ReconciliationMatch,
    ReconciliationSession,
)
from apps.banking.serializers import (
    AutoReconcileSerializer,
    BankAccountSerializer,
    BankStatementSerializer,
    BankTransactionSerializer,
    CandidateSerializer,
    MatchRequestSerializer,
    ReconciliationMatchSerializer,
    ReconciliationSessionSerializer,
    StatementImportSerializer,
    UnmatchRequestSerializer,
)
from apps.core.pagination import SmallPagePagination
from apps.core.serializers import TransitionSerializer
from apps.core.viewsets import (
    ReadOnlyTenantViewSet,
    TenantModelViewSet,
    raise_as_api_error,
)


class RbacOnlyQuerysetMixin:
    """Tenant-scoped and RBAC-guarded, but not ABAC-filtered.

    See the module docstring. This bypasses
    :class:`apps.iam.permissions.ScopedQuerysetMixin` only; the tenant filter
    still comes from ``TenantManager`` and returns ``.none()`` when no tenant
    is bound.
    """

    def get_queryset(self):
        # ``self.queryset.model._default_manager.all()``, never
        # ``self.queryset.all()``. The class attribute was evaluated at import
        # time, with no tenant bound, so ``TenantManager`` failed closed and
        # froze an empty queryset for the life of the process — ``.all()`` on
        # ``.none()`` is still nothing. The symptom is the worst kind: HTTP
        # 200, a well-formed envelope and an empty ``results`` array on every
        # request, with no error anywhere. Re-deriving from the manager runs it
        # inside the request, where the tenant actually is bound. This mirrors
        # ``apps.core.viewsets.TenantViewSetMixin.get_queryset``, which
        # documents the same trap.
        queryset = self.queryset.model._default_manager.all()
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.prefetch_related:
            queryset = queryset.prefetch_related(*self.prefetch_related)
        ordering = getattr(self, "ordering", None)
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset


class BankAccountViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Bank accounts and their mirrored GL account."""

    permission_domain = "banking"
    resource = "bank_account"
    queryset = BankAccount.objects.all()
    serializer_class = BankAccountSerializer
    select_related = ("ledger_account",)
    pagination_class = SmallPagePagination
    search_fields = ("name", "bank_name", "iban")
    filterset_fields = ("is_active", "currency", "feed_provider")
    extra_permissions = {"DELETE": ["banking.bank_account.archive"]}


class BankStatementViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Imported statement files.

    Statements arrive through ``POST /bank-statements/import/`` rather than a
    plain create, because the import is the thing that has to be idempotent.
    """

    permission_domain = "banking"
    resource = "statement"
    queryset = BankStatement.objects.all()
    serializer_class = BankStatementSerializer
    select_related = ("bank_account", "imported_by")
    filterset_fields = ("bank_account", "import_source", "statement_date")
    extra_permissions = {
        "POST": ["banking.statement.import"],
        "PUT": ["banking.statement.import"],
        "PATCH": ["banking.statement.import"],
        "DELETE": ["banking.statement.import"],
        "import_statement": ["banking.statement.import"],
    }

    @action(detail=False, methods=["post"], url_path="import")
    def import_statement(self, request):
        """Register an uploaded statement file. Deduped on ``file_checksum``.

        Why a repeat import is 200 with ``already_imported: true`` and not 409
        --------------------------------------------------------------------
        Re-importing the same file is not an error; it is the single most
        common thing a bookkeeper does. The bank portal is refreshed, the same
        month is downloaded again, and the file is dropped on the importer
        "just in case anything new came in". If that returned 4xx, the user
        would see a red banner for a correct, harmless action — and the
        clients that do bulk imports would have to distinguish "this file is a
        duplicate" from "this file is malformed" by parsing an error string.

        Returning the *existing* statement with a flag makes the operation
        idempotent in the sense that matters: the caller ends up holding the
        id of the statement that represents this file, whether it was created
        just now or last Tuesday, and can navigate straight to it. The
        alternative failure mode — creating a second statement and therefore a
        second copy of every transaction — is the one that actually hurts:
        duplicated bank lines double the apparent cash movement, every one of
        them looks matchable, and the reconciliation difference that results is
        discovered weeks later.

        The checksum is over the raw file bytes, so a file that differs by a
        single byte (a genuinely updated download) is a different statement and
        imports normally.
        """
        body = StatementImportSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payload = dict(body.validated_data)

        checksum = payload["file_checksum"].strip().lower()
        existing = BankStatement.objects.filter(file_checksum=checksum).first()
        if existing is not None:
            data = self.get_serializer(existing).data
            return Response(
                {"already_imported": True, "statement": data},
                status=status.HTTP_200_OK,
            )

        account = BankAccount.objects.filter(pk=payload["bank_account"]).first()
        if account is None:
            from rest_framework.exceptions import ValidationError as DRFValidationError

            raise DRFValidationError({"bank_account": "Unknown bank account."})

        statement = BankStatement.objects.create(
            tenant_id=getattr(request, "tenant_id", None) or account.tenant_id,
            bank_account=account,
            statement_number=payload.get("statement_number", ""),
            statement_date=payload["statement_date"],
            period_start=payload.get("period_start"),
            period_end=payload.get("period_end"),
            currency=payload.get("currency") or account.currency,
            opening_balance=payload.get("opening_balance") or 0,
            closing_balance=payload.get("closing_balance") or 0,
            import_source=payload["import_source"],
            original_filename=payload.get("original_filename", ""),
            file_checksum=checksum,
            line_count=payload.get("line_count", 0),
            imported_by_id=request.user.id,
            created_by_id=request.user.id,
            updated_by_id=request.user.id,
        )
        return Response(
            {"already_imported": False, "statement": self.get_serializer(statement).data},
            status=status.HTTP_201_CREATED,
        )


class BankTransactionViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """The reconciliation worklist.

    ``resource = "reconciliation"`` because every write on a bank line is a
    reconciliation act: the permissions that matter are
    ``banking.reconciliation.match`` and ``.read``, not a per-transaction CRUD
    grant.
    """

    permission_domain = "banking"
    resource = "reconciliation"
    queryset = BankTransaction.objects.all()
    serializer_class = BankTransactionSerializer
    select_related = ("bank_account", "statement")
    filterset_fields = ("bank_account", "statement", "status", "transaction_date")
    search_fields = ("description", "reference", "counterparty_name")
    extra_permissions = {
        "suggested_matches": ["banking.reconciliation.read"],
        "match": ["banking.reconciliation.match"],
        "unmatch": ["banking.reconciliation.match"],
        "ignore": ["banking.reconciliation.match"],
    }

    @action(detail=True, methods=["get"], url_path="suggested-matches")
    def suggested_matches(self, request, pk=None):
        """Ranked ledger lines that could explain this bank line.

        Read-only and side-effect free: it suggests, it does not apply. The
        client shows the confidence and the reasons so a reviewer can see
        *why* a line was proposed — "amount exact, reference hit, 2 days
        apart" — rather than being asked to trust a number.
        """
        from apps.banking.services.reconciliation import suggest_matches

        txn = self.get_object()
        limit = min(int(request.query_params.get("limit") or 10), 50)
        try:
            candidates = suggest_matches(txn, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response(
            {
                "bank_transaction": str(txn.pk),
                "count": len(candidates),
                "candidates": CandidateSerializer(candidates, many=True).data,
            }
        )

    @action(detail=True, methods=["post"], url_path="match")
    def match(self, request, pk=None):
        """Confirm a link to one ledger line."""
        from apps.banking.services.reconciliation import confirm_match

        txn = self.get_object()
        body = MatchRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payload = body.validated_data

        session = None
        if payload.get("session"):
            session = ReconciliationSession.objects.filter(
                pk=payload["session"]
            ).first()

        try:
            with transaction.atomic():
                match = confirm_match(
                    bank_transaction=txn,
                    journal_line=payload["journal_line"],
                    matched_amount=payload.get("matched_amount"),
                    session=session,
                    user_id=request.user.id,
                    notes=payload.get("notes", ""),
                )
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        txn.refresh_from_db()
        return Response(
            {
                "match": ReconciliationMatchSerializer(
                    match, context=self.get_serializer_context()
                ).data,
                "bank_transaction": self.get_serializer(txn).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="unmatch")
    def unmatch(self, request, pk=None):
        """Release one match, or every match on this line.

        Undoing must stay cheap. If it were expensive or blocked, reviewers
        would work around a wrong match by posting an adjusting journal entry,
        and *that* corrupts the ledger — far worse than deleting an assertion
        that no financial record depends on.
        """
        from apps.banking.services.reconciliation import unmatch as unmatch_service

        txn = self.get_object()
        body = UnmatchRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payload = body.validated_data

        matches = ReconciliationMatch.objects.filter(bank_transaction=txn)
        if payload.get("match"):
            matches = matches.filter(pk=payload["match"])
        matches = list(matches)
        if not matches:
            from apps.core.exceptions import DomainError

            raise DomainError("There is no match to release on this transaction.")

        try:
            with transaction.atomic():
                for match in matches:
                    unmatch_service(
                        match,
                        reason=payload.get("reason", ""),
                        user_id=request.user.id,
                    )
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        txn.refresh_from_db()
        return Response(
            {"released": len(matches), "bank_transaction": self.get_serializer(txn).data}
        )

    @action(detail=True, methods=["post"], url_path="ignore")
    def ignore(self, request, pk=None):
        """Mark a line as deliberately not reconcilable (bank fee reversal,
        an internal transfer already netted, a line the bank duplicated).

        IGNORED is a decision with an author, not a delete: the row stays on
        the statement, and the note is what the next person reads when they
        ask why a line has no match.
        """
        txn = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reason = body.validated_reason()

        try:
            with transaction.atomic():
                locked = (
                    BankTransaction.objects.select_for_update().get(pk=txn.pk)
                )
                locked.assert_can_transition(BankTransaction.Status.IGNORED)
                locked.status = BankTransaction.Status.IGNORED
                if reason:
                    locked.notes = f"{locked.notes}\n{reason}".strip()[:500]
                locked.updated_by_id = request.user.id
                locked.save(update_fields=["status", "notes", "updated_by", "updated_at"])
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response(self.get_serializer(locked).data)


class ReconciliationMatchViewSet(RbacOnlyQuerysetMixin, ReadOnlyTenantViewSet):
    """Read-only listing of confirmed links.

    Creating one goes through ``POST /bank-transactions/{id}/match`` and
    releasing one through ``.../unmatch``: both need the row lock and the
    residual arithmetic that live in the service, and neither is expressible
    as a plain POST/DELETE on this collection.
    """

    permission_domain = "banking"
    resource = "reconciliation"
    queryset = ReconciliationMatch.objects.all()
    serializer_class = ReconciliationMatchSerializer
    select_related = ("bank_transaction", "journal_line", "session")
    filterset_fields = ("bank_transaction", "session", "match_type")


class ReconciliationSessionViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """A period's reconciliation of one bank account."""

    permission_domain = "banking"
    resource = "reconciliation"
    queryset = ReconciliationSession.objects.all()
    serializer_class = ReconciliationSessionSerializer
    select_related = ("bank_account", "closed_by")
    filterset_fields = ("bank_account", "status", "period_end")
    extra_permissions = {
        "auto_reconcile": ["banking.reconciliation.match"],
        "close": ["banking.reconciliation.complete"],
    }

    @action(detail=True, methods=["post"], url_path="auto-reconcile")
    def auto_reconcile(self, request, pk=None):
        """Apply only the matches we are certain about; flag the rest.

        Anything under the threshold becomes a *suggestion* for a human. An
        unmatched line is a visible five-minute task; a wrongly matched line
        is an invisible error that closes the period and surfaces months later
        against a customer who already paid.
        """
        from apps.banking.services.reconciliation import auto_reconcile, refresh_session

        session = self.get_object()
        body = AutoReconcileSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payload = body.validated_data

        kwargs = {
            "tenant_id": session.tenant_id,
            "bank_account": session.bank_account_id,
            "session": session,
            "user_id": request.user.id,
        }
        if payload.get("threshold") is not None:
            kwargs["threshold"] = payload["threshold"]
        if payload.get("limit") is not None:
            kwargs["limit"] = payload["limit"]

        try:
            # auto_reconcile deliberately commits each line in its own
            # transaction so one bad line cannot roll back an hour of correct
            # work; it is therefore NOT wrapped in an outer atomic here.
            summary = auto_reconcile(**kwargs)
            session = refresh_session(session, user_id=request.user.id)
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response({"summary": summary, "session": self.get_serializer(session).data})

    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None):
        """Sign off the reconciliation. Refuses when it does not balance.

        The refusal carries the difference, the currency and the number of
        unexplained lines, because "cannot close" without the number is a dead
        end: the reviewer's next action is to go and find 12.40, and they can
        only do that if we tell them it is 12.40.
        """
        from apps.banking.services.reconciliation import close_session

        session = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                closed = close_session(
                    session, user_id=request.user.id, notes=body.validated_reason()
                )
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "UnbalancedSession":
                from rest_framework import status as http_status

                from apps.core.exceptions import DomainError

                # close_session already refreshed the row inside its own
                # transaction; re-read so the number we quote is the number the
                # service refused on, not the stale one the client held.
                session.refresh_from_db()
                error = DomainError(
                    detail=[
                        f"This reconciliation cannot be closed: the statement "
                        f"and the ledger differ by {session.difference} "
                        f"{session.currency}, with {session.unmatched_count} "
                        f"transaction(s) still unexplained. Match, post or "
                        f"explicitly write off the difference first."
                    ]
                )
                error.default_code = "reconciliation_unbalanced"
                error.status_code = http_status.HTTP_409_CONFLICT
                raise error from exc
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response(self.get_serializer(closed).data)
