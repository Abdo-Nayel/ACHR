"""
The report generator framework: context in, ``ReportResult`` out.

Design, and the failures each decision prevents
-----------------------------------------------
*One shared ledger query.* :func:`_ledger_query` is the only place a report is
allowed to describe "the rows this report is about". Every generator starts
from it. When each report re-derives its own filter, they drift: one forgets
``status='posted'`` and includes drafts, another filters on
``JournalEntry.created_at`` instead of ``entry_date`` and moves a week of
revenue into the wrong month, a third omits the tenant filter and is saved
only by row-level security. Those are not hypothetical — they are what a
reporting module looks like after two years of separate authors. Concentrating
the filter means a fix to it fixes every report at once, and a report that
disagrees with the trial balance is a bug in one function rather than a
disagreement between five.

*Decimal in, string out.* Amounts are ``Decimal`` throughout and are
serialised as **strings**. See :meth:`ReportResult.to_dict`.

*A registry, not a factory chain.* ``@register_report`` / :func:`get_generator`
mirror ``apps.payments.gateways.base`` exactly, for the same reason: adding a
report is one new module plus one import, with no edit to shared code.

*Generators are pure.* ``generate()`` reads and returns. It never writes, never
posts, never mutates a cache. Persisting a result is
``apps.reporting.services.snapshot``'s job. A generator that writes cannot be
run speculatively, cannot be run inside a read-only replica connection, and
cannot be trusted by an auditor who wants to re-run last year's report.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Type

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.accounting.models import JournalEntry, JournalLine
from apps.core.fields import ZERO, to_money

__all__ = [
    "ReportContext",
    "ReportLine",
    "ReportSection",
    "ReportResult",
    "ReportGenerator",
    "ReportError",
    "ReportImbalance",
    "register_report",
    "get_generator",
    "registered_reports",
    "ledger_query",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ReportError(Exception):
    """A report could not be produced correctly.

    Its own class so that monitoring separates "this report is wrong" from
    ordinary request validation. A wrong financial report that is *delivered*
    is far more expensive than one that fails loudly, so every consistency
    assertion in this package raises rather than annotating and continuing.
    """


class ReportImbalance(ReportError):
    """A statement failed its own arithmetic identity.

    Trial balance debits != credits, or Assets != Liabilities + Equity. The
    message always names the difference and both sides, because the first
    question anyone asks is "by how much?" and the second is "which side?" —
    and an error that answers neither sends someone into the ledger by hand.
    """


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReportContext:
    """Everything a generator is allowed to know about *what* to report.

    Frozen: a generator that could mutate its own context would be able to
    widen its date range or drop a dimension filter half-way through, and the
    resulting report would not match the parameters recorded next to it in the
    snapshot. Reproducibility starts here.

    ``include_unposted`` — read this before setting it True
    -------------------------------------------------------
    Default ``False``, and that default is a compliance control, not a
    performance choice. Draft journal entries are, by definition, entries
    nobody has accepted responsibility for: they are unbalanced while being
    built (``ck_entry_balanced`` only constrains posted rows), they carry no
    document number, and they may be abandoned entirely. Including them in a
    filed P&L, a VAT return or a figure given to a bank means reporting money
    that was never recorded as having moved — an overstatement that the ledger
    itself will never agree with, because the trial balance is computed from
    posted rows.

    The flag exists at all because a *management* preview ("what will this
    month look like once we post everything sitting in draft?") is a real and
    useful question. Any result produced with it set carries a loud warning in
    ``ReportResult.warnings``, and it is recorded in the snapshot parameters,
    so a reader can never mistake a preview for a statement.

    ``as_of`` vs ``date_from``/``date_to``
    --------------------------------------
    A P&L is a *period* (flows). A balance sheet is an *instant* (stocks).
    Modelling them with the same two fields forces the balance sheet to
    pretend ``date_to`` means "as at", and then someone passes a date_from and
    silently gets a balance sheet of one month's movements, which balances and
    is meaningless. They are separate fields; :attr:`effective_as_of` is the
    single accessor an as-of report uses.
    """

    tenant_id: uuid.UUID
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    #: Presentation currency. Reports aggregate ``base_debit``/``base_credit``,
    #: which are already in the tenant's base currency, so this is a label
    #: rather than a conversion instruction — converting at report time would
    #: make a historical report move whenever an FX table is corrected.
    currency: str = ""
    #: ``(from, to)`` of the period to compare against, or None.
    comparison_period: Optional[tuple[date, date]] = None
    department_id: Optional[uuid.UUID] = None
    project_id: Optional[uuid.UUID] = None
    include_unposted: bool = False
    as_of: Optional[date] = None
    #: Report-specific knobs (aging buckets, grouping code, employee filter).
    #: Kept as an opaque mapping so a new parameter does not change this
    #: dataclass's signature and therefore every generator's tests.
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.tenant_id is None:
            raise ReportError(
                "ReportContext requires an explicit tenant_id. Relying on the "
                "ambient tenant would let a report run against whichever "
                "tenant the worker happened to touch last."
            )
        object.__setattr__(self, "tenant_id", uuid.UUID(str(self.tenant_id)))
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ReportError(
                f"Report period ends before it starts "
                f"({self.date_from} .. {self.date_to})."
            )

    @property
    def effective_as_of(self) -> date:
        """The instant an as-of report is drawn at.

        Falls back to ``date_to`` and then to today, in that order, so a
        balance sheet requested with only a period still means "as at the end
        of that period" rather than silently meaning "now" — the latter would
        produce a December balance sheet containing January's transactions.
        """
        return self.as_of or self.date_to or timezone.localdate()

    @property
    def has_dimension_filter(self) -> bool:
        return self.department_id is not None or self.project_id is not None

    def for_comparison(self) -> Optional["ReportContext"]:
        """A copy of this context bound to the comparison period.

        Returned as a *context*, so the comparison figures go through exactly
        the same code path as the primary figures. Computing the comparison
        with a separate, simpler query is how a comparison column ends up
        including drafts, or a department filter the main column excluded.
        """
        if not self.comparison_period:
            return None
        start, end = self.comparison_period
        return replace(
            self, date_from=start, date_to=end, as_of=end, comparison_period=None
        )

    def to_parameters(self) -> dict[str, Any]:
        """Fully-resolved parameters, for the snapshot's evidence record.

        Every date is absolute. A snapshot whose parameters say "last month"
        is not reproducible, which defeats the purpose of taking one.
        """
        return {
            "tenant_id": str(self.tenant_id),
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "as_of": self.effective_as_of.isoformat(),
            "currency": self.currency,
            "comparison_period": (
                [self.comparison_period[0].isoformat(),
                 self.comparison_period[1].isoformat()]
                if self.comparison_period else None
            ),
            "department_id": str(self.department_id) if self.department_id else None,
            "project_id": str(self.project_id) if self.project_id else None,
            "include_unposted": self.include_unposted,
            "options": dict(self.options),
        }


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ReportLine:
    """One printable row. Amounts are Decimal; nothing here is a float.

    ``debit``/``credit`` are carried separately from ``amount`` for the same
    reason ``JournalLine`` splits them: a trial balance must print both sides,
    and reconstructing them from a signed net loses the information that a
    zero-net account still had activity.
    """

    label: str
    amount: Decimal = ZERO
    #: Populated by account-level reports; None on grouped/synthetic lines.
    account_id: Optional[uuid.UUID] = None
    account_code: str = ""
    account_type: str = ""
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    #: Same figure for the comparison period, when one was requested.
    comparison_amount: Optional[Decimal] = None
    #: Non-monetary measures (hours, units on hand). Decimal, never float:
    #: 0.1 hours x 3 must be 0.3, including when it is multiplied by a rate.
    quantity: Decimal = ZERO
    level: int = 0
    is_subtotal: bool = False
    is_bold: bool = False
    note: str = ""
    #: Report-specific extras (partner id, bucket name, warehouse code).
    meta: dict[str, Any] = field(default_factory=dict)
    children: list["ReportLine"] = field(default_factory=list)

    @property
    def variance(self) -> Optional[Decimal]:
        """Absolute movement against the comparison period."""
        if self.comparison_amount is None:
            return None
        return self.amount - self.comparison_amount

    @property
    def variance_pct(self) -> Optional[Decimal]:
        """Movement as a percentage, or None when the base is zero.

        Returning None rather than 0 or infinity is deliberate: "revenue went
        from 0 to 50 000" has no meaningful percentage, and printing 0% or a
        division-by-zero placeholder both read as "nothing happened".
        """
        if self.comparison_amount is None or self.comparison_amount == ZERO:
            return None
        return (
            (self.amount - self.comparison_amount) / abs(self.comparison_amount)
        ) * Decimal("100")


@dataclass(slots=True)
class ReportSection:
    """A labelled block of lines with its own total ("Revenue", "Assets").

    ``total`` is stored rather than computed on access so that a section whose
    total is *not* the plain sum of its lines — a section containing a
    contra line presented with ``sign = -1``, for instance — can say so
    truthfully. :meth:`recompute_total` is available for the ordinary case.
    """

    key: str
    title: str
    lines: list[ReportLine] = field(default_factory=list)
    total: Decimal = ZERO
    comparison_total: Optional[Decimal] = None
    sequence: int = 0
    note: str = ""

    def add(self, line: ReportLine) -> "ReportSection":
        self.lines.append(line)
        return self

    def recompute_total(self) -> Decimal:
        """Sum the non-subtotal lines. Subtotals are excluded because they are
        already the sum of lines in this section; including them double-counts
        every figure above them."""
        self.total = sum(
            (line.amount for line in self.lines if not line.is_subtotal), ZERO
        )
        comparisons = [
            line.comparison_amount
            for line in self.lines
            if not line.is_subtotal and line.comparison_amount is not None
        ]
        self.comparison_total = sum(comparisons, ZERO) if comparisons else None
        return self.total


@dataclass(slots=True)
class ReportResult:
    """The complete, computed report. Serialisable, immutable by convention.

    ``warnings`` is a first-class field rather than a log line. A cash-flow
    statement that does not reconcile to the bank, or a stock valuation that
    disagrees with the inventory control account, is still worth producing —
    but the discrepancy must travel *with* the figures to whoever reads them.
    Logging it puts it somewhere the reader will never look, which is
    functionally the same as hiding it.
    """

    report_type: str
    sections: list[ReportSection] = field(default_factory=list)
    #: Statement-level figures: net_profit, total_assets, difference...
    totals: dict[str, Decimal] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    generated_at: Optional[datetime] = None
    currency: str = ""

    def __post_init__(self) -> None:
        if self.generated_at is None:
            self.generated_at = timezone.now()

    @property
    def row_count(self) -> int:
        return sum(len(section.lines) for section in self.sections)

    def section(self, key: str) -> Optional[ReportSection]:
        return next((s for s in self.sections if s.key == key), None)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for JSONB storage and for the API.

        **Every Decimal becomes a string. This is not stylistic.**

        JSON has one numeric type and every mainstream parser maps it to an
        IEEE-754 double. A double carries about 15-17 significant decimal
        digits, and ``MoneyField`` is ``numeric(19, 6)`` — up to 19. So::

            1234567890123.456789  ->  1234567890123.4568

        The value is corrupted *by the round trip alone*, before anyone does
        arithmetic on it, and it is corrupted silently: the JSON is valid, the
        number looks right at a glance, and the error only surfaces when a
        total no longer matches the ledger by a fraction of a unit. Smaller
        amounts fare no better once they are added up — ``0.1 + 0.2`` is not
        ``0.3`` in binary floating point, which in a ledger is a failed trial
        balance rather than a rounding nuisance.

        Strings round-trip exactly, and ``Decimal(str_value)`` on the way back
        in is lossless. The cost is that consumers must parse; that cost is
        paid once, in one place, and is the reason the stored snapshot can
        claim to be reproducible byte for byte.
        """
        return {
            "report_type": self.report_type,
            "currency": self.currency,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "row_count": self.row_count,
            "totals": {k: _decimal_str(v) for k, v in sorted(self.totals.items())},
            "metadata": _jsonable(self.metadata),
            "warnings": list(self.warnings),
            "sections": [_section_dict(section) for section in self.sections],
        }


def _decimal_str(value: Any) -> Optional[str]:
    """Decimal -> its exact decimal string. See :meth:`ReportResult.to_dict`."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        # ``format(d, "f")`` never uses scientific notation. ``str(Decimal)``
        # does for small exponents ("1E-8"), which is valid JSON-as-string but
        # compares unequal to the same value written plainly — and a checksum
        # over the payload would then depend on how the value was produced.
        return format(value, "f")
    if isinstance(value, float):
        # A float here means a Decimal was lost upstream. Refusing is the
        # whole point: laundering it into a string hides the real bug.
        raise ReportError(
            f"Float {value!r} reached report serialisation. Monetary and "
            f"quantity values must be Decimal end to end."
        )
    return str(value)


def _jsonable(value: Any) -> Any:
    """Recursively convert a structure for JSON storage, Decimals to strings."""
    if isinstance(value, Decimal):
        return _decimal_str(value)
    if isinstance(value, float):
        raise ReportError(
            f"Float {value!r} reached report serialisation. Use Decimal."
        )
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


def _line_dict(line: ReportLine) -> dict[str, Any]:
    return {
        "label": line.label,
        "amount": _decimal_str(line.amount),
        "account_id": str(line.account_id) if line.account_id else None,
        "account_code": line.account_code,
        "account_type": line.account_type,
        "debit": _decimal_str(line.debit),
        "credit": _decimal_str(line.credit),
        "comparison_amount": _decimal_str(line.comparison_amount),
        "variance": _decimal_str(line.variance),
        "variance_pct": _decimal_str(line.variance_pct),
        "quantity": _decimal_str(line.quantity),
        "level": line.level,
        "is_subtotal": line.is_subtotal,
        "is_bold": line.is_bold,
        "note": line.note,
        "meta": _jsonable(line.meta),
        "children": [_line_dict(child) for child in line.children],
    }


def _section_dict(section: ReportSection) -> dict[str, Any]:
    return {
        "key": section.key,
        "title": section.title,
        "sequence": section.sequence,
        "note": section.note,
        "total": _decimal_str(section.total),
        "comparison_total": _decimal_str(section.comparison_total),
        "lines": [_line_dict(line) for line in section.lines],
    }


# ---------------------------------------------------------------------------
# The shared ledger query — every report starts here
# ---------------------------------------------------------------------------

def ledger_query(
    context: ReportContext,
    *,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    ignore_period: bool = False,
) -> QuerySet[JournalLine]:
    """The base ``JournalLine`` queryset for any report. **Use this, always.**

    What it guarantees, and the bug each guarantee prevents:

    * **Posted entries only** (unless ``context.include_unposted``). Drafts are
      unbalanced by design and may be abandoned; including them overstates a
      filed statement — see :class:`ReportContext`. Voided and reversed
      entries are excluded as well: a voided entry contributes nothing, and a
      *reversed* entry's effect is already cancelled by its mirror entry,
      which is itself posted and therefore already counted. Filtering only on
      ``!= draft`` would count reversals twice.
    * **Explicit tenant filter on ``all_tenants``.** Reports run from Celery
      workers and from management commands where the ambient tenant may not be
      bound; the default manager fails *closed* and would return ``.none()``,
      producing an empty report that looks like "no activity" rather than a
      configuration error. Naming the tenant explicitly removes the ambiguity,
      and PostgreSQL row-level security still applies underneath.
    * **``entry__entry_date``, never ``created_at``.** The accounting date is
      the date the transaction belongs to. A late-keyed December invoice has a
      December ``entry_date`` and a January ``created_at``; filtering on the
      latter moves revenue between filed periods.
    * **Dimension filters applied identically everywhere**, so a
      department-filtered P&L and a department-filtered trial balance agree.

    ``ignore_period`` is for as-of reports (balance sheet, aging): a balance is
    cumulative from the beginning of the ledger to a date, and applying a
    ``date_from`` to it would produce a "balance" that is really a period
    movement — a figure that still balances and is completely wrong.

    Index used: ``ix_entry_status`` (tenant, status, entry_date) drives the
    entry-side filter, and ``ix_line_account`` (tenant, account) the join back
    to lines. Reports that then group by a dimension are served by
    ``ix_line_project`` / ``ix_line_department`` / ``ix_line_partner``.
    """
    statuses: list[str] = [JournalEntry.Status.POSTED]
    if context.include_unposted:
        statuses.append(JournalEntry.Status.DRAFT)

    qs = JournalLine.all_tenants.filter(
        tenant_id=context.tenant_id,
        entry__tenant_id=context.tenant_id,
        entry__status__in=statuses,
    )

    if not ignore_period:
        start = date_from if date_from is not None else context.date_from
        if start is not None:
            qs = qs.filter(entry__entry_date__gte=start)

    end = date_to if date_to is not None else (context.date_to or context.as_of)
    if end is not None:
        qs = qs.filter(entry__entry_date__lte=end)

    if context.department_id is not None:
        qs = qs.filter(department_id=context.department_id)
    if context.project_id is not None:
        qs = qs.filter(project_id=context.project_id)

    return qs


#: Private alias kept because the specification and the call sites inside this
#: package refer to it by this name. Same function, no second implementation —
#: two spellings of one filter is exactly the drift this module exists to stop.
_ledger_query = ledger_query


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class ReportGenerator(abc.ABC):
    """Abstract base for one report.

    Stateless per call: instances are created inside Celery tasks that run
    concurrently for different tenants, and any state held on ``self`` between
    calls would be a cross-tenant leak of exactly the kind this system is
    built to make impossible. All state travels in the :class:`ReportContext`.

    Read-only by contract. A generator issues SELECTs and returns a
    :class:`ReportResult`. It does not write, cache, post or notify — those
    belong to ``apps.reporting.services`` and ``apps.reporting.tasks``, so that
    re-running last year's report to check an auditor's question cannot change
    anything.
    """

    #: Set by @register_report.
    report_type: str = ""
    #: Human title used as the report heading.
    title: str = ""
    #: True for statements drawn at an instant (balance sheet, aging,
    #: valuation) rather than over a period. Drives which context fields the
    #: default ``validate_context`` insists on.
    is_as_of: bool = False

    # -- hooks --------------------------------------------------------------

    def validate_context(self, context: ReportContext) -> None:
        """Refuse a context this report cannot honour — *before* querying.

        Overridden by subclasses that need more. The default enforces the
        period/instant distinction, because the most common way to get a wrong
        report is to hand a period report no period at all and silently
        receive "everything since the company was founded", which is a
        plausible-looking number that answers a different question.
        """
        if self.is_as_of:
            if context.as_of is None and context.date_to is None:
                raise ReportError(
                    f"{type(self).__name__} is an as-at report and needs "
                    f"`as_of` (or at least `date_to`). Without a cut-off it "
                    f"would report today's position under a historical title."
                )
            return
        if context.date_from is None or context.date_to is None:
            raise ReportError(
                f"{type(self).__name__} is a period report and needs both "
                f"`date_from` and `date_to`. An open-ended range silently "
                f"reports the whole ledger."
            )

    @abc.abstractmethod
    def generate(self, context: ReportContext) -> ReportResult:
        """Compute the report. Pure: reads only, returns a value.

        Implementations aggregate over :func:`ledger_query` (or, for the
        operational reports, over the relevant sub-ledger) and must never
        re-derive a monetary amount that the ledger already stores — always
        ``base_debit`` / ``base_credit``, never ``debit * exchange_rate``
        recomputed at report time. Recomputing means a corrected FX table
        silently changes a report that was already filed.
        """

    # -- template method ----------------------------------------------------

    def run(self, context: ReportContext) -> ReportResult:
        """Validate, then generate, then stamp shared metadata.

        Callers use this rather than :meth:`generate` directly so that the
        validation hook cannot be skipped by a caller who did not know it
        existed, and so the "this contains unposted drafts" warning is attached
        in one place instead of being remembered by twelve generators.
        """
        self.validate_context(context)
        result = self.generate(context)
        result.metadata.setdefault("title", self.title or self.report_type)
        result.metadata.setdefault("parameters", context.to_parameters())
        if not result.currency:
            result.currency = context.currency
        if context.include_unposted:
            result.warn(
                "UNPOSTED ENTRIES INCLUDED. This report contains draft journal "
                "entries, which are unbalanced by design, carry no document "
                "number and may never be posted. It is a management preview "
                "only and must not be filed, submitted to a lender, or used as "
                "the basis of a statutory return."
            )
        return result


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REPORT_REGISTRY: dict[str, Type[ReportGenerator]] = {}


def register_report(
    report_type: str,
) -> Callable[[Type[ReportGenerator]], Type[ReportGenerator]]:
    """Class decorator binding a generator to a ``ReportType`` value.

    A registry rather than an ``if/elif`` factory, mirroring
    ``apps.payments.gateways.base.register_gateway``: adding a report is one
    new module plus one import, with no edit to shared code, and a deployment
    can ship without a report it does not use.

    Re-registering the *same* class is a no-op (module reimport under the
    autoreloader). Registering a *different* class under a taken key raises:
    silent shadowing would mean the report a tenant scheduled is not the report
    they receive, and nothing would ever surface the substitution.
    """

    def decorator(cls: Type[ReportGenerator]) -> Type[ReportGenerator]:
        key = report_type.lower()
        existing = _REPORT_REGISTRY.get(key)
        if existing is not None and existing is not cls:
            raise ReportError(
                f"Report '{key}' is already registered to {existing.__name__}; "
                f"{cls.__name__} would silently shadow it."
            )
        cls.report_type = key
        _REPORT_REGISTRY[key] = cls
        return cls

    return decorator


def get_generator(report_type: str) -> ReportGenerator:
    """Build the generator for ``report_type``.

    Raises rather than returning None: an unregistered report is a deployment
    fault (a module that was never imported), and returning None only moves the
    ``AttributeError`` somewhere less informative — typically into a Celery
    task at 03:00, where the traceback says nothing about which report failed.
    """
    key = (report_type or "").lower()
    cls = _REPORT_REGISTRY.get(key)
    if cls is None:
        raise ReportError(
            f"No report generator registered for '{report_type}'. "
            f"Registered: {sorted(_REPORT_REGISTRY)}."
        )
    return cls()


def registered_reports() -> list[str]:
    return sorted(_REPORT_REGISTRY)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def net_balance(debit: Optional[Decimal], credit: Optional[Decimal],
                normal_side: str) -> Decimal:
    """Signed balance of an account, positive when it is on its normal side.

    Presenting every account as a positive number in the section it belongs to
    is what makes a statement readable: revenue (a credit balance) printed as
    negative, or accumulated depreciation printed as positive, both read as
    errors to anyone checking the arithmetic by hand.
    """
    dr = debit or ZERO
    cr = credit or ZERO
    return (dr - cr) if normal_side == "debit" else (cr - dr)


def sum_decimals(values: Iterable[Optional[Decimal]]) -> Decimal:
    """Sum that treats NULL aggregates as zero and never touches float.

    ``Sum()`` over an empty set returns ``None``; propagating that into
    arithmetic raises a ``TypeError`` deep inside a report instead of showing
    a legitimate zero.
    """
    return sum((v for v in values if v is not None), ZERO)


def as_money(value: Any) -> Decimal:
    """Coerce a report input to Decimal, refusing floats outright.

    Delegates to ``apps.core.fields.to_money`` so the "floats are forbidden"
    rule has exactly one implementation in the codebase.
    """
    if value is None:
        return ZERO
    return to_money(value, field_name="report_amount")


def q_any(filters: Sequence[Q]) -> Q:
    """OR a list of Q objects, returning a match-nothing Q when empty.

    An empty ``Q()`` means "no filter", i.e. *everything*. Returning that for
    an empty selector list is how an aging report ends up summing the entire
    ledger instead of the AR control accounts.
    """
    if not filters:
        return Q(pk__in=[])
    combined = filters[0]
    for extra in filters[1:]:
        combined |= extra
    return combined
