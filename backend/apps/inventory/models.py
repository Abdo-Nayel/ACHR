"""
Inventory: what we sell, where it physically is, and what it cost us.

The module is built around one structural decision, and everything else
follows from it:

    :class:`StockMovement` is the source of truth; :class:`StockLevel` is a
    materialised projection of it.

Stock is a ledger, exactly like the general ledger, and it earns the same
treatment: an append-only log of signed deltas whose running sum *is* the
balance. The alternative — storing only a mutable ``quantity_on_hand`` and
UPDATE-ing it — is what produces the classic "the system says 3, the shelf
says 1, and nobody can say when it diverged" support ticket. With the log we
can always answer *when* and *because of which document*.

``StockLevel`` exists anyway because "show me every item below its reorder
point across 40 warehouses" cannot be a ``SUM()`` over ten million movement
rows on every dashboard load. It is a cache, and like every cache it can
drift (a crashed worker between the movement INSERT and the level UPDATE, a
restore from a partial backup, a hand-written SQL fix). ``recompute_stock_levels``
in ``apps.inventory.services.stock`` re-derives levels from the movement log
and reports the difference; it runs nightly. Drift is therefore a detected,
alerted condition rather than a silent one.

Valuation is Decimal end to end. A weighted-average cost recomputed in
binary floating point drifts by a fraction of a cent per movement, and after
a year of high-volume trading the inventory asset on the balance sheet no
longer reconciles to the sum of its parts.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.fields import MoneyField, QuantityField, RateField, ZERO
from apps.core.models import (
    Currency,
    ImmutableFinancialModel,
    StatusTransitionMixin,
    TenantScopedModel,
)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

class UnitOfMeasure(TenantScopedModel):
    """How an item is counted: each, kg, litre, hour, box-of-12.

    Tenant-scoped rather than a global lookup table because "carton" means 12
    to one distributor and 24 to another, and because a shared table would
    let one tenant's edit change another tenant's stock figures.

    ``decimal_places`` is a *business* precision, not a storage precision:
    the column is always numeric(19,6), but selling 0.5 of a serialised
    laptop is nonsense, so the service layer rejects quantities with more
    precision than the unit allows. Storing it here keeps that rule with the
    unit instead of scattering ``if item.is_serialised`` checks through the
    order code.
    """

    class Kind(models.TextChoices):
        UNIT = "unit", "Discrete unit"
        WEIGHT = "weight", "Weight"
        VOLUME = "volume", "Volume"
        LENGTH = "length", "Length"
        AREA = "area", "Area"
        TIME = "time", "Time"

    code = models.CharField(max_length=20)
    name = models.CharField(max_length=80)
    symbol = models.CharField(max_length=12, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.UNIT)

    #: Conversion to the "base" unit of the same kind (kg -> g is 1000).
    #: NULL base means this *is* a base unit. Kept as a Decimal factor rather
    #: than a pair of integers because 1 lb = 0.45359237 kg is not rational
    #: in any convenient denominator.
    base_uom = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="derived"
    )
    conversion_factor = RateField(default=1)

    decimal_places = models.PositiveSmallIntegerField(default=2)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_unit_of_measure"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_uom_code"),
            models.CheckConstraint(
                condition=models.Q(conversion_factor__gt=0),
                name="ck_uom_factor_positive",
            ),
            models.CheckConstraint(
                condition=~models.Q(base_uom=models.F("id")),
                name="ck_uom_no_self_base",
            ),
            models.CheckConstraint(
                condition=models.Q(decimal_places__lte=6),
                name="ck_uom_precision_within_column",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_uom_active"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.symbol or self.code


class ItemCategory(TenantScopedModel):
    """Merchandise hierarchy used for reporting and for account defaults.

    A category may carry default income / expense / inventory accounts. New
    items inherit them at creation time and then keep their own copy: if the
    category's default is later changed, historical items must not silently
    start posting somewhere else, because that would make this year's COGS
    incomparable with last year's.
    """

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    description = models.CharField(max_length=255, blank=True)

    default_income_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    default_expense_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    default_inventory_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_item_category"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_item_category_code"
            ),
            models.CheckConstraint(
                condition=~models.Q(parent=models.F("id")),
                name="ck_item_category_no_self_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "parent"], name="ix_item_category_parent"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------

class Item(TenantScopedModel):
    """Anything that can appear on a sales or purchase line.

    Three kinds, and the difference is entirely about the general ledger:

    * ``INVENTORY``  — a physical good we own. Buying it moves value from
      cash/AP into an *asset* (the inventory account); selling it moves that
      value out of the asset and into COGS. Quantities are tracked.
    * ``SERVICE``    — labour, consulting, a subscription. There is nothing
      to own, so there is no inventory asset and nothing to count. Selling it
      recognises income with no COGS movement at all.
    * ``NON_INVENTORY`` — a physical good we deliberately do not count
      (screws, printer paper, packaging). Expensed on purchase; the clerical
      cost of counting them exceeds the value of knowing.

    Invariant enforced by ``ck_item_service_not_stocked``:
        **A SERVICE item must have ``track_inventory = False`` and
        ``inventory_account = NULL``.**
    This is not tidiness. A service row with an inventory account attached
    will, the first time it is sold, credit an asset account that was never
    debited, creating a growing negative balance in an asset on the balance
    sheet — a defect that is invisible until year-end and expensive to
    unwind, because every affected invoice must be reversed and re-posted.
    Expressing it as a CHECK means the bad row cannot exist, no matter which
    import script, admin form or future API endpoint tries to create it.

    The mirror invariant (``ck_item_tracked_needs_inv_account``) is that an
    item we *do* track must name the asset account its value lives in;
    otherwise the stock service has nowhere to post and fails at sale time —
    i.e. in front of a customer — rather than at setup time.

    ``sales_price`` / ``purchase_price`` are list defaults only. The price
    actually charged is resolved per document from :class:`PriceListItem`
    and then *copied onto the document line*, because reprinting a two-year-old
    invoice must not show today's price.
    """

    class Type(models.TextChoices):
        INVENTORY = "inventory", "Inventory item"
        SERVICE = "service", "Service"
        NON_INVENTORY = "non_inventory", "Non-inventory item"

    class ValuationMethod(models.TextChoices):
        FIFO = "fifo", "First in, first out"
        WEIGHTED_AVERAGE = "weighted_average", "Weighted average cost"
        STANDARD = "standard", "Standard cost"

    sku = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    type = models.CharField(
        max_length=16, choices=Type.choices, default=Type.INVENTORY, db_index=True
    )

    uom = models.ForeignKey(
        UnitOfMeasure, on_delete=models.PROTECT, related_name="items"
    )
    category = models.ForeignKey(
        ItemCategory, null=True, blank=True,
        on_delete=models.PROTECT, related_name="items",
    )

    currency = models.CharField(max_length=3, choices=Currency.choices)
    sales_price = MoneyField()
    purchase_price = MoneyField()

    #: PROTECT throughout: archiving an account that items still post to would
    #: leave the next sale with nowhere to book its revenue or its cost.
    income_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True,
        on_delete=models.PROTECT, related_name="income_items",
    )
    #: Cost of goods sold. Debited when stock leaves on a sale.
    expense_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True,
        on_delete=models.PROTECT, related_name="cogs_items",
    )
    #: The asset account holding this item's value while it sits on the shelf.
    inventory_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True,
        on_delete=models.PROTECT, related_name="inventory_items",
    )
    tax_rate = models.ForeignKey(
        "accounting.TaxRate", null=True, blank=True,
        on_delete=models.PROTECT, related_name="items",
    )

    is_active = models.BooleanField(default=True)
    track_inventory = models.BooleanField(default=True)

    #: Reordering policy. ``reorder_point`` is compared against
    #: ``StockLevel.quantity_available`` (not on-hand): stock that is already
    #: promised to a picked order is not available to cover the next one, and
    #: reordering against on-hand is how a warehouse ends up short.
    reorder_point = QuantityField()
    reorder_quantity = QuantityField()

    valuation_method = models.CharField(
        max_length=20,
        choices=ValuationMethod.choices,
        default=ValuationMethod.WEIGHTED_AVERAGE,
    )
    #: Allowing negative stock is a deliberate per-item policy for businesses
    #: that ship before the goods-received note is keyed. It is off by
    #: default because for everyone else a negative balance is a data error
    #: that should stop at the door.
    allow_negative_stock = models.BooleanField(default=False)

    barcode = models.CharField(max_length=64, blank=True)
    weight = QuantityField(null=True, blank=True)
    is_batch_tracked = models.BooleanField(default=False)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_item"
        ordering = ["sku"]
        constraints = [
            # Per tenant, never global: SKUs collide constantly between
            # unrelated companies, and a global unique index both breaks
            # legitimate onboarding and leaks the existence of other tenants'
            # catalogues through duplicate-key errors.
            models.UniqueConstraint(fields=["tenant", "sku"], name="uq_item_sku"),
            models.UniqueConstraint(
                fields=["tenant", "barcode"],
                condition=~models.Q(barcode=""),
                name="uq_item_barcode",
            ),
            models.CheckConstraint(
                condition=~models.Q(type="service")
                | (
                    models.Q(track_inventory=False)
                    & models.Q(inventory_account__isnull=True)
                ),
                name="ck_item_service_not_stocked",
            ),
            models.CheckConstraint(
                condition=models.Q(track_inventory=False)
                | models.Q(inventory_account__isnull=False),
                name="ck_item_tracked_needs_inv_account",
            ),
            models.CheckConstraint(
                condition=models.Q(sales_price__gte=0)
                & models.Q(purchase_price__gte=0),
                name="ck_item_prices_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(reorder_point__gte=0)
                & models.Q(reorder_quantity__gte=0),
                name="ck_item_reorder_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "type", "is_active"], name="ix_item_type"),
            models.Index(fields=["tenant", "category"], name="ix_item_category"),
            models.Index(fields=["tenant", "name"], name="ix_item_name"),
            models.Index(
                fields=["tenant", "track_inventory", "is_active"],
                name="ix_item_tracked",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.sku} — {self.name}"

    @property
    def is_stocked(self) -> bool:
        return self.type == self.Type.INVENTORY and self.track_inventory

    def clean(self) -> None:
        # Duplicates the CHECK constraints so the admin/API returns a field
        # error instead of an IntegrityError 500. The DB remains the authority.
        super().clean()
        if self.type == self.Type.SERVICE:
            if self.track_inventory:
                raise ValidationError(
                    {"track_inventory": "Service items cannot be stock-tracked."}
                )
            if self.inventory_account_id is not None:
                raise ValidationError(
                    {"inventory_account": "Service items have no inventory asset."}
                )
        if self.track_inventory and self.inventory_account_id is None:
            raise ValidationError(
                {"inventory_account": "A tracked item needs an inventory account."}
            )


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

class Warehouse(TenantScopedModel):
    """A physical or logical place stock can sit.

    "Logical" matters: consignment stock at a customer site, goods in
    transit between branches and quarantined returns are all modelled as
    warehouses so that they stay on our balance sheet and stay countable,
    instead of vanishing from the system between two events.

    Exactly one warehouse per tenant may be the default — enforced by a
    *partial* unique index (``condition=Q(is_default=True)``) rather than by
    application code. Application-level "unset the others first" logic loses
    the race between two concurrent admins and leaves two defaults, after
    which order entry picks a location non-deterministically.
    """

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    address = models.TextField(blank=True)
    #: Free-text contact rather than a FK: many warehouses are third-party
    #: 3PL sites with no user account in this system.
    contact_name = models.CharField(max_length=120, blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)

    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_warehouse"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_warehouse_code"
            ),
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_default=True),
                name="uq_warehouse_one_default",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "is_active"], name="ix_warehouse_active"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"


# ---------------------------------------------------------------------------
# Balances (projection) and movements (source of truth)
# ---------------------------------------------------------------------------

class StockLevel(TenantScopedModel):
    """The *current* balance of one item in one warehouse.

    A materialised projection of :class:`StockMovement`, maintained by
    ``apps.inventory.services.stock.apply_movement`` inside the same
    transaction — and, critically, under the same row lock — as the movement
    that changes it. See ``recompute_stock_levels`` for the nightly drift check.

    Why ``quantity_available`` is a stored column and not a
    ``models.GeneratedField``
    -------------------------------------------------------------------------
    It is arithmetically just ``quantity_on_hand - quantity_reserved``, so a
    generated column looks like the obvious choice. Two reasons it is not:

    1. **It must be indexable, cheaply, for the low-stock query.** The whole
       point of this table is to answer "every item where available <
       reorder_point" over a large catalogue without a sequential scan.
       A plain stored column takes an ordinary btree index that PostgreSQL
       can use directly; keeping the value materialised also lets the same
       index serve the "available <= 0" out-of-stock filter.
    2. **It must be computed inside the locked transaction.** The service
       already holds ``SELECT ... FOR UPDATE`` on this row while it decides
       whether the sale is allowed. It computes the new availability from
       values it has just validated, and writes on-hand, reserved and
       available together as one consistent tuple. A generated column would
       be recomputed by the database on write, which is fine, but it would
       also let a future ``UPDATE stock_level SET quantity_on_hand = ...``
       outside the service produce an availability the service never
       approved. Making the column explicit keeps one function responsible
       for the whole invariant.

    ``allow_negative`` is denormalised from ``Item.allow_negative_stock``
    because a CHECK constraint cannot join to another table. The service
    keeps the two in sync; the copy exists purely so the "never go negative"
    rule can be enforced by PostgreSQL rather than by hope.
    """

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="stock_levels")
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="stock_levels"
    )

    quantity_on_hand = QuantityField()
    #: Promised to a confirmed order but not yet picked. Reserved stock is
    #: physically present and therefore counted in the asset value, but it
    #: must not be sellable — that is exactly the double-sell bug.
    quantity_reserved = QuantityField()
    quantity_available = QuantityField(db_index=True)

    #: Weighted-average unit cost. For FIFO items this mirrors the average of
    #: the open batches and is used only for reporting; the authoritative
    #: FIFO cost comes from :class:`StockBatch`.
    currency = models.CharField(max_length=3, choices=Currency.choices)
    average_cost = MoneyField()
    #: on_hand * average_cost, materialised so the inventory valuation report
    #: is a SUM over this table rather than a per-row multiplication that
    #: rounds differently from the value actually posted to the ledger.
    total_value = MoneyField()

    last_movement_at = models.DateTimeField(null=True, blank=True)
    last_counted_at = models.DateTimeField(null=True, blank=True)
    allow_negative = models.BooleanField(default=False)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_stock_level"
        ordering = ["item", "warehouse"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "item", "warehouse"], name="uq_stock_level_bin"
            ),
            # A negative reservation is always a bug: it would mean we have
            # un-reserved more than was ever reserved, and it silently
            # inflates availability.
            models.CheckConstraint(
                condition=models.Q(quantity_reserved__gte=0),
                name="ck_stock_level_reserved_non_neg",
            ),
            # On-hand may only go negative for items whose policy allows it.
            models.CheckConstraint(
                condition=models.Q(allow_negative=True)
                | models.Q(quantity_on_hand__gte=0),
                name="ck_stock_level_on_hand_non_neg",
            ),
            # The projection must actually be the projection.
            models.CheckConstraint(
                condition=models.Q(
                    quantity_available=models.F("quantity_on_hand")
                    - models.F("quantity_reserved")
                ),
                name="ck_stock_level_available_derived",
            ),
            models.CheckConstraint(
                condition=models.Q(average_cost__gte=0),
                name="ck_stock_level_cost_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "item"], name="ix_stock_level_item"),
            models.Index(
                fields=["tenant", "warehouse"], name="ix_stock_level_warehouse"
            ),
            # The low-stock sweep: available first so the index is selective
            # before the reorder-point comparison is applied.
            models.Index(
                fields=["tenant", "quantity_available"], name="ix_stock_level_low"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.item_id}@{self.warehouse_id}={self.quantity_on_hand}"


class StockMovement(ImmutableFinancialModel):
    """One append-only entry in the stock ledger. The source of truth.

    Every change of quantity — a purchase receipt, a sale, a stock count
    adjustment, a transfer leg, a production output — is one row here, and
    nothing else is ever allowed to change ``StockLevel``. Because the rows
    are immutable, ``SUM(quantity_delta)`` for an (item, warehouse) pair
    re-derives the on-hand balance at any point in history, which is what
    makes the nightly drift check possible and what lets an auditor trace a
    valuation figure back to the documents that produced it.

    ``running_quantity_after`` / ``running_value_after`` are the balance *as
    of this movement*, captured under the same row lock that produced it.
    They are redundant with the running sum, deliberately: they make "what
    did the system believe at 14:32 on the 3rd?" a single-row read, and any
    disagreement between the stored running total and the recomputed sum
    localises corruption to an exact row instead of an exact day.

    Why ``quantity_delta`` is signed, when ``JournalLine`` forbids signed
    amounts
    -------------------------------------------------------------------------
    The ledger splits debit and credit into two non-negative columns because
    a journal entry has *two sides that must balance*: the split lets
    ``SUM(debit) = SUM(credit)`` be a plain SQL constraint, and turns a sign
    error into a constraint violation rather than a silently reversed entry.

    A stock movement has no counterparty column. There is exactly one
    quantity, against exactly one (item, warehouse) bin, and no balancing
    identity to protect. Splitting it into ``quantity_in`` / ``quantity_out``
    would buy nothing and cost real safety: every rollup becomes
    ``SUM(in) - SUM(out)``, and an extra CHECK is needed to ensure exactly
    one of the pair is non-zero — reintroducing precisely the class of bug
    the ledger's split was meant to remove. The signed column keeps the
    re-derivation a single ``SUM`` and makes the direction of the movement
    self-evident. ``total_cost``, by contrast, is a monetary amount and is
    always non-negative; direction is carried by ``movement_type``.
    """

    class MovementType(models.TextChoices):
        PURCHASE = "purchase", "Purchase receipt"
        SALE = "sale", "Sale / delivery"
        RETURN_IN = "return_in", "Customer return"
        RETURN_OUT = "return_out", "Return to vendor"
        ADJUSTMENT = "adjustment", "Stock adjustment"
        TRANSFER_IN = "transfer_in", "Transfer in"
        TRANSFER_OUT = "transfer_out", "Transfer out"
        PRODUCTION = "production", "Production"
        OPENING = "opening", "Opening balance"

    #: Movement types whose delta must be > 0 / < 0. ADJUSTMENT and
    #: PRODUCTION are genuinely two-directional (a count correction can go
    #: either way; production consumes components and yields output).
    INBOUND_TYPES: set[str] = {
        MovementType.PURCHASE,
        MovementType.RETURN_IN,
        MovementType.TRANSFER_IN,
    }
    OUTBOUND_TYPES: set[str] = {
        MovementType.SALE,
        MovementType.RETURN_OUT,
        MovementType.TRANSFER_OUT,
    }

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="movements")
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="movements"
    )
    movement_type = models.CharField(
        max_length=14, choices=MovementType.choices, db_index=True
    )

    quantity_delta = QuantityField()
    currency = models.CharField(max_length=3, choices=Currency.choices)
    #: Cost per unit *of this movement*. For an outbound movement it is the
    #: cost being relieved from the asset (average or FIFO layer cost), not a
    #: selling price — the sale price lives on the invoice line. Conflating
    #: the two is the most common way inventory valuation goes wrong.
    unit_cost = MoneyField()
    total_cost = MoneyField()

    running_quantity_after = QuantityField()
    running_value_after = MoneyField()

    #: Generic pointer to the document that caused the movement
    #: ("invoice", "bill", "stock_adjustment", "transfer"). Generic rather
    #: than a dozen nullable FKs, which would make this hot table wide and
    #: every new document type a schema migration.
    reference_type = models.CharField(max_length=50, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)

    #: NULL for movements with no GL effect (an internal transfer between two
    #: of our own warehouses does not change the value we own).
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True,
        on_delete=models.PROTECT, related_name="stock_movements",
    )
    batch = models.ForeignKey(
        "inventory.StockBatch", null=True, blank=True,
        on_delete=models.PROTECT, related_name="movements",
    )

    #: When the movement happened in the real world, which is not when the
    #: row was written (a goods-received note keyed the next morning must be
    #: valued on yesterday's date). Reports use this; ``created_at`` is only
    #: for forensics.
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    notes = models.CharField(max_length=255, blank=True)

    #: Makes a retried Celery task or a re-delivered webhook harmless: the
    #: second attempt hits the unique index instead of double-shipping stock.
    idempotency_key = models.CharField(max_length=128, blank=True)

    class Meta(ImmutableFinancialModel.Meta):
        db_table = "inventory_stock_movement"
        ordering = ["-occurred_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uq_stock_movement_idempotency",
            ),
            # A zero-quantity movement is noise that breaks the "every row
            # changed something" assumption of the drift report.
            models.CheckConstraint(
                condition=~models.Q(quantity_delta=0),
                name="ck_stock_movement_nonzero",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_cost__gte=0) & models.Q(total_cost__gte=0),
                name="ck_stock_movement_cost_non_neg",
            ),
            models.CheckConstraint(
                condition=~models.Q(
                    movement_type__in=["purchase", "return_in", "transfer_in"]
                )
                | models.Q(quantity_delta__gt=0),
                name="ck_stock_movement_inbound_sign",
            ),
            models.CheckConstraint(
                condition=~models.Q(
                    movement_type__in=["sale", "return_out", "transfer_out"]
                )
                | models.Q(quantity_delta__lt=0),
                name="ck_stock_movement_outbound_sign",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            # The re-derivation / item-card query: one bin, in time order.
            models.Index(
                fields=["tenant", "item", "warehouse", "occurred_at"],
                name="ix_stock_movement_bin",
            ),
            models.Index(
                fields=["tenant", "movement_type", "occurred_at"],
                name="ix_stock_movement_type",
            ),
            models.Index(
                fields=["reference_type", "reference_id"],
                name="ix_stock_movement_source",
            ),
            models.Index(fields=["tenant", "batch"], name="ix_stock_movement_batch"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.movement_type} {self.quantity_delta} of {self.item_id}"

    def build_journal_entry(self):
        """The financial effect of this movement, as an inert draft.

        Required by the GL convention: modules describe their effect and hand
        it to ``post_entry``; they never write ``JournalLine`` rows. The
        import is local because the service imports these models, and the
        mapping itself lives there so that the costing rules and the posting
        rules stay in one file.
        """
        from apps.inventory.services.stock import build_journal_entry

        return build_journal_entry(self, item=self.item, tenant_id=self.tenant_id)


class StockBatch(TenantScopedModel):
    """A lot / serial group: stock received together, with one shared cost.

    Required for pharmaceuticals, food and anything with an expiry, and it is
    also what makes true FIFO possible — an average cost cannot tell you
    which physical units are about to expire.

    Picking strategy is FEFO (first *expired*, first out) rather than plain
    FIFO for perishables: the oldest batch is not necessarily the one closest
    to expiry when shelf lives differ between receipts. The
    ``(tenant, item, expires_on)`` index is what makes that pick a cheap
    ordered range scan instead of a sort of every open batch.

    ``quantity_remaining`` is decremented as the batch is consumed and is
    never allowed below zero; a batch at zero stays in the table forever
    because recall traceability ("which customers received lot 4471?")
    depends on it.
    """

    batch_number = models.CharField(max_length=64)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="batches")
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="batches"
    )

    quantity_received = QuantityField()
    quantity_remaining = QuantityField()
    currency = models.CharField(max_length=3, choices=Currency.choices)
    unit_cost = MoneyField()

    manufactured_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    supplier_reference = models.CharField(max_length=100, blank=True)
    is_quarantined = models.BooleanField(default=False)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_stock_batch"
        ordering = ["expires_on", "received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "item", "warehouse", "batch_number"],
                name="uq_stock_batch_number",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_remaining__gte=0)
                & models.Q(quantity_received__gte=0),
                name="ck_stock_batch_qty_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_remaining__lte=models.F("quantity_received")),
                name="ck_stock_batch_not_over_consumed",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_cost__gte=0),
                name="ck_stock_batch_cost_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(manufactured_on__isnull=True)
                | models.Q(expires_on__isnull=True)
                | models.Q(expires_on__gte=models.F("manufactured_on")),
                name="ck_stock_batch_date_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            # FEFO picking.
            models.Index(
                fields=["tenant", "item", "expires_on"], name="ix_stock_batch_fefo"
            ),
            models.Index(
                fields=["tenant", "warehouse", "item"], name="ix_stock_batch_bin"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.batch_number} ({self.quantity_remaining} left)"


# ---------------------------------------------------------------------------
# Adjustments (approval workflow)
# ---------------------------------------------------------------------------

class StockAdjustment(StatusTransitionMixin, TenantScopedModel):
    """A stock count correction or write-off, awaiting approval.

    Adjustments are the one place where a human can change inventory value
    without a customer or supplier document behind it, so they are the
    obvious vector for both error and fraud. Hence the workflow:

        ``DRAFT -> APPROVED -> POSTED``

    * **DRAFT** is a working document; lines may be edited freely and nothing
      has touched stock or the ledger.
    * **APPROVED** records that a second person accepted the variance. The
      lines are frozen from here on — approving a document and then editing
      it is the whole attack.
    * **POSTED** means the movements exist and the journal entry is in the
      ledger. Terminal: a mistake is corrected by a *new*, opposite
      adjustment, never by editing this one, so that the variance history a
      stock controller is measured on cannot be rewritten.

    ``CANCELLED`` is reachable only from DRAFT/APPROVED, i.e. before anything
    financial has happened.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        POSTED = "posted", "Posted"
        CANCELLED = "cancelled", "Cancelled"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.APPROVED, Status.CANCELLED},
        Status.APPROVED: {Status.POSTED, Status.CANCELLED, Status.DRAFT},
        Status.POSTED: set(),
        Status.CANCELLED: set(),
    }

    class Reason(models.TextChoices):
        COUNT = "count", "Physical count variance"
        DAMAGE = "damage", "Damage"
        THEFT = "theft", "Theft / shrinkage"
        EXPIRY = "expiry", "Expiry"
        REVALUATION = "revaluation", "Revaluation"
        OPENING = "opening", "Opening balance"
        OTHER = "other", "Other"

    number = models.CharField(max_length=32, blank=True)
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="adjustments"
    )
    adjustment_date = models.DateField(default=timezone.localdate, db_index=True)
    reason = models.CharField(max_length=12, choices=Reason.choices, default=Reason.COUNT)
    memo = models.CharField(max_length=500, blank=True)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    currency = models.CharField(max_length=3, choices=Currency.choices)
    #: Signed: a write-off is negative, a found-stock correction positive.
    total_value_delta = MoneyField()

    approved_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True,
        on_delete=models.PROTECT, related_name="stock_adjustments",
    )

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_stock_adjustment"
        ordering = ["-adjustment_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                condition=~models.Q(number=""),
                name="uq_stock_adjustment_number",
            ),
            # An approval must record who approved it and when, or the
            # four-eyes control is decorative.
            models.CheckConstraint(
                condition=models.Q(status__in=["draft", "cancelled"])
                | (
                    models.Q(approved_by__isnull=False)
                    & models.Q(approved_at__isnull=False)
                ),
                name="ck_stock_adjustment_approver",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="posted")
                | models.Q(posted_at__isnull=False),
                name="ck_stock_adjustment_posted_at",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "status", "adjustment_date"],
                name="ix_stock_adj_status",
            ),
            models.Index(
                fields=["tenant", "warehouse"], name="ix_stock_adj_warehouse"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.number or f"ADJ {self.id}"


class StockAdjustmentLine(TenantScopedModel):
    """One item's variance within an adjustment.

    CASCADE on the parent is the one place the convention allows it: these
    lines have no meaning without their header and the header can only be
    deleted while it is still an unposted DRAFT. Once POSTED the header is
    terminal and nothing is deleted at all.

    Both the counted and the expected quantity are stored, not just the
    delta. Re-deriving "what did we think we had" from today's stock level is
    impossible after the next movement, and that number is exactly what an
    auditor asks for.
    """

    adjustment = models.ForeignKey(
        StockAdjustment, on_delete=models.CASCADE, related_name="lines"
    )
    item = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name="adjustment_lines"
    )
    batch = models.ForeignKey(
        StockBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    line_number = models.PositiveSmallIntegerField()

    quantity_expected = QuantityField()
    quantity_counted = QuantityField()
    #: counted - expected, materialised because the posting service and every
    #: variance report need it and recomputing it in three places is how the
    #: three places end up disagreeing about rounding.
    quantity_delta = QuantityField()

    unit_cost = MoneyField()
    value_delta = MoneyField()
    notes = models.CharField(max_length=255, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_stock_adjustment_line"
        ordering = ["adjustment", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["adjustment", "line_number"], name="uq_stock_adj_line_number"
            ),
            models.UniqueConstraint(
                fields=["adjustment", "item", "batch"], name="uq_stock_adj_line_item"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    quantity_delta=models.F("quantity_counted")
                    - models.F("quantity_expected")
                ),
                name="ck_stock_adj_line_delta",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_cost__gte=0),
                name="ck_stock_adj_line_cost_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "item"], name="ix_stock_adj_line_item"),
        ]

    def clean(self) -> None:
        # The line has no currency column of its own: the header pins it.
        # This assertion exists because a CHECK cannot reach across the FK.
        super().clean()
        if self.adjustment_id and self.adjustment.currency and self.unit_cost < ZERO:
            raise ValidationError({"unit_cost": "Unit cost cannot be negative."})


# ---------------------------------------------------------------------------
# Price lists
# ---------------------------------------------------------------------------

class PriceList(TenantScopedModel):
    """A named set of prices: retail, wholesale, "Q3 promo", per-customer.

    Time-bounded by ``effective_from`` / ``effective_to`` so that a price
    change is scheduled rather than applied by someone at midnight, and so
    that a historical document can be re-priced against the list that was in
    force on its date when a dispute arises.

    ``priority`` breaks ties when several lists cover the same item on the
    same date (a promo should beat the standard wholesale list). Resolution
    is: highest priority among lists effective on the document date.
    """

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    is_purchase_list = models.BooleanField(
        default=False, help_text="Supplier price list rather than a sales one."
    )
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    priority = models.PositiveSmallIntegerField(default=0)

    effective_from = models.DateField(default=timezone.localdate)
    effective_to = models.DateField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_price_list"
        ordering = ["-priority", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_price_list_code"
            ),
            models.UniqueConstraint(
                fields=["tenant", "currency", "is_purchase_list"],
                condition=models.Q(is_default=True),
                name="uq_price_list_one_default",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gte=models.F("effective_from")),
                name="ck_price_list_date_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "is_active", "effective_from"],
                name="ix_price_list_effective",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} — {self.name}"


class PriceListItem(TenantScopedModel):
    """One item's price on one list, optionally for a minimum quantity.

    ``min_quantity`` implements volume breaks: the same item can appear
    several times on one list (1+ at 100.00, 10+ at 92.50). Resolution picks
    the highest ``min_quantity`` not exceeding the ordered quantity, so the
    rows must be unique on ``(list, item, min_quantity)`` — two rows at the
    same break point make pricing non-deterministic, which shows up as two
    customers quoted different prices by the same system on the same day.

    The row's own ``effective_from``/``effective_to`` override the list's
    window, which is what makes a bulk price adjustment ("+5% on category X
    from 1 April") expressible as an insert of future-dated rows rather than
    an UPDATE that destroys the current prices before they stop being current.
    """

    price_list = models.ForeignKey(
        PriceList, on_delete=models.CASCADE, related_name="items"
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="price_rows")

    unit_price = MoneyField()
    #: Alternative to an absolute price: a percentage off the item's list
    #: price, so a catalogue-wide increase does not need every promo row
    #: rewritten. Exactly one of the two is meaningful; ``is_percentage``
    #: says which.
    discount_percent = RateField()
    is_percentage = models.BooleanField(default=False)
    min_quantity = QuantityField()

    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_price_list_item"
        ordering = ["item", "-min_quantity"]
        constraints = [
            models.UniqueConstraint(
                fields=["price_list", "item", "min_quantity", "effective_from"],
                name="uq_price_list_item_break",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_price__gte=0)
                & models.Q(min_quantity__gte=0),
                name="ck_price_list_item_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_percent__gte=0)
                & models.Q(discount_percent__lte=1),
                name="ck_price_list_item_discount",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_from__isnull=True)
                | models.Q(effective_to__gte=models.F("effective_from")),
                name="ck_price_list_item_date_order",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "item", "is_active"], name="ix_price_item_lookup"
            ),
            models.Index(
                fields=["price_list", "item"], name="ix_price_item_list"
            ),
        ]


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

class LowStockAlert(TenantScopedModel):
    """Raised when available stock falls through an item's reorder point.

    Debounce
    --------
    Stock levels *flap*. A picker reserves 3 units, the order is cancelled
    and the reservation is released, an adjustment corrects a mis-count: an
    item sitting exactly on its reorder point can cross it dozens of times an
    hour. Naively inserting an alert per crossing produces a mailbox full of
    identical warnings, which trains everyone to ignore the alert entirely —
    the failure mode is not noise, it is that the *real* stock-out is missed.

    The debounce is structural, not a timer:

        ``UniqueConstraint(fields=["tenant", "item", "warehouse"],
                           condition=Q(acknowledged_at__isnull=True))``

    At most one *open* alert may exist per bin. The service inserts with
    ``get_or_create`` and simply absorbs the duplicate: the second and
    thousandth crossing update the existing open alert rather than creating a
    new one. A new alert can only appear after a human acknowledges the
    previous one — i.e. after the condition has actually been dealt with.
    Acknowledged rows accumulate as the history of how often this item runs
    dry, which is the input to fixing the reorder point.

    ``threshold_at_trigger`` and ``quantity_at_trigger`` are snapshots, not
    lookups: the reorder point may be changed in response to the alert, and
    the alert must still show what the rule was when it fired.
    """

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="low_stock_alerts")
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="low_stock_alerts"
    )

    threshold_at_trigger = QuantityField()
    quantity_at_trigger = QuantityField()
    #: Bumped every time the condition re-fires while the alert is open. A
    #: value of 400 by lunchtime says the reorder point is wrong, not that
    #: the warehouse is on fire.
    occurrence_count = models.PositiveIntegerField(default=1)

    triggered_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        "iam.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    notification_sent = models.BooleanField(default=False)
    notification_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "inventory_low_stock_alert"
        ordering = ["-triggered_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "item", "warehouse"],
                condition=models.Q(acknowledged_at__isnull=True),
                name="uq_low_stock_alert_open",
            ),
            # Acknowledgement must record a human; an alert that closed
            # itself is indistinguishable from one nobody read.
            models.CheckConstraint(
                condition=models.Q(acknowledged_at__isnull=True)
                | models.Q(acknowledged_by__isnull=False),
                name="ck_low_stock_alert_ack_by",
            ),
            models.CheckConstraint(
                condition=models.Q(occurrence_count__gte=1),
                name="ck_low_stock_alert_occurrences",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(
                fields=["tenant", "acknowledged_at"], name="ix_low_stock_open"
            ),
            models.Index(fields=["tenant", "item"], name="ix_low_stock_item"),
            models.Index(
                fields=["tenant", "notification_sent"], name="ix_low_stock_notify"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"LOW {self.item_id}@{self.warehouse_id} ({self.quantity_at_trigger})"

    @property
    def is_open(self) -> bool:
        return self.acknowledged_at is None
