"""Recording purchases: creates the order, inventory with a landed cost basis, and the cash ledger entry.

Idempotent on (source_key, order_ref): re-recording the same order returns the existing one and creates nothing.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DealOpportunity, InventoryItem, InventoryMovement, Product, PurchaseOrder, PurchaseOrderItem
from app.money import allocate
from app.services import ledger
from app.services.audit import audit


class DuplicatePurchase(Exception):
    def __init__(self, order: PurchaseOrder):
        super().__init__(f"Order {order.source_key}:{order.order_ref} already recorded (#{order.id})")
        self.order = order


@dataclass
class LineIn:
    description: str
    unit_price_p: int
    quantity: int = 1
    product_id: int | None = None
    condition: str = "unknown"


@dataclass
class PurchaseIn:
    source_key: str
    order_ref: str
    purchased_on: date
    lines: list[LineIn]
    shipping_p: int = 0
    fees_p: int = 0
    other_costs_p: int = 0
    seller: str | None = None
    listing_url: str | None = None
    opportunity_id: int | None = None
    notes: str | None = None
    provenance: str = "manual"
    status: str = "awaiting_delivery"
    extra: dict = field(default_factory=dict)


def new_sku(s: Session, prefix: str = "DH") -> str:
    while True:
        sku = f"{prefix}-{secrets.token_hex(3).upper()}"
        if not s.scalar(select(InventoryItem.id).where(InventoryItem.sku == sku)):
            return sku


def find_existing(s: Session, source_key: str, order_ref: str) -> PurchaseOrder | None:
    return s.scalar(select(PurchaseOrder).where(PurchaseOrder.source_key == source_key,
                                                PurchaseOrder.order_ref == order_ref))


def record_purchase(s: Session, data: PurchaseIn, *, enforce_cash: bool = False) -> PurchaseOrder:
    if not data.order_ref.strip():
        raise ValueError("An order reference is required (use the seller's order number, lot number or a receipt id)")
    if not data.lines:
        raise ValueError("A purchase needs at least one line")
    for ln in data.lines:
        if ln.quantity <= 0 or ln.unit_price_p < 0:
            raise ValueError("Quantities must be positive and prices non-negative")
    if min(data.shipping_p, data.fees_p, data.other_costs_p) < 0:
        raise ValueError("Costs cannot be negative")
    existing = find_existing(s, data.source_key, data.order_ref.strip())
    if existing:
        raise DuplicatePurchase(existing)

    items_total = sum(ln.unit_price_p * ln.quantity for ln in data.lines)
    extra_costs = data.shipping_p + data.fees_p + data.other_costs_p
    total = items_total + extra_costs
    if enforce_cash and total > ledger.cash_balance(s):
        raise ValueError("Purchase exceeds available cash")

    po = PurchaseOrder(source_key=data.source_key, order_ref=data.order_ref.strip(), purchased_on=data.purchased_on,
                       seller=data.seller, listing_url=data.listing_url, opportunity_id=data.opportunity_id,
                       items_total_p=items_total, shipping_p=data.shipping_p, fees_p=data.fees_p,
                       other_costs_p=data.other_costs_p, total_p=total, notes=data.notes, provenance=data.provenance)
    s.add(po)
    s.flush()
    # Spread acquisition costs over lines by price (largest remainder → exact to the penny).
    shares = allocate(extra_costs, [ln.unit_price_p * ln.quantity for ln in data.lines])
    for ln, share in zip(data.lines, shares):
        poi = PurchaseOrderItem(order_id=po.id, product_id=ln.product_id, description=ln.description,
                                condition=ln.condition, quantity=ln.quantity, unit_price_p=ln.unit_price_p,
                                allocated_costs_p=share)
        s.add(poi)
        s.flush()
        cost = ln.unit_price_p * ln.quantity + share
        name = ln.description
        if ln.product_id and not name:
            name = s.get(Product, ln.product_id).name
        item = InventoryItem(sku=new_sku(s), product_id=ln.product_id, po_item_id=poi.id, name=name,
                             condition=ln.condition, quantity_initial=ln.quantity, quantity_on_hand=ln.quantity,
                             purchase_cost_p=ln.unit_price_p * ln.quantity, allocated_costs_p=share,
                             cost_basis_p=cost, remaining_cost_p=cost, status=data.status,
                             acquired_on=data.purchased_on, source_key=data.source_key, listing_url=data.listing_url,
                             order_ref=po.order_ref)
        s.add(item)
        s.flush()
        s.add(InventoryMovement(item_id=item.id, kind="purchase", quantity_delta=ln.quantity, cost_delta_p=cost,
                                ref_type="purchase_order", ref_id=po.id))
    ledger.post(s, "purchase", -total, f"Purchase {data.source_key} {po.order_ref}", data.purchased_on,
                "purchase_order", po.id)
    if data.opportunity_id:
        opp = s.get(DealOpportunity, data.opportunity_id)
        if opp:
            opp.status = "purchased"
    audit(s, "purchase_order", po.id, "create", {"total_p": total, "lines": len(data.lines)})
    s.flush()
    return po
