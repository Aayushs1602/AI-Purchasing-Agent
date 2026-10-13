"""Read-only database browser at /admin (SQLAdmin), for debugging and demos.

Views are read-only on purpose: the agent's validators trust database state, and hand edits
here would bypass the PO state machine. To change a scenario, edit its YAML and reset.
There is no login, so `ADMIN_UI_ENABLED` must be off anywhere but a local machine.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqladmin import Admin, ModelView
from sqlalchemy import Engine

from app.models import (
    Budget,
    DemandForecast,
    InventoryRecord,
    Node,
    Product,
    PurchaseOrder,
    PurchaseOrderEvent,
    Recommendation,
    SalesRecord,
    Supplier,
    SupplierTerm,
)


class ReadOnlyView(ModelView):
    can_create = False
    can_edit = False
    can_delete = False
    can_export = True
    page_size = 50


class RecommendationAdmin(ReadOnlyView, model=Recommendation):
    icon = "fa-solid fa-lightbulb"
    column_list = [
        Recommendation.id,
        Recommendation.scenario,
        Recommendation.title,
        Recommendation.sku,
        Recommendation.supplier_id,
        Recommendation.quantity,
        Recommendation.status,
        Recommendation.created_on,
    ]
    column_searchable_list = [Recommendation.id, Recommendation.scenario, Recommendation.sku]
    column_sortable_list = [Recommendation.id, Recommendation.quantity, Recommendation.status]


class PurchaseOrderAdmin(ReadOnlyView, model=PurchaseOrder):
    icon = "fa-solid fa-file-invoice"
    column_list = [
        PurchaseOrder.id,
        PurchaseOrder.sku,
        PurchaseOrder.status,
        PurchaseOrder.revision,
        PurchaseOrder.quantity,
        PurchaseOrder.confirmed_quantity,
        PurchaseOrder.unit_cost,
        PurchaseOrder.confirmed_unit_cost,
        PurchaseOrder.committed_value,
        PurchaseOrder.approval_status,
        PurchaseOrder.eta,
        PurchaseOrder.created_by,
        PurchaseOrder.recommendation_id,
    ]
    column_searchable_list = [PurchaseOrder.id, PurchaseOrder.sku, PurchaseOrder.status]
    column_sortable_list = [PurchaseOrder.id, PurchaseOrder.status, PurchaseOrder.eta]


class PurchaseOrderEventAdmin(ReadOnlyView, model=PurchaseOrderEvent):
    name = "PO event"
    icon = "fa-solid fa-clock-rotate-left"
    column_list = [
        PurchaseOrderEvent.id,
        PurchaseOrderEvent.po_id,
        PurchaseOrderEvent.event,
        PurchaseOrderEvent.from_status,
        PurchaseOrderEvent.to_status,
        PurchaseOrderEvent.actor,
        PurchaseOrderEvent.detail,
        PurchaseOrderEvent.on,
    ]
    column_searchable_list = [PurchaseOrderEvent.po_id, PurchaseOrderEvent.event]
    column_sortable_list = [PurchaseOrderEvent.id, PurchaseOrderEvent.po_id]
    column_default_sort = [(PurchaseOrderEvent.id, True)]


class InventoryAdmin(ReadOnlyView, model=InventoryRecord):
    name = "Inventory"
    name_plural = "Inventory"
    icon = "fa-solid fa-boxes-stacked"
    column_list = [
        InventoryRecord.sku,
        InventoryRecord.node_id,
        InventoryRecord.on_hand,
        InventoryRecord.reserved,
        InventoryRecord.safety_stock,
        InventoryRecord.storage_capacity,
        InventoryRecord.review_period_days,
        InventoryRecord.counted_on,
    ]
    column_searchable_list = [InventoryRecord.sku]
    column_sortable_list = [InventoryRecord.sku, InventoryRecord.on_hand]


class SupplierTermAdmin(ReadOnlyView, model=SupplierTerm):
    name = "Supplier term"
    icon = "fa-solid fa-handshake"
    column_list = [
        SupplierTerm.supplier_id,
        SupplierTerm.sku,
        SupplierTerm.lead_time_days,
        SupplierTerm.moq,
        SupplierTerm.case_pack,
        SupplierTerm.unit_cost,
        SupplierTerm.mock_ack,
        SupplierTerm.note,
    ]
    column_searchable_list = [SupplierTerm.sku, SupplierTerm.supplier_id]
    column_sortable_list = [SupplierTerm.sku, SupplierTerm.moq, SupplierTerm.unit_cost]


class BudgetAdmin(ReadOnlyView, model=Budget):
    icon = "fa-solid fa-wallet"
    column_list = [
        Budget.node_id,
        Budget.category,
        Budget.period,
        Budget.total,
        Budget.committed,
        "available",
    ]
    column_searchable_list = [Budget.category]
    column_sortable_list = [Budget.category, Budget.committed]


class DemandForecastAdmin(ReadOnlyView, model=DemandForecast):
    name = "Demand forecast"
    icon = "fa-solid fa-chart-line"
    column_list = [
        DemandForecast.sku,
        DemandForecast.node_id,
        DemandForecast.day,
        DemandForecast.units,
    ]
    column_searchable_list = [DemandForecast.sku]
    column_sortable_list = [DemandForecast.sku, DemandForecast.day]
    column_default_sort = [(DemandForecast.sku, False), (DemandForecast.day, False)]


class SalesAdmin(ReadOnlyView, model=SalesRecord):
    name = "Sale"
    name_plural = "Sales history"
    icon = "fa-solid fa-receipt"
    column_list = [SalesRecord.sku, SalesRecord.node_id, SalesRecord.day, SalesRecord.units]
    column_searchable_list = [SalesRecord.sku]
    column_sortable_list = [SalesRecord.sku, SalesRecord.day]
    column_default_sort = [(SalesRecord.sku, False), (SalesRecord.day, False)]


class ProductAdmin(ReadOnlyView, model=Product):
    icon = "fa-solid fa-tag"
    column_list = [Product.sku, Product.name, Product.category]
    column_searchable_list = [Product.sku, Product.name, Product.category]


class SupplierAdmin(ReadOnlyView, model=Supplier):
    icon = "fa-solid fa-truck"
    column_list = [Supplier.id, Supplier.name, Supplier.on_time_rate, Supplier.fill_rate]


class NodeAdmin(ReadOnlyView, model=Node):
    name = "Node"
    icon = "fa-solid fa-warehouse"
    column_list = [Node.id, Node.name, Node.city]


VIEWS = [
    RecommendationAdmin,
    PurchaseOrderAdmin,
    PurchaseOrderEventAdmin,
    InventoryAdmin,
    SupplierTermAdmin,
    BudgetAdmin,
    DemandForecastAdmin,
    SalesAdmin,
    ProductAdmin,
    SupplierAdmin,
    NodeAdmin,
]


def mount_admin(app: FastAPI, engine: Engine) -> Admin:
    admin = Admin(app, engine, title="Purchasing Agent DB", base_url="/admin")
    for view in VIEWS:
        admin.add_view(view)
    return admin
