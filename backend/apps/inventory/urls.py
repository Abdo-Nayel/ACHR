"""
Inventory URL registration.

The four original prefixes are unchanged. ``units-of-measure``,
``item-categories``, ``stock-adjustments``, ``price-lists`` and
``low-stock-alerts`` are new: the viewsets existed in
:mod:`apps.inventory.viewsets` with no route, so the whole configuration and
adjustment surface of the module was unreachable from the API.
"""

from __future__ import annotations

from apps.inventory.viewsets import (
    ItemCategoryViewSet,
    ItemViewSet,
    LowStockAlertViewSet,
    PriceListViewSet,
    StockAdjustmentViewSet,
    StockLevelViewSet,
    StockMovementViewSet,
    UnitOfMeasureViewSet,
    WarehouseViewSet,
)


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(r"items", ItemViewSet, basename="items")
    router.register(r"warehouses", WarehouseViewSet, basename="warehouses")
    router.register(r"stock-levels", StockLevelViewSet, basename="stock-levels")
    router.register(r"stock-movements", StockMovementViewSet, basename="stock-movements")
    router.register(
        r"units-of-measure", UnitOfMeasureViewSet, basename="units-of-measure"
    )
    router.register(r"item-categories", ItemCategoryViewSet, basename="item-categories")
    router.register(
        r"stock-adjustments", StockAdjustmentViewSet, basename="stock-adjustments"
    )
    router.register(r"price-lists", PriceListViewSet, basename="price-lists")
    router.register(
        r"low-stock-alerts", LowStockAlertViewSet, basename="low-stock-alerts"
    )
