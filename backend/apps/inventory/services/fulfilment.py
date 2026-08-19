"""
Document-level stock fulfilment — the bridge between sales and inventory.

``apps.sales.services.invoice_workflow`` needs to say "this invoice ships
these lines" without knowing about warehouses, costing methods or COGS
accounts. This module is that seam: it takes a document and a list of line
dicts and fans them out to :func:`apps.inventory.services.stock.apply_movement`,
which owns the locking, valuation and GL posting.

Why a separate module rather than another function in ``stock.py``:
``sales`` imports this, and ``inventory.models`` imports ``sales`` for its
document links. Keeping the sales-facing entry point in its own module makes
the lazy import in ``invoice_workflow`` a one-line, obviously-safe cycle
break instead of a comment asking the next reader to trust it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.fields import to_money
from apps.inventory.models import Item, StockMovement, Warehouse
from apps.inventory.services.stock import MovementResult, apply_movement


class NoDefaultWarehouse(ValidationError):
    """Raised when a document ships stock but the tenant has no warehouse.

    Its own class because the fix is a configuration action ("mark a warehouse
    as default"), not a data correction — the API maps it to a distinct error
    code so the client can link straight to the settings screen.
    """


def _resolve_warehouse(tenant_id: uuid.UUID, warehouse_id: Optional[uuid.UUID]) -> Warehouse:
    if warehouse_id is not None:
        warehouse = Warehouse.all_tenants.filter(
            tenant_id=tenant_id, id=warehouse_id, is_active=True
        ).first()
        if warehouse is None:
            raise ValidationError(f"Warehouse {warehouse_id} not found or inactive.")
        return warehouse

    warehouse = Warehouse.all_tenants.filter(
        tenant_id=tenant_id, is_default=True, is_active=True
    ).first()
    if warehouse is None:
        raise NoDefaultWarehouse(
            "This document ships stock but the tenant has no default warehouse. "
            "Mark one warehouse as default before issuing stock documents."
        )
    return warehouse


def _as_datetime(value: date | datetime | None) -> datetime:
    """Normalise a document *date* to an aware datetime for the movement log.

    Stock movements are ordered by ``occurred_at`` and the running balance
    depends on that order, so a naive datetime here would order incorrectly
    against timezone-aware rows already in the table.
    """
    if value is None:
        return timezone.now()
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return timezone.make_aware(datetime.combine(value, time(12, 0)))


@transaction.atomic
def issue_stock(
    *,
    tenant_id: uuid.UUID,
    occurred_on: date | datetime | None,
    document_type: str,
    document_id: uuid.UUID,
    reference: str = "",
    user_id: Optional[uuid.UUID] = None,
    lines: Sequence[dict[str, Any]],
    warehouse_id: Optional[uuid.UUID] = None,
) -> list[MovementResult]:
    """Decrement stock for every line of an outbound document.

    Each line is a dict with ``item_id``, ``quantity`` and optionally
    ``project_id``, ``warehouse_id`` and ``source_line_id``.

    Runs inside the caller's transaction: if any line has insufficient stock,
    the whole invoice issue rolls back rather than leaving a half-shipped
    document with a posted revenue entry. That is the entire reason this
    function does not swallow per-line errors.

    Idempotency is per source line (``issue:<document_id>:<source_line_id>``),
    not per document, so a retry that partially succeeded resumes cleanly
    instead of double-decrementing the lines that already went through.
    """
    return _apply_document_lines(
        tenant_id=tenant_id,
        occurred_on=occurred_on,
        document_type=document_type,
        document_id=document_id,
        reference=reference,
        user_id=user_id,
        lines=lines,
        warehouse_id=warehouse_id,
        movement_type=StockMovement.MovementType.SALE,
        direction=Decimal("-1"),
        key_verb="issue",
    )


@transaction.atomic
def receive_stock(
    *,
    tenant_id: uuid.UUID,
    occurred_on: date | datetime | None,
    document_type: str,
    document_id: uuid.UUID,
    reference: str = "",
    user_id: Optional[uuid.UUID] = None,
    lines: Sequence[dict[str, Any]],
    warehouse_id: Optional[uuid.UUID] = None,
) -> list[MovementResult]:
    """Increment stock for an inbound document (vendor bill, customer return).

    Lines may carry ``unit_cost``; without it the item's ``purchase_price`` is
    used. Cost matters here in a way it does not on issue — it is what feeds
    the weighted-average valuation that every later COGS posting depends on.
    """
    return _apply_document_lines(
        tenant_id=tenant_id,
        occurred_on=occurred_on,
        document_type=document_type,
        document_id=document_id,
        reference=reference,
        user_id=user_id,
        lines=lines,
        warehouse_id=warehouse_id,
        movement_type=StockMovement.MovementType.PURCHASE,
        direction=Decimal("1"),
        key_verb="receive",
    )


@transaction.atomic
def return_stock(
    *,
    tenant_id: uuid.UUID,
    occurred_on: date | datetime | None,
    document_type: str,
    document_id: uuid.UUID,
    reference: str = "",
    user_id: Optional[uuid.UUID] = None,
    lines: Sequence[dict[str, Any]],
    warehouse_id: Optional[uuid.UUID] = None,
) -> list[MovementResult]:
    """Put stock back for a credit note. Mirrors :func:`issue_stock`.

    Deliberately a distinct movement type rather than a negative sale: the
    inventory report must be able to show returns separately, and netting
    them into sales makes a returns spike invisible.
    """
    return _apply_document_lines(
        tenant_id=tenant_id,
        occurred_on=occurred_on,
        document_type=document_type,
        document_id=document_id,
        reference=reference,
        user_id=user_id,
        lines=lines,
        warehouse_id=warehouse_id,
        movement_type=StockMovement.MovementType.RETURN_IN,
        direction=Decimal("1"),
        key_verb="return",
    )


def _apply_document_lines(
    *,
    tenant_id: uuid.UUID,
    occurred_on: date | datetime | None,
    document_type: str,
    document_id: uuid.UUID,
    reference: str,
    user_id: Optional[uuid.UUID],
    lines: Sequence[dict[str, Any]],
    warehouse_id: Optional[uuid.UUID],
    movement_type: str,
    direction: Decimal,
    key_verb: str,
) -> list[MovementResult]:
    occurred_at = _as_datetime(occurred_on)
    results: list[MovementResult] = []

    # Resolve every item up front in one query. Doing it per line turns a
    # 40-line invoice into 40 round trips, and worse, spreads the "unknown
    # item" failure across the loop after some movements have been written.
    item_ids = {line["item_id"] for line in lines if line.get("item_id")}
    items = {
        item.id: item
        for item in Item.all_tenants.filter(tenant_id=tenant_id, id__in=item_ids)
    }
    missing = item_ids - items.keys()
    if missing:
        raise ValidationError(
            f"Items not found in this tenant: {sorted(str(m) for m in missing)}"
        )

    default_warehouse = None
    for index, line in enumerate(lines):
        item = items[line["item_id"]]
        if not item.track_inventory:
            # Services and non-inventory items have no stock ledger by
            # definition. Silently skipping is correct here — the caller
            # should not have to filter, and raising would break every
            # invoice that mixes goods and labour on one document.
            continue

        line_warehouse_id = line.get("warehouse_id") or warehouse_id
        if line_warehouse_id is None:
            if default_warehouse is None:
                default_warehouse = _resolve_warehouse(tenant_id, None)
            warehouse = default_warehouse
        else:
            warehouse = _resolve_warehouse(tenant_id, line_warehouse_id)

        quantity = to_money(line["quantity"], field_name="quantity")
        if quantity <= 0:
            raise ValidationError(
                f"Line {index + 1}: quantity must be positive; the movement "
                f"direction is decided by the document type, not by the sign."
            )

        source_line_id = line.get("source_line_id") or f"{index}"
        results.append(
            apply_movement(
                tenant_id=tenant_id,
                item=item,
                warehouse=warehouse,
                movement_type=movement_type,
                quantity_delta=quantity * direction,
                unit_cost=line.get("unit_cost"),
                occurred_at=occurred_at,
                reference_type=document_type,
                reference_id=document_id,
                consume_reservation=(direction < 0),
                post_to_ledger=True,
                idempotency_key=f"{key_verb}:{document_id}:{source_line_id}",
                notes=reference,
                user_id=user_id,
            )
        )
    return results
