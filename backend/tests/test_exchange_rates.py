"""Foreign-currency postings must use a real rate, or not post at all.

The bug these tests exist to prevent is silent and self-consistent, which is
the worst combination. Before ``apps.accounting.services.fx``, the
``ExchangeRate`` table was written by a CRUD endpoint and read by nothing;
``JournalEntryDraft.exchange_rate`` defaulted to 1 and every serializer
treated it as optional. So a tenant on EGP books could post a USD invoice with
no rate, and it converted at 1:1.

Nothing complained. Both sides of the entry are in USD, so it balances. The
balance trigger passes. The trial balance foots, because ``base_debit`` and
``base_credit`` are equally wrong. The only symptom is a receivable understated
by the whole spread — visible at a reconciliation, months later, with no error
anywhere to point at the cause.

The rule now: a rate is either supplied deliberately or looked up. It is never
assumed.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.accounting.models import ExchangeRate
from apps.accounting.services.fx import (
    InvalidExchangeRate,
    NoExchangeRate,
    lookup_rate,
    resolve_rate,
)
from apps.accounting.services.posting import post_entry
from apps.core.fields import ZERO
from apps.core.models import Currency
from tests.conftest import TEST_CURRENCY, make_draft

pytestmark = pytest.mark.django_db

TODAY = date.today()


@pytest.fixture
def usd_rate(tenant) -> ExchangeRate:
    """USD -> EGP at 48.50, dated today."""
    return ExchangeRate.objects.create(
        tenant=tenant,
        from_currency=Currency.USD,
        to_currency=TEST_CURRENCY,
        rate=Decimal("48.500000"),
        rate_date=TODAY,
        source="test",
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_base_currency_always_resolves_to_one(tenant):
    assert resolve_rate(tenant.id, TEST_CURRENCY, TODAY) == Decimal("1")


def test_a_rate_on_a_base_currency_entry_is_refused(tenant):
    """Converting the books against themselves is an error, not a conversion.

    This is the case the old ledger test asserted was *allowed*, which is how
    the foreign-currency path went unexercised.
    """
    with pytest.raises(InvalidExchangeRate):
        resolve_rate(tenant.id, TEST_CURRENCY, TODAY, Decimal("2.5"))


def test_foreign_currency_without_a_rate_on_file_is_refused(tenant):
    """The whole point: refuse, rather than quietly assume 1:1."""
    with pytest.raises(NoExchangeRate):
        resolve_rate(tenant.id, Currency.USD, TODAY)


def test_foreign_currency_uses_the_table_when_no_rate_is_supplied(tenant, usd_rate):
    assert resolve_rate(tenant.id, Currency.USD, TODAY) == Decimal("48.500000")


def test_an_explicitly_supplied_rate_overrides_the_table(tenant, usd_rate):
    """A contracted or customs rate must be usable without editing the table.

    Forcing users to rewrite the shared rate table to post one document would
    corrupt every other document dated that day.
    """
    assert resolve_rate(
        tenant.id, Currency.USD, TODAY, Decimal("50.000000")
    ) == Decimal("50.000000")


def test_the_most_recent_rate_on_or_before_the_date_is_used(tenant):
    """Rate tables have holes — weekends, holidays, failed feeds.

    Requiring an exact date match would refuse to post a Saturday invoice in
    any system whose rates arrive on business days.
    """
    ExchangeRate.objects.create(
        tenant=tenant, from_currency=Currency.USD, to_currency=TEST_CURRENCY,
        rate=Decimal("47.000000"), rate_date=TODAY - timedelta(days=10),
    )
    ExchangeRate.objects.create(
        tenant=tenant, from_currency=Currency.USD, to_currency=TEST_CURRENCY,
        rate=Decimal("48.000000"), rate_date=TODAY - timedelta(days=3),
    )

    assert resolve_rate(tenant.id, Currency.USD, TODAY) == Decimal("48.000000")


def test_a_rate_dated_after_the_entry_is_not_used(tenant):
    """Hindsight is not a valid basis for a filed figure."""
    ExchangeRate.objects.create(
        tenant=tenant, from_currency=Currency.USD, to_currency=TEST_CURRENCY,
        rate=Decimal("99.000000"), rate_date=TODAY + timedelta(days=1),
    )

    with pytest.raises(NoExchangeRate):
        resolve_rate(tenant.id, Currency.USD, TODAY)


def test_the_inverse_pair_is_used_when_only_it_is_on_file(tenant):
    """A table with one direction recorded is far more common than both."""
    ExchangeRate.objects.create(
        tenant=tenant, from_currency=TEST_CURRENCY, to_currency=Currency.USD,
        rate=Decimal("0.020000"), rate_date=TODAY,
    )

    assert lookup_rate(tenant.id, Currency.USD, TEST_CURRENCY, TODAY) == Decimal("50")


# ---------------------------------------------------------------------------
# End to end through the posting choke point
# ---------------------------------------------------------------------------

def test_posting_a_foreign_entry_with_no_rate_anywhere_is_refused(
    tenant, chart_of_accounts, open_period, owner_user
):
    """The regression test for the original defect, at the real boundary."""
    draft = make_draft(
        debit_account=chart_of_accounts["bank_main"],
        credit_account=chart_of_accounts["sales_revenue"],
        amount=Decimal("100.00"),
        currency=Currency.USD,
    )

    with pytest.raises(NoExchangeRate):
        post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)


def test_posting_a_foreign_entry_converts_at_the_table_rate(
    tenant, chart_of_accounts, open_period, owner_user, usd_rate
):
    draft = make_draft(
        debit_account=chart_of_accounts["bank_main"],
        credit_account=chart_of_accounts["sales_revenue"],
        amount=Decimal("100.00"),
        currency=Currency.USD,
    )

    entry = post_entry(draft, tenant_id=tenant.id, user_id=owner_user.id)

    line = entry.lines.get(debit__gt=ZERO)
    assert entry.currency == Currency.USD
    assert entry.exchange_rate == Decimal("48.500000")
    assert line.debit == Decimal("100.00")          # transaction currency
    assert line.base_debit == Decimal("4850.00")    # tenant's books


def test_the_rate_used_is_frozen_onto_the_entry(
    tenant, chart_of_accounts, open_period, owner_user, usd_rate
):
    """Editing the rate table afterwards must not restate a filed entry.

    ``base_debit`` is stored, not derived — this asserts that end to end
    rather than by reading the column definition.
    """
    entry = post_entry(
        make_draft(
            debit_account=chart_of_accounts["bank_main"],
            credit_account=chart_of_accounts["sales_revenue"],
            amount=Decimal("100.00"),
            currency=Currency.USD,
        ),
        tenant_id=tenant.id,
        user_id=owner_user.id,
    )

    usd_rate.rate = Decimal("60.000000")
    usd_rate.save(update_fields=["rate", "updated_at"])

    entry.refresh_from_db()
    line = entry.lines.get(debit__gt=ZERO)
    assert entry.exchange_rate == Decimal("48.500000")
    assert line.base_debit == Decimal("4850.00")


@pytest.mark.rls
def test_rates_do_not_leak_between_tenants(tenant, other_tenant, db_no_rls):
    """``ExchangeRate`` is tenant-scoped: a group's corporate rate table is
    not another company's.

    The other tenant's row has to be written with the RLS bypass held open —
    the policy's WITH CHECK refuses an INSERT carrying a different
    ``tenant_id`` than the bound one, which is the isolation model doing its
    job. The bypass is then dropped so the lookup runs under the policy, the
    way application code always does.
    """
    from apps.core.tenancy_context import bind_database_session  # noqa: PLC0415

    ExchangeRate.all_tenants.create(
        tenant=other_tenant, from_currency=Currency.USD,
        to_currency=TEST_CURRENCY, rate=Decimal("48.500000"), rate_date=TODAY,
    )

    # Back under the policy, bound to the first tenant.
    bind_database_session(tenant.id)

    assert lookup_rate(tenant.id, Currency.USD, TEST_CURRENCY, TODAY) is None
