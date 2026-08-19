"""
Reference data for the public signup form.

Served by ``GET /api/v1/auth/reference/`` (AllowAny) so the signup screen can
render its country / currency / timezone pickers before anyone has an account.

Why this lives in code and not in a database table
--------------------------------------------------
It is not customer data and it is not configuration: it is a property of the
release, exactly like ``config/permissions.json``. A table would have to be
seeded per environment, would drift between them, and would put a query on the
one endpoint that is hit by every unauthenticated visitor. A module constant is
diffable in review, cached by the process, and impossible to have "not seeded
yet" in production.

The currency lists
------------------
Two things are deliberately different and must not be conflated:

``ISO-4217 currency of a country``
    A fact about the world. Qatar's currency is QAR whether or not this
    product can keep books in it. ``Country.default_currency`` is that fact.

``The currency a ledger may be kept in``
    A property of *this* deployment: :class:`apps.core.models.Currency`. Every
    monetary column in the schema declares those choices, so a tenant whose
    ``base_currency`` is outside the set would be unable to create an invoice
    — the serializer's ``ChoiceField`` would reject it. So signup validates
    ``base_currency`` against ``Currency.values`` and the payload marks each
    currency with ``is_ledger_supported`` plus a top-level
    ``ledger_currencies`` list, rather than offering the client a choice the
    rest of the API will refuse.

Minor units come from :data:`apps.core.fields.CURRENCY_MINOR_UNITS` — the same
table the posting service rounds with — because a second copy of "KWD has
three decimal places" is a copy that will disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from apps.core.fields import minor_units


@dataclass(frozen=True, slots=True)
class Country:
    code: str            # ISO-3166 alpha-2, matching Tenant.country
    name: str
    default_currency: str  # ISO-4217 alpha-3
    default_timezone: str  # IANA zone


#: Gulf, wider MENA, Europe, North America, Asia. Ordered by region then name
#: so the picker groups sensibly without the client having to sort.
COUNTRIES: Final[tuple[Country, ...]] = (
    # -- Gulf Cooperation Council -------------------------------------------
    Country("AE", "United Arab Emirates", "AED", "Asia/Dubai"),
    Country("BH", "Bahrain", "BHD", "Asia/Bahrain"),
    Country("KW", "Kuwait", "KWD", "Asia/Kuwait"),
    Country("OM", "Oman", "OMR", "Asia/Muscat"),
    Country("QA", "Qatar", "QAR", "Asia/Qatar"),
    Country("SA", "Saudi Arabia", "SAR", "Asia/Riyadh"),
    # -- Wider MENA ----------------------------------------------------------
    Country("DZ", "Algeria", "DZD", "Africa/Algiers"),
    Country("EG", "Egypt", "EGP", "Africa/Cairo"),
    Country("IQ", "Iraq", "IQD", "Asia/Baghdad"),
    Country("JO", "Jordan", "JOD", "Asia/Amman"),
    Country("LB", "Lebanon", "LBP", "Asia/Beirut"),
    Country("LY", "Libya", "LYD", "Africa/Tripoli"),
    Country("MA", "Morocco", "MAD", "Africa/Casablanca"),
    Country("SD", "Sudan", "SDG", "Africa/Khartoum"),
    Country("TN", "Tunisia", "TND", "Africa/Tunis"),
    Country("TR", "Türkiye", "TRY", "Europe/Istanbul"),
    Country("IL", "Israel", "ILS", "Asia/Jerusalem"),
    Country("KE", "Kenya", "KES", "Africa/Nairobi"),
    Country("NG", "Nigeria", "NGN", "Africa/Lagos"),
    Country("ZA", "South Africa", "ZAR", "Africa/Johannesburg"),
    # -- Europe --------------------------------------------------------------
    Country("AT", "Austria", "EUR", "Europe/Vienna"),
    Country("BE", "Belgium", "EUR", "Europe/Brussels"),
    Country("CH", "Switzerland", "CHF", "Europe/Zurich"),
    Country("CZ", "Czechia", "CZK", "Europe/Prague"),
    Country("DE", "Germany", "EUR", "Europe/Berlin"),
    Country("DK", "Denmark", "DKK", "Europe/Copenhagen"),
    Country("ES", "Spain", "EUR", "Europe/Madrid"),
    Country("FI", "Finland", "EUR", "Europe/Helsinki"),
    Country("FR", "France", "EUR", "Europe/Paris"),
    Country("GB", "United Kingdom", "GBP", "Europe/London"),
    Country("GR", "Greece", "EUR", "Europe/Athens"),
    Country("IE", "Ireland", "EUR", "Europe/Dublin"),
    Country("IT", "Italy", "EUR", "Europe/Rome"),
    Country("NL", "Netherlands", "EUR", "Europe/Amsterdam"),
    Country("NO", "Norway", "NOK", "Europe/Oslo"),
    Country("PL", "Poland", "PLN", "Europe/Warsaw"),
    Country("PT", "Portugal", "EUR", "Europe/Lisbon"),
    Country("RO", "Romania", "RON", "Europe/Bucharest"),
    Country("SE", "Sweden", "SEK", "Europe/Stockholm"),
    # -- North America -------------------------------------------------------
    Country("CA", "Canada", "CAD", "America/Toronto"),
    Country("MX", "Mexico", "MXN", "America/Mexico_City"),
    Country("US", "United States", "USD", "America/New_York"),
    # -- Asia & Oceania ------------------------------------------------------
    Country("AU", "Australia", "AUD", "Australia/Sydney"),
    Country("BD", "Bangladesh", "BDT", "Asia/Dhaka"),
    Country("CN", "China", "CNY", "Asia/Shanghai"),
    Country("HK", "Hong Kong SAR", "HKD", "Asia/Hong_Kong"),
    Country("ID", "Indonesia", "IDR", "Asia/Jakarta"),
    Country("IN", "India", "INR", "Asia/Kolkata"),
    Country("JP", "Japan", "JPY", "Asia/Tokyo"),
    Country("KR", "South Korea", "KRW", "Asia/Seoul"),
    Country("LK", "Sri Lanka", "LKR", "Asia/Colombo"),
    Country("MY", "Malaysia", "MYR", "Asia/Kuala_Lumpur"),
    Country("NZ", "New Zealand", "NZD", "Pacific/Auckland"),
    Country("PH", "Philippines", "PHP", "Asia/Manila"),
    Country("PK", "Pakistan", "PKR", "Asia/Karachi"),
    Country("SG", "Singapore", "SGD", "Asia/Singapore"),
    Country("TH", "Thailand", "THB", "Asia/Bangkok"),
    Country("VN", "Vietnam", "VND", "Asia/Ho_Chi_Minh"),
)

#: ``code -> (name, symbol)``. Decimal places are *not* here on purpose: they
#: come from ``CURRENCY_MINOR_UNITS`` via :func:`apps.core.fields.minor_units`,
#: which is what the posting service rounds with.
CURRENCY_NAMES: Final[dict[str, tuple[str, str]]] = {
    "AED": ("UAE Dirham", "د.إ"),
    "AUD": ("Australian Dollar", "A$"),
    "BDT": ("Bangladeshi Taka", "৳"),
    "BHD": ("Bahraini Dinar", ".د.ب"),
    "CAD": ("Canadian Dollar", "C$"),
    "CHF": ("Swiss Franc", "CHF"),
    "CNY": ("Chinese Yuan", "¥"),
    "CZK": ("Czech Koruna", "Kč"),
    "DKK": ("Danish Krone", "kr"),
    "DZD": ("Algerian Dinar", "د.ج"),
    "EGP": ("Egyptian Pound", "E£"),
    "EUR": ("Euro", "€"),
    "GBP": ("Pound Sterling", "£"),
    "HKD": ("Hong Kong Dollar", "HK$"),
    "IDR": ("Indonesian Rupiah", "Rp"),
    "ILS": ("Israeli New Shekel", "₪"),
    "INR": ("Indian Rupee", "₹"),
    "IQD": ("Iraqi Dinar", "ع.د"),
    "JOD": ("Jordanian Dinar", "د.ا"),
    "JPY": ("Japanese Yen", "¥"),
    "KES": ("Kenyan Shilling", "KSh"),
    "KRW": ("South Korean Won", "₩"),
    "KWD": ("Kuwaiti Dinar", "د.ك"),
    "LBP": ("Lebanese Pound", "ل.ل"),
    "LKR": ("Sri Lankan Rupee", "Rs"),
    "LYD": ("Libyan Dinar", "ل.د"),
    "MAD": ("Moroccan Dirham", "د.م."),
    "MXN": ("Mexican Peso", "MX$"),
    "MYR": ("Malaysian Ringgit", "RM"),
    "NGN": ("Nigerian Naira", "₦"),
    "NOK": ("Norwegian Krone", "kr"),
    "NZD": ("New Zealand Dollar", "NZ$"),
    "OMR": ("Omani Rial", "ر.ع."),
    "PHP": ("Philippine Peso", "₱"),
    "PKR": ("Pakistani Rupee", "₨"),
    "PLN": ("Polish Złoty", "zł"),
    "QAR": ("Qatari Riyal", "ر.ق"),
    "RON": ("Romanian Leu", "lei"),
    "SAR": ("Saudi Riyal", "ر.س"),
    "SDG": ("Sudanese Pound", "ج.س"),
    "SEK": ("Swedish Krona", "kr"),
    "SGD": ("Singapore Dollar", "S$"),
    "THB": ("Thai Baht", "฿"),
    "TND": ("Tunisian Dinar", "د.ت"),
    "TRY": ("Turkish Lira", "₺"),
    "USD": ("US Dollar", "$"),
    "VND": ("Vietnamese Đồng", "₫"),
    "ZAR": ("South African Rand", "R"),
}

#: Zones offered by the picker: every country default above, plus the handful
#: of extra zones a multi-site customer actually asks for. A full
#: ``zoneinfo.available_timezones()`` dump is ~600 entries, most of them
#: aliases and deprecated names, and it makes the picker unusable.
EXTRA_TIMEZONES: Final[tuple[str, ...]] = (
    "UTC",
    "Africa/Accra",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "America/Vancouver",
    "Asia/Kathmandu",
    "Asia/Tehran",
    "Australia/Perth",
    "Europe/Kyiv",
    "Europe/Moscow",
    "Pacific/Honolulu",
)


def country_map() -> dict[str, Country]:
    return {c.code: c for c in COUNTRIES}


def timezones() -> list[str]:
    """Every offered IANA zone, ``UTC`` first, then alphabetical."""
    zones = {c.default_timezone for c in COUNTRIES} | set(EXTRA_TIMEZONES)
    zones.discard("UTC")
    return ["UTC", *sorted(zones)]


def currency_payload(code: str) -> dict[str, Any]:
    """One currency row: name, symbol, and its ISO-4217 minor-unit count.

    ``decimal_places`` is what the client must format with. Getting it from
    :func:`apps.core.fields.minor_units` rather than hard-coding 2 is the
    difference between showing a Kuwaiti customer ``KWD 12.500`` and showing
    them ``KWD 12.50``, which is a different amount.
    """
    from apps.core.models import Currency

    name, symbol = CURRENCY_NAMES.get(code, (code, code))
    return {
        "code": code,
        "name": name,
        "symbol": symbol,
        "decimal_places": minor_units(code),
        # Whether a ledger may be *kept* in it — see the module docstring.
        "is_ledger_supported": code in set(Currency.values),
    }


def currencies() -> list[dict[str, Any]]:
    codes = {c.default_currency for c in COUNTRIES} | set(CURRENCY_NAMES)
    return [currency_payload(code) for code in sorted(codes)]


def ledger_currencies() -> list[str]:
    """The subset a tenant's ``base_currency`` may actually be set to."""
    from apps.core.models import Currency

    return sorted(Currency.values)


def countries() -> list[dict[str, Any]]:
    return [
        {
            "code": c.code,
            "name": c.name,
            "default_currency": c.default_currency,
            "default_timezone": c.default_timezone,
        }
        for c in COUNTRIES
    ]


def system_roles() -> list[dict[str, Any]]:
    """The roles shipped with the product, lowest rank (most authority) first.

    Read with no tenant bound: ``iam_role``'s RLS policy has an explicit
    ``tenant_id IS NULL`` arm (``tenancy.0003_rls_nullable_tenant``) precisely
    so system roles are visible outside a tenant. Custom tenant roles are not
    listed here and must not be — this endpoint is public.
    """
    from apps.iam.models import Role

    return [
        {
            "code": role.code,
            "name": role.name,
            "rank": role.rank,
            "description": role.description,
        }
        for role in Role.objects.filter(tenant__isnull=True, is_system=True).order_by(
            "rank", "name"
        )
    ]


def reference_payload() -> dict[str, Any]:
    """The whole ``GET /auth/reference/`` body."""
    return {
        "countries": countries(),
        "currencies": currencies(),
        "ledger_currencies": ledger_currencies(),
        "timezones": timezones(),
        "roles": system_roles(),
    }
