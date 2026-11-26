"""Sales, refunds, returns and cancellations. Every operation posts matching inventory and ledger records."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import InventoryItem, SalesOrder, SalesOrderItem
from app.money import allocate
from app.services import ledger
from app.services.audit import audit
from app.services.inventory import move, release_cost


@dataclass
class SaleLineIn:
    inventory_item_id: int
    quantity: int
    unit_price_p: int


@dataclass
class SaleIn:
    platform: str
    order_ref: str
    sold_on: date
    lines: list[SaleLineIn]
    buyer_shipping_p: int = 0
    platform_fees_p: int = 0
    ad_fees_p: int = 0
    postage_p: int = 0
    packaging_p: int = 0
    other_costs_p: int = 0
    notes: str | None = None


class DuplicateSale(Exception):
    pass


def record_sale(s: Session, data: SaleIn) -> SalesOrder:
    if not data.order_ref.strip():
        raise ValueError("Order reference required")
    if not data.lines:
        raise ValueError("A sale needs at least one line")
    if s.scalar(select(SalesOrder.id).where(SalesOrder.platform == data.platform,
                                            SalesOrder.order_ref == data.order_ref.strip())):
        raise DuplicateSale(f"Sale {data.platform}:{data.order_ref} already recorded")
    costs = [data.buyer_shipping_p, data.platform_fees_p, data.ad_fees_p, data.postage_p, data.packaging_p,
             data.other_costs_p]
    if min(costs) < 0:
        raise ValueError("Amounts cannot be negative")
    # Validate quantities across all lines first (the same item may appear twice).
    wanted: dict[int, int] = {}
    for ln in data.lines:
        if ln.quantity <= 0 or ln.unit_price_p < 0:
            raise ValueError("Quantities must be positive and prices non-negative")
        wanted[ln.inventory_item_id] = wanted.get(ln.inventory_item_id, 0) + ln.quantity
    for item_id, qty in wanted.items():
        item = s.get(InventoryItem, item_id)
        if item is None:
            raise ValueError(f"Inventory item {item_id} not found")
        if qty > item.quantity_on_hand:
            raise ValueError(f"{item.sku}: only {item.quantity_on_hand} on hand, cannot sell {qty}")

    so = SalesOrder(platform=data.platform, order_ref=data.order_ref.strip(), sold_on=data.sold_on,
                    buyer_shipping_p=data.buyer_shipping_p, platform_fees_p=data.platform_fees_p,
                    ad_fees_p=data.ad_fees_p, postage_p=data.postage_p, packaging_p=data.packaging_p,
                    other_costs_p=data.other_costs_p, notes=data.notes)
    s.add(so)
    s.flush()
    for ln in data.lines:
        item = s.get(InventoryItem, ln.inventory_item_id)
        cogs = release_cost(item, ln.quantity)
        move(s, item, "sale", -ln.quantity, -cogs, "sales_order", so.id)
        if item.quantity_on_hand == 0:
            item.status = "sold"
        s.add(SalesOrderItem(order_id=so.id, inventory_item_id=item.id, quantity=ln.quantity,
                             unit_price_p=ln.unit_price_p, cogs_p=cogs))
    s.flush()
    s.refresh(so)
    gross = gross_revenue(so)
    selling = selling_costs_total(so)
    ledger.post(s, "sale_receipt", gross, f"Sale {so.platform} {so.order_ref}", so.sold_on, "sales_order", so.id)
    if selling:
        ledger.post(s, "selling_cost", -selling, f"Fees/postage {so.platform} {so.order_ref}", so.sold_on,
                    "sales_order", so.id)
    audit(s, "sales_order", so.id, "create", {"gross_p": gross, "selling_costs_p": selling})
    return so


def gross_revenue(so: SalesOrder) -> int:
    return sum(i.unit_price_p * i.quantity for i in so.items) + so.buyer_shipping_p


def selling_costs_total(so: SalesOrder) -> int:
    return so.platform_fees_p + so.ad_fees_p + so.postage_p + so.packaging_p + so.other_costs_p


@dataclass
class SaleFigures:
    gross_p: int
    selling_costs_p: int
    refunds_p: int
    fee_credits_p: int
    net_proceeds_p: int
    cogs_p: int
    profit_p: int
    roi_pct: float | None
    margin_pct: float | None


def figures(so: SalesOrder) -> SaleFigures:
    """Net proceeds = revenue + buyer shipping − fees − postage − packaging − refunds (+ fee credits).
    Profit = net proceeds − cost of goods still sold (returned units' cost goes back to stock)."""
    gross = gross_revenue(so)
    costs = selling_costs_total(so)
    refunds = sum(r.amount_p for r in so.refunds)
    credits = sum(r.fee_credit_p for r in so.refunds)
    net = gross - costs - refunds + credits
    cogs = sum(i.cogs_p - i.returned_cogs_p for i in so.items)
    profit = net - cogs
    return SaleFigures(gross, costs, refunds, credits, net, cogs, profit,
                       round(profit * 100 / cogs, 1) if cogs else None,
                       round(profit * 100 / gross, 1) if gross else None)


def refund(s: Session, so: SalesOrder, amount_p: int, *, returns: dict[int, int] | None = None, restock: bool = True,
           fee_credit_p: int = 0, reason: str = "", on: date | None = None) -> None:
    """Refund money and optionally take items back.

    returns: {sales_order_item_id: quantity}. Returned units put their original cost back into stock
    (restock=True) or are written off as damaged (restock=False, cost becomes a loss)."""
    on = on or date.today()
    if amount_p < 0 or fee_credit_p < 0:
        raise ValueError("Amounts cannot be negative")
    already = sum(r.amount_p for r in so.refunds)
    if already + amount_p > gross_revenue(so):
        raise ValueError("Refunds would exceed what the buyer paid")
    from app.models import Refund
    r = Refund(order_id=so.id, amount_p=amount_p, fee_credit_p=fee_credit_p, reason=reason)
    s.add(r)
    s.flush()
    for line_id, qty in (returns or {}).items():
        line = s.get(SalesOrderItem, line_id)
        if line is None or line.order_id != so.id:
            raise ValueError("Return line does not belong to this sale")
        if qty <= 0 or line.returned_quantity + qty > line.quantity:
            raise ValueError("Cannot return more than was sold")
        remaining_qty = line.quantity - line.returned_quantity
        remaining_cogs = line.cogs_p - line.returned_cogs_p
        back_cost = remaining_cogs if qty == remaining_qty else remaining_cogs * qty // remaining_qty
        line.returned_quantity += qty
        line.returned_cogs_p += back_cost
        item = s.get(InventoryItem, line.inventory_item_id)
        move(s, item, "return", qty, back_cost, "refund", r.id, reason or "Customer return")
        if restock:
            item.status = "returned"
        else:
            # Damaged return: cost goes to a write-off immediately.
            move(s, item, "write_off", -qty, -back_cost, "refund", r.id, "Returned damaged")
            # Keep the loss visible on the sale: the returned units' cost is not recovered.
            line.returned_cogs_p -= back_cost
            if item.quantity_on_hand == 0:
                item.status = "damaged"
    if amount_p:
        ledger.post(s, "refund", -amount_p, f"Refund {so.platform} {so.order_ref}", on, "sales_order", so.id)
    if fee_credit_p:
        ledger.post(s, "fee_credit", fee_credit_p, f"Fee credit {so.platform} {so.order_ref}", on, "sales_order", so.id)
    s.flush()
    s.refresh(so)
    total_refunded = sum(x.amount_p for x in so.refunds)
    so.status = "refunded" if total_refunded >= gross_revenue(so) else "partially_refunded"
    audit(s, "sales_order", so.id, "refund", {"amount_p": amount_p, "returns": returns, "restock": restock})


def cancel(s: Session, so: SalesOrder, reason: str = "Cancelled", on: date | None = None) -> None:
    """Full reversal before dispatch: refund everything, credit all selling costs, restock all items."""
    if so.status == "cancelled":
        raise ValueError("Already cancelled")
    returns = {i.id: i.quantity - i.returned_quantity for i in so.items if i.quantity > i.returned_quantity}
    remaining = gross_revenue(so) - sum(r.amount_p for r in so.refunds)
    refund(s, so, remaining, returns=returns, restock=True, fee_credit_p=selling_costs_total(so), reason=reason, on=on)
    for i in so.items:
        item = s.get(InventoryItem, i.inventory_item_id)
        item.status = "ready_to_list"
    so.status = "cancelled"


def allocate_order_costs(total_p: int, line_values: list[int]) -> list[int]:
    return allocate(total_p, line_values)


COST_FIELDS = ("platform_fees_p", "ad_fees_p", "postage_p", "packaging_p", "other_costs_p")


def adjust_costs(s: Session, so: SalesOrder, on: date | None = None, **new: int) -> int:
    """Correct a sale's selling costs (e.g. estimated fees replaced by actual ones).
    Posts the difference to the ledger rather than rewriting history. Returns the change in pence."""
    if so.status == "cancelled":
        raise ValueError("Cancelled orders cannot be adjusted")
    before = selling_costs_total(so)
    changes = {}
    for k, v in new.items():
        if k not in COST_FIELDS or v is None:
            continue
        if v < 0:
            raise ValueError("Amounts cannot be negative")
        if getattr(so, k) != v:
            changes[k] = (getattr(so, k), v)
            setattr(so, k, v)
    diff = selling_costs_total(so) - before
    if diff:
        ledger.post(s, "selling_cost", -diff, f"Cost correction {so.platform} {so.order_ref}", on or date.today(),
                    "sales_order", so.id)
    if changes:
        audit(s, "sales_order", so.id, "adjust_costs", {k: list(v) for k, v in changes.items()})
    return diff
