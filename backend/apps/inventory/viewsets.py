"""
Inventory viewsets.

ABAC note — why these endpoints do not use the scoped queryset
-------------------------------------------------------------
``apps.iam.permissions.build_scope_q`` fails closed: a resource with no
``ScopeRule`` row returns ``DENY_ALL``. That is exactly right for
employee-shaped data, where "no rule" must never mean "everything". But
``config/permissions.json`` defines no scope rules for ``item``,
``warehouse``, ``price_list``, ``stock_movement`` or ``adjustment`` — a
product catalogue has no per-actor dimension; either you may read the
catalogue or you may not, and that is an RBAC question
(``inventory.item.read``). Inheriting the scoped queryset here would return an
empty catalogue to every user in every tenant. :class:`RbacOnlyQuerysetMixin`
therefore keeps the tenant filter (which comes from ``TenantManager``, not from
ABAC) and skips the scope ``Q``. It is opt-in per viewset, so nothing is
silently unscoped: adding a ``ScopeRule`` for ``item`` later means removing
the mixin from one class.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from apps.core.fields import ZERO
from apps.core.pagination import LedgerCursorPagination, SmallPagePagination


class MovementCursorPagination(LedgerCursorPagination):
    """``LedgerCursorPagination`` retuned to this app's date column.

    ``CursorPagination`` re-applies its own ``ordering`` to the queryset, so a
    viewset that calls ``.order_by()`` itself is overruled and the paginator
    sorts by ``entry_date`` — a column ``StockMovement`` does not have. The
    result was a hard 500 (``FieldError``) on every ``GET /stock-movements/``,
    with the ordering line in the viewset looking like it had already handled
    it. Overriding ``ordering`` on the paginator is the only place that
    actually decides.

    ``occurred_at`` and not ``created_at``: a backdated correction keyed today
    belongs next to the movement it corrects, not at the top of the log.
    """

    ordering = ("-occurred_at", "-created_at", "-id")
from apps.core.serializers import TransitionSerializer
from apps.core.viewsets import (
    ReadOnlyTenantViewSet,
    TenantModelViewSet,
    raise_as_api_error,
)
from apps.inventory.models import (
    Item,
    ItemCategory,
    LowStockAlert,
    PriceList,
    StockAdjustment,
    StockLevel,
    StockMovement,
    UnitOfMeasure,
    Warehouse,
)
from apps.inventory.serializers import (
    ItemCategorySerializer,
    ItemSerializer,
    LowStockAlertSerializer,
    PriceAdjustmentSerializer,
    PriceListItemSerializer,
    PriceListSerializer,
    StockAdjustmentSerializer,
    StockLevelSerializer,
    StockMovementSerializer,
    UnitOfMeasureSerializer,
    WarehouseSerializer,
)


class RbacOnlyQuerysetMixin:
    """Tenant-scoped, RBAC-guarded, but not ABAC-filtered. See module docstring.

    Bypasses :class:`apps.iam.permissions.ScopedQuerysetMixin` by reading the
    viewset's ``queryset`` directly. The tenant boundary is untouched:
    ``TenantManager.get_queryset`` still filters to the bound tenant and
    returns ``.none()`` when no tenant is bound.
    """

    def get_queryset(self):
        # ``self.queryset.model._default_manager.all()``, never
        # ``self.queryset.all()``. The class attribute was evaluated at import
        # time, with no tenant bound, so ``TenantManager`` failed closed and
        # froze an empty queryset for the life of the process — ``.all()`` on
        # ``.none()`` is still nothing. The symptom is the worst kind: HTTP
        # 200, a well-formed envelope and an empty ``results`` array on every
        # request, with no error anywhere. Re-deriving from the manager runs it
        # inside the request, where the tenant actually is bound. This mirrors
        # ``apps.core.viewsets.TenantViewSetMixin.get_queryset``, which
        # documents the same trap.
        queryset = self.queryset.model._default_manager.all()
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.prefetch_related:
            queryset = queryset.prefetch_related(*self.prefetch_related)
        ordering = getattr(self, "ordering", None)
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset


class UnitOfMeasureViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Units of measure. Guarded by the item permissions: a unit is part of the
    item catalogue and there is no separate ``inventory.uom`` permission."""

    permission_domain = "inventory"
    resource = "item"
    queryset = UnitOfMeasure.objects.all()
    serializer_class = UnitOfMeasureSerializer
    select_related = ("base_uom",)
    pagination_class = SmallPagePagination
    search_fields = ("code", "name", "symbol")
    filterset_fields = ("kind", "is_active")


class ItemCategoryViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Item categories. Guarded by the item permissions (see above)."""

    permission_domain = "inventory"
    resource = "item"
    queryset = ItemCategory.objects.all()
    serializer_class = ItemCategorySerializer
    select_related = ("parent",)
    pagination_class = SmallPagePagination
    search_fields = ("code", "name")
    filterset_fields = ("parent", "is_active")


class ItemViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Product/service catalogue, plus its stock position."""

    permission_domain = "inventory"
    resource = "item"
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    select_related = ("uom", "category", "tax_rate")
    prefetch_related = ("stock_levels",)
    search_fields = ("sku", "name", "barcode")
    filterset_fields = ("type", "category", "is_active", "track_inventory")
    extra_permissions = {
        "stock": ["inventory.item.read"],
        "movements": ["inventory.stock_movement.read"],
        # No ``inventory.item.delete`` exists in the catalogue; map DELETE onto
        # archive so the caller gets the 405 explaining that items are
        # archived, rather than a 403 that looks like a missing grant.
        "destroy": ["inventory.item.archive"],
    }

    @action(detail=True, methods=["get"], url_path="stock")
    def stock(self, request, pk=None):
        """Stock levels for this item across every warehouse.

        Returned unpaginated on purpose: the number of rows is bounded by the
        tenant's warehouse count (single digits for almost every tenant), and
        a client rendering an item page needs the whole set to show a total.
        """
        item = self.get_object()
        levels = list(
            StockLevel.objects.filter(item=item)
            .select_related("warehouse")
            .order_by("warehouse__code")
        )
        data = StockLevelSerializer(levels, many=True, context=self.get_serializer_context()).data
        totals = {
            "quantity_on_hand": str(
                sum((level.quantity_on_hand for level in levels), ZERO)
            ),
            "quantity_available": str(
                sum((level.quantity_available for level in levels), ZERO)
            ),
            "total_value": str(sum((level.total_value for level in levels), ZERO)),
        }
        return Response({"item": str(item.pk), "totals": totals, "levels": data})

    @action(detail=True, methods=["get"], url_path="movements",
            pagination_class=MovementCursorPagination)
    def movements(self, request, pk=None):
        """The append-only movement log for this item, newest first."""
        item = self.get_object()
        movements = (
            StockMovement.objects.filter(item=item)
            .select_related("warehouse", "batch")
            .order_by("-occurred_at", "-created_at")
        )
        warehouse_id = request.query_params.get("warehouse")
        if warehouse_id:
            movements = movements.filter(warehouse_id=warehouse_id)

        page = self.paginate_queryset(movements)
        serializer = StockMovementSerializer(
            page if page is not None else movements,
            many=True,
            context=self.get_serializer_context(),
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class WarehouseViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Stock locations.

    The catalogue has only ``inventory.warehouse.manage`` and ``.read``, so
    every write verb maps onto ``manage`` — the auto-generated
    ``inventory.warehouse.create`` does not exist and an unknown codename
    denies.
    """

    permission_domain = "inventory"
    resource = "warehouse"
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer
    pagination_class = SmallPagePagination
    search_fields = ("code", "name")
    filterset_fields = ("is_active", "is_default")
    extra_permissions = {
        "POST": ["inventory.warehouse.manage"],
        "PUT": ["inventory.warehouse.manage"],
        "PATCH": ["inventory.warehouse.manage"],
        "DELETE": ["inventory.warehouse.manage"],
    }


class StockLevelViewSet(RbacOnlyQuerysetMixin, ReadOnlyTenantViewSet):
    """Read-only projection of on-hand / reserved / available quantities."""

    permission_domain = "inventory"
    resource = "item"
    queryset = StockLevel.objects.all()
    serializer_class = StockLevelSerializer
    select_related = ("item", "warehouse")
    filterset_fields = ("item", "warehouse")
    ordering_fields = ("quantity_available", "total_value", "last_movement_at")


class StockMovementViewSet(RbacOnlyQuerysetMixin, ReadOnlyTenantViewSet):
    """Read-only: the movement log is append-only. See the serializer."""

    permission_domain = "inventory"
    resource = "stock_movement"
    queryset = StockMovement.objects.all()
    serializer_class = StockMovementSerializer
    select_related = ("item", "warehouse", "batch")
    pagination_class = MovementCursorPagination
    filterset_fields = ("item", "warehouse", "movement_type", "journal_entry")
    ordering_fields = ("occurred_at", "created_at")
    ordering = ("-occurred_at", "-created_at", "-id")

    def get_queryset(self):
        # Ordered here *and* on the paginator. The queryset order is what a
        # non-paginated caller sees; the paginator's ``ordering`` is what the
        # cursor is built from, and it wins — see MovementCursorPagination.
        return super().get_queryset().order_by("-occurred_at", "-created_at", "-id")


class StockAdjustmentViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Count corrections and write-offs: DRAFT -> APPROVED -> POSTED.

    Both transitions are POST sub-resources. ``approve`` records the second
    pair of eyes (``ck_stock_adjustment_approver`` refuses an approved row with
    no approver), and ``post`` is the only thing that touches stock or the
    ledger — it delegates every line to
    ``apps.inventory.services.stock.apply_movement`` so that the movement, the
    level update and the journal entry are written by the same code path a
    purchase receipt uses.
    """

    permission_domain = "inventory"
    resource = "adjustment"
    queryset = StockAdjustment.objects.all()
    serializer_class = StockAdjustmentSerializer
    select_related = ("warehouse", "journal_entry")
    prefetch_related = ("lines", "lines__item")
    filterset_fields = ("status", "warehouse", "reason", "adjustment_date")
    extra_permissions = {
        # There is no ``inventory.adjustment.update`` codename: editing a
        # draft is part of authoring it, so it rides on ``create``.
        "PUT": ["inventory.adjustment.create"],
        "PATCH": ["inventory.adjustment.create"],
        "DELETE": ["inventory.adjustment.create"],
        "approve": ["inventory.adjustment.approve"],
        "post_to_ledger": ["inventory.stock_movement.post"],
    }

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """Record the second pair of eyes. DRAFT -> APPROVED."""
        adjustment = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                locked = (
                    StockAdjustment.objects.select_for_update()
                    .get(pk=adjustment.pk)
                )
                locked.assert_can_transition(StockAdjustment.Status.APPROVED)
                locked.status = StockAdjustment.Status.APPROVED
                locked.approved_by_id = request.user.id
                locked.approved_at = timezone.now()
                locked.updated_by_id = request.user.id
                locked.save(update_fields=[
                    "status", "approved_by", "approved_at", "updated_by", "updated_at",
                ])
        except Exception as exc:  # noqa: BLE001 - re-raised as an API error
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response(self.get_serializer(locked).data)

    # Named ``post_to_ledger``, exposed as ``.../post``: a method literally
    # called ``post`` on an APIView is picked up by ``dispatch`` as the HTTP
    # POST handler for *every* route of this viewset, so ``POST
    # /stock-adjustments/{id}/`` would silently run this action instead of
    # returning 405.
    @action(detail=True, methods=["post"], url_path="post")
    def post_to_ledger(self, request, pk=None):
        """APPROVED -> POSTED: write one stock movement per line.

        One transaction for the whole document. A half-posted adjustment —
        three of five lines moved, header still APPROVED — is the worst
        possible outcome because the totals still look plausible; either every
        line moves or none does.
        """
        from apps.inventory.services.stock import apply_movement

        adjustment = self.get_object()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                locked = (
                    StockAdjustment.objects.select_for_update()
                    .select_related("warehouse")
                    .get(pk=adjustment.pk)
                )
                locked.assert_can_transition(StockAdjustment.Status.POSTED)
                lines = list(locked.lines.select_related("item", "batch").all())
                if not lines:
                    from apps.core.exceptions import DomainError

                    raise DomainError("An adjustment with no lines cannot be posted.")

                total = ZERO
                journal_entry_id = None
                for line in lines:
                    if line.quantity_delta == ZERO:
                        # A counted-equals-expected line is evidence the count
                        # happened; it is simply not a movement.
                        continue
                    result = apply_movement(
                        tenant_id=locked.tenant_id,
                        item=line.item,
                        warehouse=locked.warehouse,
                        movement_type=StockMovement.MovementType.ADJUSTMENT,
                        quantity_delta=line.quantity_delta,
                        unit_cost=line.unit_cost,
                        reference_type="inventory.StockAdjustment",
                        reference_id=locked.pk,
                        batch=line.batch,
                        notes=locked.memo[:255],
                        user_id=request.user.id,
                    )
                    total += Decimal(line.value_delta)
                    if result.journal_entry is not None and journal_entry_id is None:
                        journal_entry_id = result.journal_entry.pk

                locked.status = StockAdjustment.Status.POSTED
                locked.posted_at = timezone.now()
                locked.total_value_delta = total
                locked.journal_entry_id = journal_entry_id
                locked.updated_by_id = request.user.id
                locked.save(update_fields=[
                    "status", "posted_at", "total_value_delta", "journal_entry",
                    "updated_by", "updated_at",
                ])
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response(self.get_serializer(locked).data)


class PriceListViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Price lists, and the bulk re-pricing action."""

    permission_domain = "inventory"
    resource = "price_list"
    queryset = PriceList.objects.all()
    serializer_class = PriceListSerializer
    pagination_class = SmallPagePagination
    search_fields = ("code", "name")
    filterset_fields = ("is_active", "is_purchase_list", "currency")
    extra_permissions = {
        "POST": ["inventory.price_list.manage"],
        "PUT": ["inventory.price_list.manage"],
        "PATCH": ["inventory.price_list.manage"],
        "DELETE": ["inventory.price_list.manage"],
        "apply": ["inventory.price_list.manage"],
    }

    @action(detail=True, methods=["post"], url_path="apply")
    def apply(self, request, pk=None):
        """Bulk price adjustment.

        Delegates to ``bulk_adjust_prices``, which closes the current rows and
        inserts future-dated successors rather than updating prices in place —
        so last month's invoice can still be explained and "prices go up on
        1 April" means 1 April, not "the instant this request commits".
        """
        from apps.inventory.services.stock import bulk_adjust_prices

        price_list = self.get_object()
        body = PriceAdjustmentSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payload = body.validated_data

        try:
            with transaction.atomic():
                rows = bulk_adjust_prices(
                    tenant_id=price_list.tenant_id,
                    price_list=price_list,
                    percentage=payload.get("percentage"),
                    absolute_delta=payload.get("absolute_delta"),
                    category_id=payload.get("category_id"),
                    item_ids=payload.get("item_ids"),
                    effective_from=payload.get("effective_from"),
                    round_to=payload.get("round_to"),
                    user_id=request.user.id,
                )
        except Exception as exc:  # noqa: BLE001
            raise_as_api_error(exc)
            raise  # pragma: no cover

        return Response(
            {
                "price_list": str(price_list.pk),
                "adjusted_count": len(rows),
                "rows": PriceListItemSerializer(
                    rows, many=True, context=self.get_serializer_context()
                ).data,
            },
            status=status.HTTP_200_OK,
        )


class LowStockAlertViewSet(RbacOnlyQuerysetMixin, TenantModelViewSet):
    """Reorder signals raised by the stock service."""

    permission_domain = "inventory"
    resource = "item"
    queryset = LowStockAlert.objects.all()
    serializer_class = LowStockAlertSerializer
    select_related = ("item", "warehouse")
    filterset_fields = ("item", "warehouse", "notification_sent")
    extra_permissions = {"acknowledge": ["inventory.item.update"]}

    def get_queryset(self):
        queryset = super().get_queryset()
        # ?open=true narrows to unacknowledged alerts; an acknowledged alert
        # is history. ``request`` is absent during schema generation.
        request = getattr(self, "request", None)
        open_only = request.query_params.get("open") if request is not None else None
        if open_only in ("1", "true", "True"):
            queryset = queryset.filter(acknowledged_at__isnull=True)
        return queryset

    @action(detail=True, methods=["post"], url_path="acknowledge")
    def acknowledge(self, request, pk=None):
        """Mark the alert seen. Idempotent: re-acknowledging is a no-op.

        Re-acknowledging must not move ``acknowledged_at`` forward — the value
        that matters for a "how long did we sit on this?" report is the *first*
        time somebody looked at it.
        """
        alert = get_object_or_404(self.get_queryset(), pk=pk)
        self.check_object_permissions(request, alert)
        if alert.acknowledged_at is None:
            alert.acknowledged_at = timezone.now()
            alert.acknowledged_by_id = request.user.id
            alert.updated_by_id = request.user.id
            alert.save(update_fields=[
                "acknowledged_at", "acknowledged_by", "updated_by", "updated_at",
            ])
        return Response(self.get_serializer(alert).data)
