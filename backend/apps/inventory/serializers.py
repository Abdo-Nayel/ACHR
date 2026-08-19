"""
Inventory serializers.

Three rules inherited from :mod:`apps.core.serializers` and re-stated here
because inventory is where they are most often broken:

* Quantities and costs are ``MoneyField``/``QuantityField`` — JSON strings.
  A stock quantity multiplied by a unit cost *is* money one operation later,
  so a float in a quantity is a float in the inventory valuation.
* ``status`` is never writable. ``StockAdjustment`` moves DRAFT -> APPROVED ->
  POSTED through POST sub-resources that run the service layer; a PATCH that
  wrote the column would mark an adjustment posted without creating a single
  stock movement or journal line.
* Derived columns (``quantity_available``, ``running_quantity_after``,
  ``total_value``) are outputs of ``apps.inventory.services.stock``, never
  inputs. A client that could write them could make the ledger and the
  warehouse disagree with no movement to explain it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

from apps.core.fields import ZERO
from apps.core.serializers import (
    MoneyField,
    QuantityField,
    RateField,
    ReadOnlyModelSerializer,
    TenantScopedSerializer,
)
from apps.inventory.models import (
    Item,
    ItemCategory,
    LowStockAlert,
    PriceList,
    PriceListItem,
    StockAdjustment,
    StockAdjustmentLine,
    StockLevel,
    StockMovement,
    UnitOfMeasure,
    Warehouse,
)


class UnitOfMeasureSerializer(TenantScopedSerializer):
    """A unit and, when derived, its factor back to the base unit."""

    conversion_factor = RateField(required=False)

    class Meta:
        model = UnitOfMeasure
        fields = (
            "id", "code", "name", "symbol", "kind", "base_uom",
            "conversion_factor", "decimal_places", "is_active",
            "created_at", "updated_at",
        )


class ItemCategorySerializer(TenantScopedSerializer):
    """Product category, with the default GL accounts items inherit."""

    children_count = serializers.IntegerField(source="children.count", read_only=True)

    class Meta:
        model = ItemCategory
        fields = (
            "id", "code", "name", "parent", "description",
            "default_income_account", "default_expense_account",
            "default_inventory_account", "is_active", "children_count",
            "created_at", "updated_at",
        )


class ItemSerializer(TenantScopedSerializer):
    """A sellable/stockable thing.

    ``valuation_method`` and ``allow_negative_stock`` are writable but consumed
    only by the stock service — changing the valuation method does not
    retrospectively revalue existing layers, which is why the field carries a
    warning in the client rather than a silent recalculation here.
    """

    sales_price = MoneyField(required=False)
    purchase_price = MoneyField(required=False)
    reorder_point = QuantityField(required=False)
    reorder_quantity = QuantityField(required=False)
    weight = QuantityField(required=False, allow_null=True)

    uom_code = serializers.CharField(source="uom.code", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    # Sum of on-hand across every warehouse; a scalar convenience for list
    # screens. The per-warehouse breakdown is GET /items/{id}/stock.
    quantity_on_hand = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = (
            "id", "sku", "name", "description", "type", "uom", "uom_code",
            "category", "category_name", "currency", "sales_price",
            "purchase_price", "income_account", "expense_account",
            "inventory_account", "tax_rate", "is_active", "track_inventory",
            "reorder_point", "reorder_quantity", "valuation_method",
            "allow_negative_stock", "barcode", "weight", "is_batch_tracked",
            "quantity_on_hand", "created_at", "updated_at",
        )

    def get_quantity_on_hand(self, obj: Item) -> str:
        total = getattr(obj, "total_on_hand", None)
        if total is None:
            total = sum(
                (level.quantity_on_hand for level in obj.stock_levels.all()), ZERO
            )
        return str(Decimal(total or ZERO))


class WarehouseSerializer(TenantScopedSerializer):
    """A physical or logical stock location."""

    class Meta:
        model = Warehouse
        fields = (
            "id", "code", "name", "address", "contact_name", "contact_phone",
            "is_default", "is_active", "created_at", "updated_at",
        )


class StockLevelSerializer(ReadOnlyModelSerializer):
    """Read-only: a stock level is a projection, not a document.

    The only legitimate way to change on-hand quantity is to record a movement
    (``apps.inventory.services.stock.apply_movement``), which updates this row
    inside the same transaction as the movement and the journal entry. Exposing
    a writable level would let a user set the number without the movement that
    explains it — the exact state the nightly drift check exists to detect.
    """

    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    warehouse_code = serializers.CharField(source="warehouse.code", read_only=True)

    class Meta:
        model = StockLevel
        fields = (
            "id", "item", "item_sku", "item_name", "warehouse",
            "warehouse_code", "quantity_on_hand", "quantity_reserved",
            "quantity_available", "currency", "average_cost", "total_value",
            "last_movement_at", "last_counted_at", "allow_negative",
            "created_at", "updated_at",
        )


class StockMovementSerializer(ReadOnlyModelSerializer):
    """Read-only. ``StockMovement`` is an append-only log.

    Every row is one immutable fact: "this quantity of this item entered or
    left this warehouse at this cost, at this instant". The running balance
    columns are computed from the row before it, so editing or deleting a
    movement invalidates every later row's ``running_quantity_after`` and
    breaks the reconciliation between the stock ledger and the GL inventory
    account. The model is an ``ImmutableFinancialModel`` (its ``delete()``
    raises); this serializer refuses writes at the API edge for the same
    reason. A mistake is corrected by posting an opposite movement — usually
    through a ``StockAdjustment`` — never by rewriting history.
    """

    item_sku = serializers.CharField(source="item.sku", read_only=True)
    warehouse_code = serializers.CharField(source="warehouse.code", read_only=True)

    class Meta:
        model = StockMovement
        fields = (
            "id", "item", "item_sku", "warehouse", "warehouse_code",
            "movement_type", "quantity_delta", "currency", "unit_cost",
            "total_cost", "running_quantity_after", "running_value_after",
            "reference_type", "reference_id", "journal_entry", "batch",
            "occurred_at", "notes", "created_at", "created_by",
        )


class StockAdjustmentLineSerializer(TenantScopedSerializer):
    """One item's counted-vs-expected variance.

    ``quantity_delta`` and ``value_delta`` are computed here, never accepted:
    ``ck_stock_adj_line_delta`` requires ``delta == counted - expected`` at the
    database level, and a client that could send its own delta could book a
    write-off larger than the variance it claims to be correcting.
    """

    quantity_expected = QuantityField(required=False)
    quantity_counted = QuantityField()
    quantity_delta = QuantityField(read_only=True)
    unit_cost = MoneyField(required=False)
    value_delta = MoneyField(read_only=True)
    # Defaulted to the line's position when omitted, so a client posting an
    # ordered array does not have to number the rows itself.
    line_number = serializers.IntegerField(required=False, min_value=1)

    item_sku = serializers.CharField(source="item.sku", read_only=True)

    class Meta:
        model = StockAdjustmentLine
        fields = (
            "id", "adjustment", "item", "item_sku", "batch", "line_number",
            "quantity_expected", "quantity_counted", "quantity_delta",
            "unit_cost", "value_delta", "notes",
        )
        read_only_fields = ("adjustment",)


class StockAdjustmentSerializer(TenantScopedSerializer):
    """Header + lines for a stock count correction or write-off.

    Lines are written with the header because an adjustment with no lines is
    not a document a reviewer can approve, and a two-step create would leave
    empty headers behind whenever the second call failed.

    Lines may only be replaced while the adjustment is DRAFT. Approving a
    document and then editing its lines is the whole attack the four-eyes
    control exists to stop, so an update against an APPROVED or POSTED header
    is refused here as well as by the service.
    """

    lines = StockAdjustmentLineSerializer(many=True, required=False)
    total_value_delta = MoneyField(read_only=True)
    warehouse_code = serializers.CharField(source="warehouse.code", read_only=True)
    line_count = serializers.IntegerField(source="lines.count", read_only=True)

    server_owned_fields = (
        "status", "number", "total_value_delta", "approved_by", "approved_at",
        "posted_at", "journal_entry",
    )

    class Meta:
        model = StockAdjustment
        fields = (
            "id", "number", "warehouse", "warehouse_code", "adjustment_date",
            "reason", "memo", "status", "currency", "total_value_delta",
            "approved_by", "approved_at", "posted_at", "journal_entry",
            "lines", "line_count", "created_at", "updated_at", "created_by",
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _derive(line: dict[str, Any], index: int) -> dict[str, Any]:
        expected = Decimal(str(line.get("quantity_expected") or ZERO))
        counted = Decimal(str(line.get("quantity_counted") or ZERO))
        unit_cost = Decimal(str(line.get("unit_cost") or ZERO))
        delta = counted - expected
        line["quantity_expected"] = expected
        line["quantity_counted"] = counted
        line["quantity_delta"] = delta
        line["unit_cost"] = unit_cost
        line["value_delta"] = delta * unit_cost
        line.setdefault("line_number", index)
        return line

    def _write_lines(self, adjustment: StockAdjustment, lines: list[dict]) -> None:
        tenant_id = self.get_tenant_id()
        actor_id = self.get_actor_id()
        total = ZERO
        for index, raw in enumerate(lines, start=1):
            data = self._derive(dict(raw), index)
            total += data["value_delta"]
            StockAdjustmentLine.objects.create(
                tenant_id=tenant_id,
                adjustment=adjustment,
                created_by_id=actor_id,
                updated_by_id=actor_id,
                **data,
            )
        StockAdjustment.objects.filter(pk=adjustment.pk).update(total_value_delta=total)
        adjustment.total_value_delta = total

    # -- write paths --------------------------------------------------------

    def create(self, validated_data: dict):
        lines = validated_data.pop("lines", [])
        adjustment = super().create(validated_data)
        self._write_lines(adjustment, lines)
        return adjustment

    def update(self, instance: StockAdjustment, validated_data: dict):
        lines = validated_data.pop("lines", None)
        if lines is not None and instance.status != StockAdjustment.Status.DRAFT:
            raise serializers.ValidationError(
                {"lines": "Lines can only be edited while the adjustment is a draft."}
            )
        adjustment = super().update(instance, validated_data)
        if lines is not None:
            adjustment.lines.all().delete()
            self._write_lines(adjustment, lines)
        return adjustment


class PriceListItemSerializer(TenantScopedSerializer):
    """One item's price on one list, optionally quantity- and date-bounded."""

    unit_price = MoneyField()
    discount_percent = RateField(required=False)
    min_quantity = QuantityField(required=False)
    item_sku = serializers.CharField(source="item.sku", read_only=True)

    class Meta:
        model = PriceListItem
        fields = (
            "id", "price_list", "item", "item_sku", "unit_price",
            "discount_percent", "is_percentage", "min_quantity",
            "effective_from", "effective_to", "is_active",
        )


class PriceListSerializer(TenantScopedSerializer):
    """A named, date-bounded set of prices."""

    item_count = serializers.IntegerField(source="items.count", read_only=True)

    class Meta:
        model = PriceList
        fields = (
            "id", "code", "name", "currency", "is_purchase_list", "is_default",
            "is_active", "priority", "effective_from", "effective_to",
            "item_count", "created_at", "updated_at",
        )


class PriceAdjustmentSerializer(serializers.Serializer):
    """Body for ``POST /price-lists/{id}/apply``.

    Exactly one of ``percentage`` / ``absolute_delta`` — "raise by 5% and also
    by 2.00" has no single meaning and the two orders give different answers.
    """

    percentage = RateField(required=False, allow_null=True)
    absolute_delta = MoneyField(required=False, allow_null=True)
    category_id = serializers.UUIDField(required=False, allow_null=True)
    item_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, allow_empty=False
    )
    effective_from = serializers.DateField(required=False, allow_null=True)
    round_to = MoneyField(required=False, allow_null=True)

    def validate(self, attrs: dict) -> dict:
        percentage = attrs.get("percentage")
        absolute = attrs.get("absolute_delta")
        if (percentage is None) == (absolute is None):
            raise serializers.ValidationError(
                "Provide exactly one of 'percentage' or 'absolute_delta'."
            )
        return attrs


class LowStockAlertSerializer(TenantScopedSerializer):
    """An open reorder signal for one item in one warehouse.

    Acknowledgement is a POST sub-resource, not a writable ``acknowledged_at``:
    the partial unique index only permits one *unacknowledged* alert per
    (item, warehouse), so clearing the timestamp by hand would let a duplicate
    alert be created behind the constraint's back.
    """

    threshold_at_trigger = QuantityField(read_only=True)
    quantity_at_trigger = QuantityField(read_only=True)
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    warehouse_code = serializers.CharField(source="warehouse.code", read_only=True)
    is_open = serializers.BooleanField(read_only=True)

    server_owned_fields = (
        "acknowledged_at", "acknowledged_by", "notification_sent",
        "notification_sent_at", "occurrence_count", "triggered_at",
        "last_seen_at",
    )

    class Meta:
        model = LowStockAlert
        fields = (
            "id", "item", "item_sku", "item_name", "warehouse",
            "warehouse_code", "threshold_at_trigger", "quantity_at_trigger",
            "occurrence_count", "triggered_at", "last_seen_at",
            "acknowledged_at", "acknowledged_by", "notification_sent",
            "notification_sent_at", "is_open", "created_at",
        )
