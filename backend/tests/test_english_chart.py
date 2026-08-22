"""The default English 5-level chart: coverage and shape.

The chart is ported from the reference GL and extended so that every role this
ERP's automated postings resolve by ``system_key`` lands on a leaf. If any is
missing the ledger cannot post the corresponding document, so this is a
provisioning guarantee, not a nicety.
"""

from __future__ import annotations

import pytest

from apps.accounting.chart.english_chart import (
    build_default_chart,
    required_system_keys,
)
from apps.accounting.models import Account

pytestmark = pytest.mark.django_db


def test_chart_seeds_every_required_system_key(tenant):
    counts, by_key = build_default_chart(tenant.id)
    present = set(
        Account.all_tenants.filter(tenant_id=tenant.id)
        .exclude(system_key="")
        .values_list("system_key", flat=True)
    )
    assert required_system_keys() <= present
    # Roles the nine integrating modules resolve — a representative sample.
    for key in ("ar_control", "ap_control", "sales_revenue", "cogs", "output_vat",
                "input_vat", "bank_main", "retained_earnings",
                "payroll_salaries_payable", "grp_current_assets"):
        assert key in by_key, key


def test_chart_is_a_well_formed_five_level_tree(tenant):
    build_default_chart(tenant.id)
    accounts = list(Account.all_tenants.filter(tenant_id=tenant.id))
    assert accounts, "chart seeded nothing"
    # Exactly five root sections; nothing deeper than level 5.
    roots = [a for a in accounts if a.parent_id is None]
    assert len(roots) == 5
    assert {a.type for a in roots} == {"asset", "liability", "equity", "income", "expense"}
    # Only level-5 accounts are postable, and every level-5 account is.
    for a in accounts:
        assert (a.level == 5) == a.is_postable, (a.code, a.level, a.is_postable)
        assert 1 <= a.level <= 5
    # Full codes are unique and the human code decodes them.
    full_codes = [a.full_code for a in accounts]
    assert len(full_codes) == len(set(full_codes))


def test_reseeding_is_idempotent(tenant):
    first, _ = build_default_chart(tenant.id)
    before = Account.all_tenants.filter(tenant_id=tenant.id).count()
    second, _ = build_default_chart(tenant.id)
    after = Account.all_tenants.filter(tenant_id=tenant.id).count()
    assert after == before                 # no duplicates on a re-run
    assert second["created"] == 0          # everything matched by full_code
