"""
Accounting URL registration.

Mounts the real viewsets from :mod:`apps.accounting.viewsets` — the ones that
carry the ``@action`` state transitions (``post``, ``void``, ``reverse``,
``close``, ``reopen``) rather than the read-only scaffolding this module used
to hold. The prefixes are unchanged: the frontend and the generated TypeScript
client are built from them, and renaming one is a breaking API change dressed
up as a refactor.

Registration order is deliberate and stable. drf-spectacular emits operation
ids in registration order, so shuffling this list produces a noisy diff in
``packages/api-client`` for no behavioural change.
"""

from __future__ import annotations

from apps.accounting.viewsets import (
    AccountViewSet,
    ExchangeRateViewSet,
    FiscalPeriodViewSet,
    FiscalYearViewSet,
    JournalEntryViewSet,
    JournalViewSet,
    TaxRateViewSet,
)


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(r"accounts", AccountViewSet, basename="accounts")
    router.register(r"tax-rates", TaxRateViewSet, basename="tax-rates")
    router.register(r"fiscal-years", FiscalYearViewSet, basename="fiscal-years")
    router.register(r"fiscal-periods", FiscalPeriodViewSet, basename="fiscal-periods")
    router.register(r"journals", JournalViewSet, basename="journals")
    router.register(r"journal-entries", JournalEntryViewSet, basename="journal-entries")
    router.register(r"exchange-rates", ExchangeRateViewSet, basename="exchange-rates")
