"""
Taking and comparing report snapshots — the evidence layer.

:func:`generate_and_snapshot` is the only sanctioned way to produce a report
that will be *shown to somebody outside the company*. Running a generator
directly is fine for a screen; the moment a figure is filed, emailed to a
lender or attached to a return, there must be a row saying exactly what was
sent and proving it has not been edited since.

:func:`compare_snapshots` answers the question that follows three months
later: "why did last quarter's P&L change?". Without stored snapshots the only
available answer is "the ledger moved", which is true and useless. With them,
the answer is a list of lines and amounts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from django.db import transaction

from apps.core.fields import ZERO
from apps.reporting.generators.base import (
    ReportContext,
    ReportError,
    ReportGenerator,
    ReportResult,
    get_generator,
)
from apps.reporting.models import ReportDefinition, ReportSnapshot

__all__ = [
    "generate_and_snapshot",
    "compare_snapshots",
    "canonical_json",
    "compute_checksum",
    "SnapshotDiff",
]


# ---------------------------------------------------------------------------
# Canonicalisation and checksum
# ---------------------------------------------------------------------------

def _json_default(value: Any) -> Any:
    """Last-resort encoder for values the payload should not contain anyway.

    ``ReportResult.to_dict`` already converts Decimals to strings, so anything
    reaching here is an object a generator put in ``metadata`` or a line's
    ``meta`` — a UUID, a date, or an ABAC ``Q`` handed in as a parameter.
    Stringifying is right for the first two. A ``float`` is *not* stringified:
    it is refused, because a float in a snapshot payload is a monetary value
    that has already lost precision and freezing it would make the corruption
    permanent and officially attested.
    """
    if isinstance(value, float):
        raise ReportError(
            f"Float {value!r} reached snapshot serialisation. Monetary values "
            f"must be Decimal end to end; a float has already lost precision "
            f"and must not be frozen into evidence."
        )
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    return str(value)


def canonical_json(payload: Any) -> str:
    """One and only one textual form for a given payload.

    ``sort_keys`` and fixed separators are what make the checksum a property of
    the *content* rather than of the dict ordering that happened to come out of
    Python on the day. Without them, re-serialising the identical report on a
    different Python version produces a different digest, and the checksum
    stops proving anything — the one thing it exists to do.

    ``ensure_ascii=False`` keeps Arabic account names as themselves rather than
    as escape sequences; the encoding is pinned to UTF-8 at the digest step, so
    the bytes are still deterministic.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def compute_checksum(payload: Any) -> str:
    """sha256 over the canonical JSON. 64 lowercase hex characters.

    sha256 rather than a cheaper hash because this is a tamper-evidence
    control, not a cache key: the property required is that nobody can produce
    a different set of figures with the same digest.
    """
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Taking a snapshot
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_and_snapshot(
    report_type: str,
    context: ReportContext,
    user_id: Optional[uuid.UUID] = None,
    *,
    definition: Optional[ReportDefinition] = None,
    generator: Optional[ReportGenerator] = None,
    file_key: str = "",
    file_format: str = "",
) -> ReportSnapshot:
    """Run a report and freeze the result as a :class:`ReportSnapshot`.

    Why the whole thing is one transaction
    --------------------------------------
    A report is many aggregate queries — a P&L reads the journal two or three
    times, a cash-flow statement four or five. Outside a transaction each of
    those queries sees a *different* committed state of the database, because
    postings are happening concurrently. The result is a statement whose
    sections were computed from different ledgers: net profit from one
    instant, working capital from another, and a reconciliation difference
    that no amount of investigation will explain because it never existed in
    any single state of the data.

    Running inside one atomic block gives every query the same snapshot of the
    database (PostgreSQL's MVCC does the work; under the default READ
    COMMITTED each *statement* gets a fresh snapshot, so callers who need
    absolute rigour should set ``REPEATABLE READ`` on the connection for
    report workers). The transaction is also where the snapshot row is
    written, so the evidence and the figures it attests to cannot diverge:
    either both land or neither does.

    Nothing in here posts, updates a balance or touches a sub-ledger. The only
    write is the ``ReportSnapshot`` insert. That is deliberate — a report that
    can change data cannot be re-run to check an auditor's question.

    ``definition`` is optional because ad-hoc reports get filed too; see
    :class:`ReportSnapshot`.
    """
    generator = generator if generator is not None else get_generator(report_type)
    result: ReportResult = generator.run(context)

    payload = result.to_dict()
    checksum = compute_checksum(payload)

    snapshot = ReportSnapshot(
        tenant_id=context.tenant_id,
        definition=definition,
        report_type=report_type,
        parameters=json.loads(canonical_json(context.to_parameters())),
        period_start=context.date_from,
        period_end=context.date_to,
        as_of_date=context.effective_as_of,
        generated_at=result.generated_at,
        generated_by_id=user_id,
        payload=payload,
        row_count=result.row_count,
        checksum=checksum,
        file_key=file_key,
        file_format=file_format,
        warnings=list(result.warnings),
        currency=result.currency or context.currency,
        created_by_id=user_id,
    )
    snapshot.save()
    return snapshot


def verify_snapshot(snapshot: ReportSnapshot) -> bool:
    """Recompute the checksum and compare. True means untampered.

    Cheap enough to run whenever a snapshot is served to an external party,
    and the only way to detect a payload edited by a direct UPDATE — which the
    ORM's immutability guard does not prevent, because it only blocks
    ``delete()``.
    """
    return compute_checksum(snapshot.payload) == snapshot.checksum


# ---------------------------------------------------------------------------
# Comparing snapshots
# ---------------------------------------------------------------------------

class SnapshotDiff(dict):
    """A structured difference between two snapshots.

    A ``dict`` subclass rather than a dataclass so it drops straight into a
    JSON response and into a ``ReportSnapshot.payload``-shaped log without a
    serialiser, while still being able to carry the convenience properties
    below.
    """

    @property
    def has_changes(self) -> bool:
        return bool(
            self.get("total_changes")
            or self.get("line_changes")
            or self.get("added_lines")
            or self.get("removed_lines")
            or self.get("parameter_changes")
        )


def _decimal(value: Any) -> Decimal:
    """Parse an amount back out of a snapshot payload.

    Payload amounts are strings (see ``ReportResult.to_dict``). Parsing them
    with ``Decimal`` is lossless; parsing them with ``float`` would reintroduce
    exactly the corruption the string form exists to prevent, and a diff
    computed in float would report spurious differences of 1e-15 between two
    identical reports.
    """
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ReportError(f"Snapshot contains a non-numeric amount: {value!r}") from exc


def _index_lines(payload: dict[str, Any]) -> dict[tuple[str, str], Decimal]:
    """Flatten a payload into ``{(section_key, line_key): amount}``.

    Lines are keyed by account code where there is one and by label otherwise.
    Account code is preferred because a tenant renaming an account must not
    make every line look like a removal plus an addition — which is what
    keying on the label alone produces, and it buries the one line that
    actually changed under forty that did not.
    """
    index: dict[tuple[str, str], Decimal] = {}
    for section in payload.get("sections") or []:
        section_key = str(section.get("key", ""))
        for line in section.get("lines") or []:
            line_key = str(line.get("account_code") or line.get("label") or "")
            key = (section_key, line_key)
            # Two lines with the same key inside one section are summed rather
            # than one silently winning; a lost line would read as "unchanged".
            index[key] = index.get(key, ZERO) + _decimal(line.get("amount"))
    return index


def compare_snapshots(a: ReportSnapshot, b: ReportSnapshot) -> SnapshotDiff:
    """Structured diff of two snapshots — "why did last quarter's P&L change?".

    ``a`` is the earlier/baseline snapshot, ``b`` the later one. Every movement
    is reported as ``b - a``, so a positive delta means "went up since the
    figure we filed".

    The comparison is refused when the two snapshots are not comparable —
    different report types, or different periods. Diffing a Q1 P&L against a Q2
    P&L produces a page of large differences that are entirely expected and
    tell you nothing, and the resulting noise is how a real, small,
    prior-period restatement gets overlooked. Parameters that differ in a way
    that still permits comparison (a department filter, drafts included) are
    reported as ``parameter_changes`` and are usually the answer on their own.
    """
    if a.report_type != b.report_type:
        raise ReportError(
            f"Cannot compare a '{a.report_type}' snapshot with a "
            f"'{b.report_type}' one: the sections do not mean the same thing, "
            f"so every line would appear both added and removed."
        )
    if (a.period_start, a.period_end) != (b.period_start, b.period_end):
        raise ReportError(
            f"Refusing to compare different periods "
            f"({a.period_start}..{a.period_end} vs "
            f"{b.period_start}..{b.period_end}). The question this function "
            f"answers is 'why did the *same* period's figures change?'; "
            f"diffing two different periods reports ordinary business "
            f"movement as if it were a restatement."
        )

    diff = SnapshotDiff(
        report_type=a.report_type,
        baseline={
            "id": str(a.id),
            "generated_at": a.generated_at.isoformat() if a.generated_at else None,
            "checksum": a.checksum,
        },
        current={
            "id": str(b.id),
            "generated_at": b.generated_at.isoformat() if b.generated_at else None,
            "checksum": b.checksum,
        },
        identical=a.checksum == b.checksum,
        period={
            "start": a.period_start.isoformat() if a.period_start else None,
            "end": a.period_end.isoformat() if a.period_end else None,
        },
    )

    # -- parameters ---------------------------------------------------------
    parameter_changes: dict[str, dict[str, Any]] = {}
    for key in sorted(set(a.parameters or {}) | set(b.parameters or {})):
        before = (a.parameters or {}).get(key)
        after = (b.parameters or {}).get(key)
        if before != after:
            parameter_changes[key] = {"before": before, "after": after}
    diff["parameter_changes"] = parameter_changes

    # -- statement totals ---------------------------------------------------
    totals_a = (a.payload or {}).get("totals") or {}
    totals_b = (b.payload or {}).get("totals") or {}
    total_changes: dict[str, dict[str, str]] = {}
    for key in sorted(set(totals_a) | set(totals_b)):
        before = _decimal(totals_a.get(key))
        after = _decimal(totals_b.get(key))
        if before != after:
            total_changes[key] = {
                "before": format(before, "f"),
                "after": format(after, "f"),
                "delta": format(after - before, "f"),
            }
    diff["total_changes"] = total_changes

    # -- line level ---------------------------------------------------------
    lines_a = _index_lines(a.payload or {})
    lines_b = _index_lines(b.payload or {})

    line_changes: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []

    for key in sorted(set(lines_a) | set(lines_b)):
        section_key, line_key = key
        before = lines_a.get(key)
        after = lines_b.get(key)
        if before is None:
            added.append(
                {"section": section_key, "line": line_key,
                 "amount": format(after or ZERO, "f")}
            )
        elif after is None:
            removed.append(
                {"section": section_key, "line": line_key,
                 "amount": format(before, "f")}
            )
        elif before != after:
            line_changes.append(
                {
                    "section": section_key,
                    "line": line_key,
                    "before": format(before, "f"),
                    "after": format(after, "f"),
                    "delta": format(after - before, "f"),
                }
            )

    # Largest movements first: the explanation for a changed total is almost
    # always one or two lines, and making the reader scan an alphabetical list
    # for them is how "why did this change?" stays unanswered.
    line_changes.sort(key=lambda change: abs(Decimal(change["delta"])), reverse=True)

    diff["line_changes"] = line_changes
    diff["added_lines"] = added
    diff["removed_lines"] = removed
    diff["summary"] = (
        f"{len(total_changes)} total(s), {len(line_changes)} line(s) changed, "
        f"{len(added)} added, {len(removed)} removed."
    )
    return diff
