"""Resolve the exchange rate a foreign-currency entry is posted at.

The gap this closes
-------------------
``ExchangeRate`` has existed since the first accounting migration, with a
per-tenant rate table, a uniqueness constraint per currency pair per day and a
CRUD endpoint. Nothing read it. ``JournalEntryDraft.exchange_rate`` defaults to
``Decimal("1")`` and every serializer that accepts one treats it as optional
(``data.get("exchange_rate") or Decimal("1")``).

The consequence was quiet and expensive: a tenant keeping books in EGP could
post a USD invoice, omit the rate, and have it convert at 1:1. The entry
balances -- both sides are USD -- so no guard fires. ``base_debit`` and
``base_credit`` are written as the USD figures, the trial balance still
foots, and the balance sheet understates the receivable by roughly
ninety-eight per cent. Nothing in the system disagrees with itself, which is
exactly why nobody would find it until a reconciliation.

The contract
------------
``resolve_rate`` is called from ``post_entry`` for every posting, so all five
modules that post (sales, expenses, payroll, inventory, banking) get the same
behaviour without each remembering to ask.

* **Transaction currency == base currency.** The rate is 1, and anything else
  is refused. A rate of 1.05 on an EGP entry in an EGP-books tenant is not a
  currency conversion, it is a five per cent error waiting to be posted.
* **Foreign currency, rate supplied by the caller.** Honoured. Contracted
  rates, customs rates and the rate printed on a supplier's invoice are all
  legitimate reasons to override the table, and refusing them would push
  users into editing the rate table to post one document.
* **Foreign currency, no rate supplied.** Looked up in ``ExchangeRate``: the
  most recent rate on or before the entry date. If there is none, the posting
  is **refused**. Falling back to 1 is the behaviour that caused the problem
  above -- an error that is invisible is worse than one that stops work.

Why "on or before" rather than an exact date match
--------------------------------------------------
Rate tables have holes: weekends, public holidays, and the days a feed failed.
Requiring an exact match would refuse to post a Saturday invoice in a system
whose rates arrive on business days, which is most of them. Carrying the last
known rate forward is what accounting practice does, and it is auditable
because the rate that was used is frozen onto the entry.

Direction
---------
Rates are stored as ``from_currency -> to_currency``. A tenant on EGP books
posting USD needs USD -> EGP. If only the inverse (EGP -> USD) is on file it
is used as ``1 / rate``, because a rate table with one direction recorded is
far more common than one with both, and refusing would be pedantry. The
inversion happens at full ``RateField`` precision.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError

from apps.accounting.models import ExchangeRate

ONE = Decimal("1")


class NoExchangeRate(ValidationError):
    """No rate on file for a foreign-currency posting. Its own class so a
    caller can offer "add a rate" rather than a generic validation error."""


class InvalidExchangeRate(ValidationError):
    """A rate was supplied that contradicts the currencies involved."""


def base_currency(tenant_id: uuid.UUID) -> str:
    """The tenant's reporting currency.

    Imported inside the function: ``apps.tenancy`` is not an accounting
    dependency at module level and a top-level import makes the two apps
    circular through the migration graph.
    """
    from apps.tenancy.models import Tenant  # noqa: PLC0415

    code = (
        Tenant.objects.filter(id=tenant_id)
        .values_list("base_currency", flat=True)
        .first()
    )
    if not code:
        raise ValidationError(
            f"Tenant {tenant_id} has no base currency set; nothing can be "
            f"converted until it does."
        )
    return code


def lookup_rate(
    tenant_id: uuid.UUID,
    from_currency: str,
    to_currency: str,
    on_date: date,
) -> Optional[Decimal]:
    """Most recent rate on or before ``on_date``, or its inverse, or None."""
    if from_currency == to_currency:
        return ONE

    direct = (
        ExchangeRate.all_tenants.filter(
            tenant_id=tenant_id,
            from_currency=from_currency,
            to_currency=to_currency,
            rate_date__lte=on_date,
        )
        .order_by("-rate_date")
        .values_list("rate", flat=True)
        .first()
    )
    if direct is not None:
        return direct

    inverse = (
        ExchangeRate.all_tenants.filter(
            tenant_id=tenant_id,
            from_currency=to_currency,
            to_currency=from_currency,
            rate_date__lte=on_date,
        )
        .order_by("-rate_date")
        .values_list("rate", flat=True)
        .first()
    )
    if inverse is not None and inverse > 0:
        return ONE / inverse

    return None


def resolve_rate(
    tenant_id: uuid.UUID,
    currency: str,
    on_date: date,
    supplied: Optional[Decimal] = None,
) -> Decimal:
    """The rate this entry will be posted at. See the module docstring.

    ``supplied`` is the caller's rate. ``None`` and ``1`` are treated the same
    for a foreign currency -- the draft's default is 1, so there is no way to
    tell "I mean one" from "I did not say", and for a foreign currency the
    former is almost never true. A caller that genuinely means 1 on a foreign
    currency (a pegged pair) should have that rate in the table, where it is
    visible and dated, rather than implied by an omission.
    """
    base = base_currency(tenant_id)

    if currency == base:
        if supplied is not None and supplied != ONE:
            raise InvalidExchangeRate(
                f"Entry is in {currency}, which is this tenant's base "
                f"currency, so its exchange rate must be 1 (got {supplied}). "
                f"A rate here would convert the books against themselves."
            )
        return ONE

    if supplied is not None and supplied != ONE:
        return supplied

    rate = lookup_rate(tenant_id, currency, base, on_date)
    if rate is None:
        raise NoExchangeRate(
            f"No exchange rate on file for {currency} -> {base} on or before "
            f"{on_date}. Add one (POST /api/v1/exchange-rates/) or supply an "
            f"explicit rate with the document. Refusing to post at 1:1, which "
            f"would balance and still misstate the ledger."
        )
    return rate


__all__ = [
    "InvalidExchangeRate",
    "NoExchangeRate",
    "base_currency",
    "lookup_rate",
    "resolve_rate",
]
