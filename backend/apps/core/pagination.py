"""
Cursor pagination.

Offset pagination (``?page=42``) is wrong for a ledger for two reasons:

1. **Cost.** ``LIMIT 50 OFFSET 500000`` makes PostgreSQL read and discard half
   a million rows. On a journal-line table it degrades from milliseconds to
   seconds as a tenant's history grows — the customers who paid you longest
   are the ones with the slowest reports.
2. **Correctness.** Rows are being inserted while a user pages. With an
   offset, a new row at the top shifts everything down one and page 2 repeats
   the last row of page 1. Auditors notice duplicated entries.

A cursor keyed on ``(-created_at, -id)`` is O(log n) and stable under
concurrent inserts. ``id`` is the tie-breaker because timestamps collide —
two invoices issued in the same millisecond would otherwise page erratically.
"""

from __future__ import annotations

from collections import OrderedDict

from rest_framework.pagination import CursorPagination, PageNumberPagination
from rest_framework.response import Response


class TenantCursorPagination(CursorPagination):
    page_size = 50
    max_page_size = 200
    page_size_query_param = "page_size"
    ordering = ("-created_at", "-id")
    cursor_query_param = "cursor"

    def get_paginated_response(self, data):
        return Response(
            OrderedDict(
                [
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                    ("page_size", self.get_page_size(self.request)),
                    ("results", data),
                ]
            )
        )

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "properties": {
                "next": {"type": "string", "nullable": True, "format": "uri"},
                "previous": {"type": "string", "nullable": True, "format": "uri"},
                "page_size": {"type": "integer"},
                "results": schema,
            },
        }


class LedgerCursorPagination(TenantCursorPagination):
    """For journal lines and stock movements: ordered by business date.

    Users reading a ledger expect entry-date order, not insertion order — a
    backdated correction posted today belongs next to the period it corrects.
    """

    page_size = 100
    max_page_size = 500
    ordering = ("-entry_date", "-created_at", "-id")

    def get_ordering(self, request, queryset, view):
        """Always this paginator's ordering, never the view's.

        ``CursorPagination.get_ordering`` looks for an ``OrderingFilter`` on
        the view and, if it finds one, takes the ordering from *that* —
        ignoring the ``ordering`` declared here. On a viewset whose own
        ordering suits its main resource, the mismatch is fatal for a nested
        action: ``AccountViewSet`` orders by ``code``, so
        ``/accounts/{id}/ledger/`` tried to order ``JournalLine`` rows by a
        field they do not have and answered 500 with
        ``FieldError: Cannot resolve keyword 'code'``.

        Overriding is not merely a fix for that crash — it is required for
        correctness. The running balance the ledger returns is a property of
        a *sequence*, computed by walking the page in entry-date order, and
        ``_opening_balance`` compares on the full ``(entry_date, created_at,
        id)`` tuple to decide what is "strictly older". Letting a caller
        re-sort by ``?ordering=`` would leave those balances attached to rows
        in an order that does not produce them: every figure on the page would
        be wrong, and nothing would say so.
        """
        return self.ordering


class SmallPagePagination(PageNumberPagination):
    """Page numbers, for genuinely small bounded sets only.

    Legitimate uses: the chart of accounts, departments, tax rates — sets a
    user expects to see a page count for and that do not grow with
    transaction volume. Never use this for documents.
    """

    page_size = 100
    max_page_size = 500
    page_size_query_param = "page_size"
