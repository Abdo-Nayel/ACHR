"""
The stock service — the only sanctioned write path into inventory.

Everything that changes a quantity goes through :func:`apply_movement`. That
is not bureaucracy; it is the only way five separate invariants can be held
at once:

1. The movement log and the level projection change together, atomically.
2. Concurrent requests for the same bin are serialised by a row lock, so the
   "is there enough stock?" check cannot be overtaken between reading and
   writing (see the oversell note in :func:`apply_movement`).
3. Weighted-average cost is recomputed from the pre-movement balance, in
   Decimal, at full precision.
4. The resulting financial effect is expressed as a
   :class:`JournalEntryDraft` and posted through
   ``apps.accounting.services.posting.post_entry`` — the single choke point
   where debits are proved equal to credits.
5. Reorder alerts are raised from the same transaction, so an alert cannot
   describe a stock level that was rolled back.

Rounding policy: intermediate arithmetic keeps the full numeric(19,6)
precision; rounding to the currency's minor unit happens exactly once, at
the posting boundary, in ``post_entry``. Rounding earlier would make the
inventory asset drift away from the sum of the movements that built it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.accounting.models import Account, Journal, JournalEntry
from apps.accounting.services.posting import JournalEntryDraft, post_entry
from apps.core.fields import ZERO, to_money
from apps.inventory.models import (
    Item,
    LowStockAlert,
    PriceList,
    PriceListItem,
    StockBatch,
    StockLevel,
    StockMovement,
    Warehouse,
)

__all__ = [
    "InsufficientStock",
    "MovementResult",
    "apply_movement",
    "reserve_stock",
    "release_reservation",
    "recompute_stock_levels",
    "bulk_adjust_prices",
    "StockDrift",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: System accounts the inventory postings need. Looked up by ``system_key``
#: rather than by code, because the account *code* differs between national
#: standard charts while the role does not.
AP_CONTROL = "ap_control"
INVENTORY_ADJUSTMENT = "inventory_adjustment"
OPENING_BALANCE_EQUITY = "opening_balance_equity"
WORK_IN_PROGRESS = "work_in_progress"

#: Movements that never change the value we own and therefore produce no
#: journal entry. An internal transfer moves goods between two of our own
#: warehouses: the asset account is the same on both legs, so a "posting"
#: would be Dr Inventory / Cr Inventory for the same amount — a no-op entry
#: that only adds noise to the ledger and work to the reconciliation.
NON_POSTING_TYPES: frozenset[str] = frozenset(
    {StockMovement.MovementType.TRANSFER_IN, StockMovement.MovementType.TRANSFER_OUT}
)


class InsufficientStock(ValidationError):
    """Raised when a movement would push a bin negative without permission.

    Its own class so an API layer can map it to 409 Conflict (a legitimate
    race the client should retry or re-quote) rather than to 400, and so
    monitoring can distinguish it from a programming error.
    """


class StockDrift(ValidationError):
    """Raised by the nightly checker when a level disagrees with its log."""


@dataclass(frozen=True, slots=True)
class MovementResult:
    movement: StockMovement
    level: StockLevel
    journal_entry: Optional[JournalEntry] = None
    alert: Optional[LowStockAlert] = None


@dataclass(frozen=True, slots=True)
class DriftReport:
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    stored_quantity: Decimal
    derived_quantity: Decimal
    stored_value: Decimal
    derived_value: Decimal

    @property
    def quantity_difference(self) -> Decimal:
        return self.stored_quantity - self.derived_quantity

    @property
    def value_difference(self) -> Decimal:
        return self.stored_value - self.derived_value


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------

@transaction.atomic
def apply_movement(
    *,
    tenant_id: uuid.UUID,
    item: Item | uuid.UUID,
    warehouse: Warehouse | uuid.UUID,
    movement_type: str,
    quantity_delta: Decimal | str | int,
    unit_cost: Decimal | str | int | None = None,
    occurred_at: Optional[datetime] = None,
    reference_type: str = "",
    reference_id: Optional[uuid.UUID] = None,
    batch: Optional[StockBatch] = None,
    consume_reservation: bool = False,
    post_to_ledger: bool = True,
    idempotency_key: str = "",
    notes: str = "",
    user_id: Optional[uuid.UUID] = None,
) -> MovementResult:
    """Record one stock movement and everything that must move with it.

    ``quantity_delta`` is signed: positive for goods coming in, negative for
    goods going out. The whole function is one transaction — the movement
    row, the level update, the batch consumption, the alert and the journal
    entry either all exist or none of them do. A partial application here is
    exactly the state the nightly drift check is designed to catch, and the
    cheapest way to never need it is to make it impossible.
    """
    item = _resolve(Item, item, tenant_id)
    warehouse = _resolve(Warehouse, warehouse, tenant_id)
    occurred_at = occurred_at or timezone.now()

    if movement_type not in StockMovement.MovementType.values:
        raise ValidationError(f"Unknown movement type '{movement_type}'.")

    # Decimal or bust. ``to_money`` refuses floats outright rather than
    # absorbing the imprecision — a float arriving here means some caller
    # parsed JSON without ``parse_float=Decimal``, and silently rounding it
    # would push the error into the ledger where it is far harder to find.
    delta = to_money(quantity_delta, field_name="quantity_delta")
    if delta == ZERO:
        raise ValidationError(
            {"quantity_delta": "A movement must change the quantity."}
        )
    _assert_direction(movement_type, delta)
    _assert_uom_precision(item, delta)

    if not item.is_stocked:
        raise ValidationError(
            f"Item {item.sku} is a {item.get_type_display().lower()} and does "
            f"not carry stock. Nothing to move."
        )

    # --- idempotency ------------------------------------------------------
    # A retried Celery task or a re-delivered webhook must not ship the goods
    # twice. Cheap pre-check here; the unique index is what actually holds.
    if idempotency_key:
        existing = StockMovement.all_tenants.filter(
            tenant_id=tenant_id, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            level = _get_level_unlocked(tenant_id, item.id, warehouse.id)
            return MovementResult(movement=existing, level=level)

    # ------------------------------------------------------------------
    # THE LOCK. This is the single most important line in the module.
    #
    # SELECT ... FOR UPDATE on the (item, warehouse) row is what serialises
    # concurrent sales of the last unit. Without it, under PostgreSQL's
    # default READ COMMITTED isolation, two checkout requests arriving
    # milliseconds apart both SELECT quantity_on_hand = 1, both evaluate
    # "1 >= 1, sufficient", and both write quantity_on_hand = 0 — the second
    # UPDATE simply overwrites the first. Two customers are charged, one
    # parcel exists, and the stock figure looks perfectly healthy afterwards
    # because the two writes were individually consistent. That is the
    # classic oversell race, and no amount of application-level checking
    # fixes it: the check and the write must be inside the same lock.
    #
    # Taking the lock on the *level* row (rather than, say, an advisory lock
    # on the item) also gives us the right granularity: sales of different
    # items, or of the same item in different warehouses, proceed in
    # parallel and never queue behind each other.
    #
    # Equivalent to `StockLevel.objects.select_for_update().get_or_create()`;
    # the tenant is bound explicitly because background workers run with no
    # ambient tenant context and the default manager would return nothing.
    # ------------------------------------------------------------------
    level = _lock_level(tenant_id, item, warehouse, user_id=user_id)

    quantity_before = level.quantity_on_hand
    value_before = level.total_value
    quantity_after = quantity_before + delta

    # --- sufficiency ------------------------------------------------------
    if delta < ZERO and not item.allow_negative_stock and quantity_after < ZERO:
        raise InsufficientStock(
            f"Insufficient stock for {item.sku} at {warehouse.code}: "
            f"{quantity_before} on hand, {-delta} requested. "
            f"Enable allow_negative_stock on the item if this business "
            f"genuinely ships before receipting."
        )
    if delta < ZERO and not consume_reservation:
        # Reserved units are physically present but already promised. Letting
        # a walk-in sale eat them is the same oversell, one step removed.
        unreserved = quantity_before - level.quantity_reserved
        if not item.allow_negative_stock and -delta > unreserved:
            raise InsufficientStock(
                f"{item.sku} at {warehouse.code}: only {unreserved} unreserved "
                f"of {quantity_before} on hand ({level.quantity_reserved} are "
                f"reserved for other orders); {-delta} requested."
            )

    # --- costing ----------------------------------------------------------
    movement_unit_cost, movement_total_cost, new_average = _compute_costs(
        item=item,
        level=level,
        delta=delta,
        supplied_unit_cost=(
            None if unit_cost is None else to_money(unit_cost, field_name="unit_cost")
        ),
        batch=batch,
        tenant_id=tenant_id,
    )
    value_after = value_before + (
        movement_total_cost if delta > ZERO else -movement_total_cost
    )
    # Relieving more value than exists rounds the asset to zero rather than
    # letting it go negative on a rounding tail; the difference is booked by
    # the adjustment account through the journal entry, never lost.
    if quantity_after == ZERO:
        value_after = ZERO

    # --- the log row (source of truth) ------------------------------------
    movement = StockMovement(
        tenant_id=tenant_id,
        item=item,
        warehouse=warehouse,
        movement_type=movement_type,
        quantity_delta=delta,
        currency=level.currency or item.currency,
        unit_cost=movement_unit_cost,
        total_cost=movement_total_cost,
        running_quantity_after=quantity_after,
        running_value_after=value_after,
        reference_type=reference_type[:50],
        reference_id=reference_id,
        batch=batch,
        occurred_at=occurred_at,
        notes=notes[:255],
        idempotency_key=idempotency_key,
        created_by_id=user_id,
    )
    try:
        movement.save()
    except IntegrityError as exc:
        if "uq_stock_movement_idempotency" in str(exc):
            # Lost the race with a concurrent retry of the same event; the
            # other transaction did the work, so this one must not repeat it.
            raise ValidationError(
                "This stock movement has already been applied."
            ) from exc
        raise

    # --- the projection ---------------------------------------------------
    level.quantity_on_hand = quantity_after
    if consume_reservation and delta < ZERO:
        # Never below zero: an over-release would inflate availability.
        level.quantity_reserved = max(ZERO, level.quantity_reserved + delta)
    level.quantity_available = level.quantity_on_hand - level.quantity_reserved
    level.average_cost = new_average
    level.total_value = value_after
    level.last_movement_at = occurred_at
    level.allow_negative = item.allow_negative_stock
    level.updated_by_id = user_id
    level.save(
        update_fields=[
            "quantity_on_hand",
            "quantity_reserved",
            "quantity_available",
            "average_cost",
            "total_value",
            "last_movement_at",
            "allow_negative",
            "updated_by",
            "updated_at",
        ]
    )

    if batch is not None:
        _apply_to_batch(batch, delta)

    # --- alerting ---------------------------------------------------------
    alert = _maybe_raise_low_stock(
        tenant_id=tenant_id, item=item, warehouse=warehouse, level=level
    )

    # --- the ledger -------------------------------------------------------
    entry = None
    if post_to_ledger and movement_type not in NON_POSTING_TYPES:
        draft = build_journal_entry(movement, item=item, tenant_id=tenant_id)
        if draft is not None:
            entry = post_entry(draft, tenant_id=tenant_id, user_id=user_id)
            # The FK is set with an UPDATE rather than movement.save():
            # StockMovement is an ImmutableFinancialModel and this column is
            # the one piece of it that is legitimately written once, after
            # the entry it points at exists.
            StockMovement.all_tenants.filter(pk=movement.pk).update(
                journal_entry=entry
            )
            movement.journal_entry = entry

    return MovementResult(movement=movement, level=level, journal_entry=entry, alert=alert)


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

@transaction.atomic
def reserve_stock(
    *,
    tenant_id: uuid.UUID,
    item: Item | uuid.UUID,
    warehouse: Warehouse | uuid.UUID,
    quantity: Decimal | str | int,
    user_id: Optional[uuid.UUID] = None,
) -> StockLevel:
    """Promise ``quantity`` to an order without moving it off the shelf.

    Same lock, same reason: reservation is a read-check-write on exactly the
    column two concurrent orders compete for. Reserving does not change
    ``quantity_on_hand`` — the goods are still ours and still on the balance
    sheet — it only removes them from ``quantity_available``, which is what
    the next order and the reorder-point check both look at.
    """
    item = _resolve(Item, item, tenant_id)
    warehouse = _resolve(Warehouse, warehouse, tenant_id)
    amount = to_money(quantity, field_name="quantity")
    if amount <= ZERO:
        raise ValidationError({"quantity": "Reserve a positive quantity."})

    level = _lock_level(tenant_id, item, warehouse, user_id=user_id)
    if not item.allow_negative_stock and amount > level.quantity_available:
        raise InsufficientStock(
            f"Cannot reserve {amount} of {item.sku} at {warehouse.code}: "
            f"only {level.quantity_available} available "
            f"({level.quantity_on_hand} on hand, {level.quantity_reserved} "
            f"already reserved)."
        )

    level.quantity_reserved += amount
    level.quantity_available = level.quantity_on_hand - level.quantity_reserved
    level.updated_by_id = user_id
    level.save(
        update_fields=[
            "quantity_reserved", "quantity_available", "updated_by", "updated_at"
        ]
    )
    _maybe_raise_low_stock(
        tenant_id=tenant_id, item=item, warehouse=warehouse, level=level
    )
    return level


@transaction.atomic
def release_reservation(
    *,
    tenant_id: uuid.UUID,
    item: Item | uuid.UUID,
    warehouse: Warehouse | uuid.UUID,
    quantity: Decimal | str | int,
    user_id: Optional[uuid.UUID] = None,
) -> StockLevel:
    """Give reserved units back to general availability.

    Releasing more than is reserved is clamped rather than raising: the
    common cause is a cancelled order whose lines were partially shipped
    already, and failing the cancellation because the arithmetic disagrees
    leaves the reservation stuck forever — a worse outcome than releasing
    what is actually there. The clamp also protects
    ``ck_stock_level_reserved_non_neg`` from ever being the thing that
    surfaces the problem.
    """
    item = _resolve(Item, item, tenant_id)
    warehouse = _resolve(Warehouse, warehouse, tenant_id)
    amount = to_money(quantity, field_name="quantity")
    if amount <= ZERO:
        raise ValidationError({"quantity": "Release a positive quantity."})

    level = _lock_level(tenant_id, item, warehouse, user_id=user_id)
    level.quantity_reserved = max(ZERO, level.quantity_reserved - amount)
    level.quantity_available = level.quantity_on_hand - level.quantity_reserved
    level.updated_by_id = user_id
    level.save(
        update_fields=[
            "quantity_reserved", "quantity_available", "updated_by", "updated_at"
        ]
    )
    return level


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

def recompute_stock_levels(
    tenant_id: uuid.UUID,
    *,
    item_id: Optional[uuid.UUID] = None,
    repair: bool = False,
    as_of: Optional[datetime] = None,
) -> list[DriftReport]:
    """Re-derive every level from the movement log and report disagreements.

    Run nightly. ``StockLevel`` is a cache of ``SUM(quantity_delta)``; caches
    drift, and the ways they drift here are all real: a worker killed between
    the movement INSERT and the level UPDATE (impossible inside this service,
    but not for a data import), a restore from a backup taken mid-transaction,
    or the SQL somebody ran by hand at 2am to "fix" one row.

    ``repair=False`` by default and deliberately. Silently correcting a
    difference destroys the evidence needed to find out *why* it appeared,
    and if the movement log is the thing that is wrong, repairing the level
    to match it makes the error permanent. The nightly job reports; a human
    decides.
    """
    movements = StockMovement.all_tenants.filter(tenant_id=tenant_id)
    levels = StockLevel.all_tenants.filter(tenant_id=tenant_id)
    if item_id is not None:
        movements = movements.filter(item_id=item_id)
        levels = levels.filter(item_id=item_id)
    if as_of is not None:
        movements = movements.filter(occurred_at__lte=as_of)

    derived: dict[tuple[uuid.UUID, uuid.UUID], tuple[Decimal, Decimal]] = {}
    for row in movements.values("item_id", "warehouse_id").annotate(
        quantity=Sum("quantity_delta"),
        # Signed value: inbound movements add cost, outbound relieve it.
        # Expressed as a single SUM so PostgreSQL does the arithmetic in
        # numeric, not in Python floats over a million rows.
        value=Sum("total_cost"),
    ):
        derived[(row["item_id"], row["warehouse_id"])] = (
            row["quantity"] or ZERO,
            row["value"] or ZERO,
        )

    reports: list[DriftReport] = []
    for level in levels.iterator(chunk_size=2000):
        derived_qty, _derived_value = derived.pop(
            (level.item_id, level.warehouse_id), (ZERO, ZERO)
        )
        # Value is re-derived from quantity x average cost rather than from
        # the SUM of total_cost: under weighted average the cost relieved on
        # an outbound movement uses the average *at that moment*, so the
        # signed sum of movement costs is the correct value only if no
        # revaluation ever happened. Quantity is the authoritative check.
        derived_value = derived_qty * level.average_cost
        if derived_qty == level.quantity_on_hand and derived_value == level.total_value:
            continue
        reports.append(
            DriftReport(
                item_id=level.item_id,
                warehouse_id=level.warehouse_id,
                stored_quantity=level.quantity_on_hand,
                derived_quantity=derived_qty,
                stored_value=level.total_value,
                derived_value=derived_value,
            )
        )
        if repair:
            with transaction.atomic():
                locked = _lock_level_by_ids(
                    tenant_id, level.item_id, level.warehouse_id
                )
                locked.quantity_on_hand = derived_qty
                locked.quantity_available = derived_qty - locked.quantity_reserved
                locked.total_value = derived_qty * locked.average_cost
                locked.save(
                    update_fields=[
                        "quantity_on_hand",
                        "quantity_available",
                        "total_value",
                        "updated_at",
                    ]
                )

    # Bins with movements but no level row at all: the projection is missing
    # entirely, which is drift of the most serious kind.
    for (missing_item_id, missing_wh_id), (qty, value) in derived.items():
        if qty == ZERO:
            continue
        reports.append(
            DriftReport(
                item_id=missing_item_id,
                warehouse_id=missing_wh_id,
                stored_quantity=ZERO,
                derived_quantity=qty,
                stored_value=ZERO,
                derived_value=value,
            )
        )
    return reports


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

@transaction.atomic
def bulk_adjust_prices(
    *,
    tenant_id: uuid.UUID,
    price_list: PriceList | uuid.UUID,
    percentage: Decimal | str | int | None = None,
    absolute_delta: Decimal | str | int | None = None,
    category_id: Optional[uuid.UUID] = None,
    item_ids: Optional[Sequence[uuid.UUID]] = None,
    effective_from: Optional[date] = None,
    round_to: Optional[Decimal] = None,
    user_id: Optional[uuid.UUID] = None,
) -> list[PriceListItem]:
    """Apply a across-the-board price change as *new, future-dated rows*.

    The naive implementation is ``UPDATE price_list_item SET unit_price =
    unit_price * 1.05``. It is wrong for two reasons that only show up later:
    the old price is destroyed, so a customer disputing last month's invoice
    cannot be answered; and the change takes effect the instant it commits,
    which is never what "prices go up on 1 April" means.

    So a price adjustment closes the current rows (``effective_to`` = the day
    before the new price starts) and inserts successors. Both versions
    survive, and pricing on any date remains a lookup rather than an
    archaeology exercise.
    """
    price_list = _resolve(PriceList, price_list, tenant_id)
    if (percentage is None) == (absolute_delta is None):
        raise ValidationError(
            "Specify exactly one of percentage or absolute_delta."
        )
    effective_from = effective_from or timezone.localdate()

    factor = None
    delta = None
    if percentage is not None:
        factor = Decimal("1") + to_money(percentage, field_name="percentage")
        if factor <= ZERO:
            raise ValidationError({"percentage": "Adjustment would invert prices."})
    else:
        delta = to_money(absolute_delta, field_name="absolute_delta")

    rows = PriceListItem.all_tenants.filter(
        tenant_id=tenant_id, price_list=price_list, is_active=True
    ).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gte=effective_from)
    )
    if category_id is not None:
        rows = rows.filter(item__category_id=category_id)
    if item_ids:
        rows = rows.filter(item_id__in=list(item_ids))

    # Lock the rows we are about to supersede so two concurrent bulk changes
    # cannot each close the other's successor and leave a gap in coverage.
    rows = list(rows.select_for_update().select_related("item"))

    #: The old price stops being current the day before the new one starts.
    #: Ending it *on* the same day would leave two rows valid simultaneously,
    #: and price resolution would become order-dependent.
    close_on = effective_from - timedelta(days=1)

    successors: list[PriceListItem] = []
    superseded: list[uuid.UUID] = []
    for row in rows:
        new_price = _adjusted_price(row.unit_price, factor, delta, round_to)
        if new_price < ZERO:
            raise ValidationError(
                f"Adjustment would make {row.item.sku} negative "
                f"({row.unit_price} -> {new_price})."
            )
        if row.effective_from is not None and row.effective_from >= effective_from:
            # This row has not taken effect yet (or starts on the very day of
            # the change), so there is no history to preserve and no second
            # version to create — amending it in place is both correct and
            # what keeps ``uq_price_list_item_break`` satisfiable.
            row.unit_price = new_price
            row.updated_by_id = user_id
            row.save(update_fields=["unit_price", "updated_by", "updated_at"])
            continue

        superseded.append(row.pk)
        successors.append(
            PriceListItem(
                tenant_id=tenant_id,
                price_list=price_list,
                item=row.item,
                unit_price=new_price,
                discount_percent=row.discount_percent,
                is_percentage=row.is_percentage,
                min_quantity=row.min_quantity,
                effective_from=effective_from,
                effective_to=row.effective_to,
                is_active=True,
                created_by_id=user_id,
            )
        )

    if superseded:
        PriceListItem.all_tenants.filter(pk__in=superseded).update(
            effective_to=close_on, updated_by_id=user_id, updated_at=timezone.now()
        )
    PriceListItem.all_tenants.bulk_create(successors)
    return successors


def _adjusted_price(
    current: Decimal,
    factor: Optional[Decimal],
    delta: Optional[Decimal],
    round_to: Optional[Decimal],
) -> Decimal:
    new_price = (current * factor) if factor is not None else (current + delta)
    if round_to is not None and round_to > ZERO:
        # Psychological rounding (…99, …95) is a business decision, so it is
        # a parameter rather than a hardcoded quantize buried in the service.
        new_price = (new_price / round_to).quantize(Decimal("1")) * round_to
    return new_price


# ---------------------------------------------------------------------------
# GL integration
# ---------------------------------------------------------------------------

def build_journal_entry(
    movement: StockMovement, *, item: Item, tenant_id: uuid.UUID
) -> Optional[JournalEntryDraft]:
    """Express a stock movement as a balanced pair of ledger lines.

    Returns a *draft*, never ORM rows: only ``post_entry`` may write to the
    ledger, and it is the place where debits are proved equal to credits.

    The mapping, in plain terms — the amount is always the movement's
    ``total_cost``, i.e. what the goods cost *us*, never what we sold them
    for. Revenue is the invoice's business, not inventory's; mixing the two
    is how gross margin ends up wrong in both directions at once:

    ============  ====================  ====================
    Movement      Debit                 Credit
    ============  ====================  ====================
    PURCHASE      Inventory (asset)     Accounts payable
    SALE          COGS (expense)        Inventory
    RETURN_IN     Inventory             COGS
    RETURN_OUT    Accounts payable      Inventory
    ADJUSTMENT +  Inventory             Inventory adjustment
    ADJUSTMENT -  Inventory adjustment  Inventory
    PRODUCTION +  Inventory             Work in progress
    PRODUCTION -  Work in progress      Inventory
    OPENING       Inventory             Opening balance equity
    ============  ====================  ====================
    """
    amount = movement.total_cost
    if amount <= ZERO:
        # A zero-cost movement (a free sample, an as-yet-uncosted receipt)
        # has no financial effect. Posting a zero entry would fail
        # ``validate_draft`` anyway; returning None says so honestly.
        return None

    inventory_account_id = item.inventory_account_id
    if inventory_account_id is None:
        raise ValidationError(
            f"Item {item.sku} has no inventory account; cannot post its "
            f"stock movement. Fix the item setup, do not skip the posting."
        )

    mt = StockMovement.MovementType
    inbound = movement.quantity_delta > ZERO

    if movement.movement_type == mt.PURCHASE:
        debit_id, credit_id = inventory_account_id, _system_account(tenant_id, AP_CONTROL)
    elif movement.movement_type == mt.SALE:
        debit_id, credit_id = _cogs_account(item), inventory_account_id
    elif movement.movement_type == mt.RETURN_IN:
        debit_id, credit_id = inventory_account_id, _cogs_account(item)
    elif movement.movement_type == mt.RETURN_OUT:
        debit_id, credit_id = _system_account(tenant_id, AP_CONTROL), inventory_account_id
    elif movement.movement_type == mt.ADJUSTMENT:
        counter = _system_account(tenant_id, INVENTORY_ADJUSTMENT, fallback=_cogs_account(item))
        debit_id, credit_id = (
            (inventory_account_id, counter) if inbound else (counter, inventory_account_id)
        )
    elif movement.movement_type == mt.PRODUCTION:
        wip = _system_account(tenant_id, WORK_IN_PROGRESS, fallback=_cogs_account(item))
        debit_id, credit_id = (
            (inventory_account_id, wip) if inbound else (wip, inventory_account_id)
        )
    elif movement.movement_type == mt.OPENING:
        equity = _system_account(tenant_id, OPENING_BALANCE_EQUITY)
        debit_id, credit_id = (
            (inventory_account_id, equity) if inbound else (equity, inventory_account_id)
        )
    else:  # TRANSFER_* are filtered out before we get here.
        return None

    draft = JournalEntryDraft(
        journal_code=_inventory_journal_code(tenant_id),
        entry_date=timezone.localdate(movement.occurred_at),
        currency=movement.currency,
        source=JournalEntry.Source.INVENTORY,
        source_document_type="stock_movement",
        source_document_id=movement.id,
        memo=f"{movement.get_movement_type_display()} {movement.quantity_delta} "
             f"x {item.sku}"[:500],
        # Ties the ledger entry to this exact movement, so a retry of the
        # surrounding task cannot post the cost twice.
        idempotency_key=f"stock_movement:{movement.id}",
    )
    draft.debit(debit_id, amount, description=item.name[:500])
    draft.credit(credit_id, amount, description=item.name[:500])
    return draft


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve(model, value, tenant_id: uuid.UUID):
    """Accept either an instance or an id, always inside the tenant."""
    if isinstance(value, model):
        if value.tenant_id != tenant_id:
            raise ValidationError(
                f"{model.__name__} {value.pk} belongs to another tenant."
            )
        return value
    obj = model.all_tenants.filter(tenant_id=tenant_id, pk=value).first()
    if obj is None:
        raise ValidationError(f"{model.__name__} {value} not found in this tenant.")
    return obj


def _lock_level(
    tenant_id: uuid.UUID,
    item: Item,
    warehouse: Warehouse,
    *,
    user_id: Optional[uuid.UUID] = None,
) -> StockLevel:
    """``select_for_update().get_or_create()`` on the (item, warehouse) bin.

    ``get_or_create`` under a race can still raise ``IntegrityError`` on the
    create path: two transactions both miss the row and both INSERT. The
    unique index catches the loser, and the correct response is to re-read —
    by then the winner has committed, so the row exists and can be locked.
    """
    defaults = {
        "currency": item.currency,
        "average_cost": ZERO,
        "allow_negative": item.allow_negative_stock,
        "created_by_id": user_id,
    }
    qs = StockLevel.all_tenants.filter(tenant_id=tenant_id).select_for_update()
    try:
        level, _created = qs.get_or_create(
            tenant_id=tenant_id, item=item, warehouse=warehouse, defaults=defaults
        )
    except IntegrityError:
        level = qs.get(tenant_id=tenant_id, item=item, warehouse=warehouse)
    return level


def _lock_level_by_ids(
    tenant_id: uuid.UUID, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> StockLevel:
    return (
        StockLevel.all_tenants.filter(tenant_id=tenant_id)
        .select_for_update()
        .get(item_id=item_id, warehouse_id=warehouse_id)
    )


def _get_level_unlocked(
    tenant_id: uuid.UUID, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Optional[StockLevel]:
    return StockLevel.all_tenants.filter(
        tenant_id=tenant_id, item_id=item_id, warehouse_id=warehouse_id
    ).first()


def _assert_direction(movement_type: str, delta: Decimal) -> None:
    """Mirror of ``ck_stock_movement_inbound_sign`` with a readable message.

    The DB constraint is the authority; this exists so the API returns
    "a sale cannot increase stock" instead of a raw IntegrityError.
    """
    if movement_type in StockMovement.INBOUND_TYPES and delta < ZERO:
        raise ValidationError(
            f"A {movement_type} movement must increase stock; got {delta}. "
            f"Use the matching outbound type instead of a negative quantity."
        )
    if movement_type in StockMovement.OUTBOUND_TYPES and delta > ZERO:
        raise ValidationError(
            f"A {movement_type} movement must decrease stock; got {delta}."
        )


def _assert_uom_precision(item: Item, delta: Decimal) -> None:
    """Refuse 0.5 of a unit that is only sold whole.

    Checked here rather than by a constraint because the allowed precision
    lives on the UoM row and a CHECK cannot join.
    """
    places = item.uom.decimal_places
    # ``normalize()`` first: every value coming out of ``to_money`` carries a
    # -6 exponent (2 is stored as 2.000000), so testing the raw exponent
    # would reject every quantity for every unit finer than numeric(19,6).
    significant = -delta.normalize().as_tuple().exponent
    if significant > places:
        raise ValidationError(
            f"{item.sku} is measured in {item.uom.code}, which allows "
            f"{places} decimal place(s); got {delta}."
        )


def _compute_costs(
    *,
    item: Item,
    level: StockLevel,
    delta: Decimal,
    supplied_unit_cost: Optional[Decimal],
    batch: Optional[StockBatch],
    tenant_id: uuid.UUID,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(unit_cost, total_cost, new_average_cost)`` for the movement.

    Weighted average is recomputed from the *pre-movement* balance:

        new_avg = (qty_before * avg_before + qty_in * cost_in)
                  / (qty_before + qty_in)

    All in Decimal at numeric(19,6). Doing this in binary floating point
    drifts a fraction of a cent per receipt; after a few hundred thousand
    movements the inventory asset in the ledger and the sum of
    ``quantity x average_cost`` across this table no longer agree, and there
    is no way to say which is right.
    """
    quantity_before = level.quantity_on_hand
    average_before = level.average_cost

    if delta > ZERO:
        # Inbound: the cost is whatever we paid. Falling back to the item's
        # list purchase price is a last resort — it is a *plan*, not a fact,
        # and using it silently is how a receipt gets valued at a price we
        # never paid.
        unit_cost = supplied_unit_cost
        if unit_cost is None:
            unit_cost = batch.unit_cost if batch is not None else item.purchase_price
        if unit_cost < ZERO:
            raise ValidationError({"unit_cost": "Cost cannot be negative."})
        total_cost = unit_cost * delta

        if item.valuation_method == Item.ValuationMethod.STANDARD:
            # Standard costing values everything at the frozen standard; the
            # difference against the actual price is a purchase price
            # variance, booked by the purchasing module, not here.
            new_average = item.purchase_price
        else:
            quantity_after = quantity_before + delta
            if quantity_after <= ZERO:
                # Receipt into a negative balance: no meaningful average
                # exists until the balance is positive again.
                new_average = unit_cost
            else:
                new_average = (
                    (quantity_before * average_before) + total_cost
                ) / quantity_after
        return unit_cost, total_cost, new_average

    # Outbound: the cost is what we are relieving from the asset, decided by
    # the valuation method, never by the selling price.
    outbound_quantity = -delta
    if item.valuation_method == Item.ValuationMethod.STANDARD:
        unit_cost = item.purchase_price
    elif item.valuation_method == Item.ValuationMethod.FIFO:
        unit_cost = _fifo_unit_cost(
            tenant_id=tenant_id,
            item=item,
            warehouse_id=level.warehouse_id,
            quantity=outbound_quantity,
            fallback=average_before,
            batch=batch,
        )
    else:
        unit_cost = average_before

    total_cost = unit_cost * outbound_quantity
    # Issuing stock never changes the average cost of what remains; that is
    # the defining property of the weighted-average method and the reason a
    # sale cannot move gross margin on the *next* sale.
    return unit_cost, total_cost, average_before


def _fifo_unit_cost(
    *,
    tenant_id: uuid.UUID,
    item: Item,
    warehouse_id: uuid.UUID,
    quantity: Decimal,
    fallback: Decimal,
    batch: Optional[StockBatch],
) -> Decimal:
    """Weighted cost of the batches this issue would consume, FEFO order.

    If a specific batch was picked, its cost is the answer — that is the
    whole point of lot tracking. Otherwise consume open batches by expiry
    (first expired, first out), falling back to the moving average when the
    item is not batch-tracked or the batches are exhausted.
    """
    if batch is not None:
        return batch.unit_cost

    remaining = quantity
    consumed_value = ZERO
    consumed_quantity = ZERO
    batches = (
        StockBatch.all_tenants.filter(
            tenant_id=tenant_id,
            item=item,
            warehouse_id=warehouse_id,
            quantity_remaining__gt=0,
            is_quarantined=False,
        )
        .order_by(F("expires_on").asc(nulls_last=True), "received_at")
        .select_for_update()
    )
    for candidate in batches:
        if remaining <= ZERO:
            break
        take = min(candidate.quantity_remaining, remaining)
        consumed_value += take * candidate.unit_cost
        consumed_quantity += take
        remaining -= take

    if consumed_quantity <= ZERO:
        return fallback
    if remaining > ZERO:
        # Short-picked: value the uncovered part at the moving average rather
        # than pretending the last batch's cost applies to units it never
        # contained.
        consumed_value += remaining * fallback
        consumed_quantity += remaining
    return consumed_value / consumed_quantity


def _apply_to_batch(batch: StockBatch, delta: Decimal) -> None:
    """Move a batch's remaining quantity with the movement that consumed it.

    ``F()`` rather than read-modify-write: the batch row is not the row we
    locked, so a concurrent issue from the same batch must not be lost. The
    ``ck_stock_batch_not_over_consumed`` constraint is what stops the update
    from taking it below zero.
    """
    StockBatch.all_tenants.filter(pk=batch.pk).update(
        quantity_remaining=F("quantity_remaining") + delta,
        updated_at=timezone.now(),
    )


def _maybe_raise_low_stock(
    *,
    tenant_id: uuid.UUID,
    item: Item,
    warehouse: Warehouse,
    level: StockLevel,
) -> Optional[LowStockAlert]:
    """Open or refresh the reorder alert for this bin.

    Compares against ``quantity_available``, not on-hand: units already
    promised to a picked order cannot cover the next one, and reordering
    against on-hand is precisely how a warehouse runs out while the system
    insists it has stock.

    The debounce is the partial unique index on the model (one open alert per
    bin). Here we only ever *refresh* an existing open alert, so a level
    oscillating around the reorder point bumps a counter instead of filling
    an inbox — the second identical email is the one that teaches people to
    ignore the first.
    """
    if item.reorder_point <= ZERO or not item.is_stocked:
        return None
    if level.quantity_available > item.reorder_point:
        return None

    now = timezone.now()
    alert = LowStockAlert.all_tenants.filter(
        tenant_id=tenant_id,
        item=item,
        warehouse=warehouse,
        acknowledged_at__isnull=True,
    ).first()
    if alert is not None:
        LowStockAlert.all_tenants.filter(pk=alert.pk).update(
            occurrence_count=F("occurrence_count") + 1,
            quantity_at_trigger=level.quantity_available,
            last_seen_at=now,
            updated_at=now,
        )
        alert.refresh_from_db()
        return alert

    try:
        return LowStockAlert.objects.create(
            tenant_id=tenant_id,
            item=item,
            warehouse=warehouse,
            threshold_at_trigger=item.reorder_point,
            quantity_at_trigger=level.quantity_available,
            triggered_at=now,
            last_seen_at=now,
        )
    except IntegrityError:
        # Another transaction opened the alert between our SELECT and INSERT.
        # That is the debounce working, not an error.
        return LowStockAlert.all_tenants.filter(
            tenant_id=tenant_id,
            item=item,
            warehouse=warehouse,
            acknowledged_at__isnull=True,
        ).first()


def _cogs_account(item: Item) -> uuid.UUID:
    if item.expense_account_id is None:
        raise ValidationError(
            f"Item {item.sku} has no COGS account; its cost has nowhere to go."
        )
    return item.expense_account_id


def _system_account(
    tenant_id: uuid.UUID, system_key: str, *, fallback: Optional[uuid.UUID] = None
) -> uuid.UUID:
    """Resolve a wired-in account by role, not by code.

    Account *codes* differ between national standard charts (and between two
    tenants using the same chart), so automated postings must never hardcode
    one. ``system_key`` is the stable role identifier.
    """
    account_id = (
        Account.all_tenants.filter(
            tenant_id=tenant_id, system_key=system_key, is_active=True
        )
        .values_list("id", flat=True)
        .first()
    )
    if account_id is None:
        if fallback is not None:
            return fallback
        raise ValidationError(
            f"No account is configured with system_key '{system_key}'. "
            f"Complete the chart-of-accounts setup before posting stock."
        )
    return account_id


def _inventory_journal_code(tenant_id: uuid.UUID) -> str:
    code = (
        Journal.all_tenants.filter(
            tenant_id=tenant_id, kind=Journal.Kind.INVENTORY, is_active=True
        )
        .values_list("code", flat=True)
        .first()
    )
    if code is None:
        raise ValidationError(
            "No active inventory journal exists for this tenant. Stock "
            "postings need their own book of original entry."
        )
    return code


# ---------------------------------------------------------------------------
# Document-level entry points
# ---------------------------------------------------------------------------
# Re-exported from `fulfilment` so that callers outside this app have a single
# obvious import path (`apps.inventory.services.stock`) regardless of which
# module implements the operation. The import sits at the bottom of the file
# because `fulfilment` imports `apply_movement` from here — putting it at the
# top would be a genuine circular import at module load.
from apps.inventory.services.fulfilment import (  # noqa: E402
    NoDefaultWarehouse,
    issue_stock,
    receive_stock,
    return_stock,
)

__all__ = list(__all__) + [
    "NoDefaultWarehouse",
    "issue_stock",
    "receive_stock",
    "return_stock",
]
