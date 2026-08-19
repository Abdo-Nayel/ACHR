"""
Money & quantity primitives.

RULE #1 OF THIS CODEBASE:
    Monetary values are NEVER represented as float / double precision.
    Every amount is a `decimal.Decimal` backed by PostgreSQL `numeric`.

Rationale
---------
`0.1 + 0.2 != 0.3` in IEEE-754. In an accounting ledger that is not a
rounding nuisance, it is a broken trial balance: a journal entry whose
debits and credits differ by 1e-17 will fail the DB check constraint and
the whole posting transaction rolls back. Using `numeric` end-to-end
removes the failure mode entirely instead of papering over it.

Precision policy
----------------
* ``MoneyField``     -> numeric(19, 6)  storage precision
* ``QuantityField``  -> numeric(19, 6)
* ``RateField``      -> numeric(9, 6)   tax rates, FX rates, percentages
* Presentation rounding to the currency's minor unit happens exactly once,
  at the moment an amount is *posted* to the ledger or rendered to a user.
  Intermediate arithmetic keeps full precision.

19 integer+fraction digits with 6 decimals covers ~10 trillion in any
currency while leaving room for unit prices such as 0.000125 / litre and
for currencies with 3 minor units (KWD, BHD, TND) or 0 (JPY, KRW).
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from typing import Final

from django.core.exceptions import ValidationError
from django.db import models

# ---------------------------------------------------------------------------
# Decimal context
# ---------------------------------------------------------------------------
# ROUND_HALF_UP is the convention expected by virtually every tax authority
# ("banker's rounding" surprises auditors). Traps are enabled so that a silent
# loss of significance raises instead of quietly corrupting a ledger.
MONEY_CONTEXT: Final = decimal.Context(
    prec=38,
    rounding=decimal.ROUND_HALF_UP,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
)

MONEY_MAX_DIGITS: Final[int] = 19
MONEY_DECIMAL_PLACES: Final[int] = 6
RATE_MAX_DIGITS: Final[int] = 9
RATE_DECIMAL_PLACES: Final[int] = 6

ZERO: Final[Decimal] = Decimal("0.000000")


class MoneyField(models.DecimalField):
    """numeric(19,6) column for any monetary amount."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", MONEY_MAX_DIGITS)
        kwargs.setdefault("decimal_places", MONEY_DECIMAL_PLACES)
        kwargs.setdefault("default", ZERO)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("max_digits", None)
        kwargs.pop("decimal_places", None)
        return name, path, args, kwargs


class QuantityField(models.DecimalField):
    """numeric(19,6) column for stock quantities and billable hours."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", MONEY_MAX_DIGITS)
        kwargs.setdefault("decimal_places", MONEY_DECIMAL_PLACES)
        kwargs.setdefault("default", ZERO)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("max_digits", None)
        kwargs.pop("decimal_places", None)
        return name, path, args, kwargs


class RateField(models.DecimalField):
    """numeric(9,6) column for tax rates, discount percentages, FX rates."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", RATE_MAX_DIGITS)
        kwargs.setdefault("decimal_places", RATE_DECIMAL_PLACES)
        kwargs.setdefault("default", ZERO)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("max_digits", None)
        kwargs.pop("decimal_places", None)
        return name, path, args, kwargs


# ---------------------------------------------------------------------------
# Coercion / rounding helpers
# ---------------------------------------------------------------------------

_ALLOWED_INPUT = (int, str, Decimal)

#: Number of minor units per currency. Anything absent defaults to 2.
CURRENCY_MINOR_UNITS: Final[dict[str, int]] = {
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "XAF": 0, "XOF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}


def to_money(value, *, field_name: str = "amount") -> Decimal:
    """Coerce untrusted input to a Decimal, refusing floats outright.

    A `float` argument is a *programming* error, not a user error: somewhere
    upstream a JSON number was parsed without ``parse_float=Decimal``. We
    raise loudly rather than absorb the imprecision.
    """
    if isinstance(value, bool) or not isinstance(value, _ALLOWED_INPUT):
        raise ValidationError(
            {field_name: f"Monetary values must be Decimal/str/int, got "
                         f"{type(value).__name__}. Floats are forbidden."}
        )
    try:
        with decimal.localcontext(MONEY_CONTEXT):
            dec = Decimal(value)
    except decimal.InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValidationError({field_name: f"'{value}' is not a valid amount."}) from exc

    if not dec.is_finite():
        raise ValidationError({field_name: "Amount must be finite."})
    return dec.quantize(Decimal(1).scaleb(-MONEY_DECIMAL_PLACES), context=MONEY_CONTEXT)


def minor_units(currency: str) -> int:
    return CURRENCY_MINOR_UNITS.get((currency or "").upper(), 2)


def quantize_currency(value: Decimal, currency: str) -> Decimal:
    """Round to the currency's presentation precision (posting boundary only)."""
    exponent = Decimal(1).scaleb(-minor_units(currency))
    with decimal.localcontext(MONEY_CONTEXT):
        return Decimal(value).quantize(exponent)


def allocate(total: Decimal, weights: list[Decimal], currency: str) -> list[Decimal]:
    """Split ``total`` across ``weights`` with zero cent leakage.

    Naive proportional splitting loses or invents minor units
    (100.00 / 3 -> 33.33 * 3 = 99.99). The largest-remainder method below
    guarantees ``sum(result) == total`` exactly, which is what keeps
    tax allocation and payment application from breaking the trial balance.
    """
    weight_total = sum(weights, ZERO)
    if weight_total == 0:
        raise ValidationError("Cannot allocate across zero total weight.")

    exponent = Decimal(1).scaleb(-minor_units(currency))
    with decimal.localcontext(MONEY_CONTEXT):
        raw = [total * w / weight_total for w in weights]
        floored = [r.quantize(exponent, rounding=decimal.ROUND_DOWN) for r in raw]
        residual = total.quantize(exponent) - sum(floored, ZERO)
        step = exponent
        # Hand the leftover minor units to the largest fractional remainders.
        order = sorted(
            range(len(raw)), key=lambda i: (raw[i] - floored[i]), reverse=True
        )
        i = 0
        while residual > 0 and order:
            floored[order[i % len(order)]] += step
            residual -= step
            i += 1
    return floored
