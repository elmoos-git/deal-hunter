"""Inventory operations: cost release, bundle splitting, status changes, adjustments."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.models import (
    INVENTORY_STATUSES, BundleComponent, CostAllocation, InventoryItem, InventoryMovement, Product,
)
from app.money import allocate
from app.services import valuation as val
from app.services.audit import audit
from app.services.purchasing import new_sku


def release_cost(item: InventoryItem, qty: int) -> int:
    """Cost of `qty` units leaving stock. Last unit takes the exact remainder so totals always reconcile."""
    if qty <= 0 or qty > item.quantity_on_hand:
        raise ValueError(f"Cannot remove {qty} from {item.sku}: only {item.quantity_on_hand} on hand")
    if qty == item.quantity_on_hand:
        return item.remaining_cost_p
    return item.remaining_cost_p * qty // item.quantity_on_hand


def move(s: Session, item: InventoryItem, kind: str, qty_delta: int, cost_delta: int, ref_type=None, ref_id=None,
         note=None) -> None:
    item.quantity_on_hand += qty_delta
    item.remaining_cost_p += cost_delta
    if item.quantity_on_hand < 0 or item.remaining_cost_p < 0:
        raise ValueError("Inventory would go negative")
    s.add(InventoryMovement(item_id=item.id, kind=kind, quantity_delta=qty_delta, cost_delta_p=cost_delta,
                            ref_type=ref_type, ref_id=ref_id, note=note))


def set_status(s: Session, item: InventoryItem, status: str) -> None:
    if status not in INVENTORY_STATUSES:
        raise ValueError(f"Unknown status {status}")
    audit(s, "inventory_item", item.id, "status", {"from": item.status, "to": status})
    item.status = status


@dataclass
class SplitPart:
    """One sellable unit produced by splitting a bundle."""
    name: str
    product_id: int | None
    quantity: int
    unit_value_p: int | None  # per-unit allocation basis
    basis: str  # expected_value | rrp | manual_weight | missing
    note: str | None = None


def plan_split(s: Session, bundle_item: InventoryItem, manual_weights: dict[int, int] | None = None) -> list[SplitPart]:
    """Work out which sellable units a bundle yields and their value basis.

    - Components marked not separately sellable are dropped (they carry no cost).
    - Components sharing a sprue group become ONE unit (valued at the most valuable alternative), so a shared
      sprue is never counted twice.
    """
    product = s.get(Product, bundle_item.product_id) if bundle_item.product_id else None
    if not product or not product.components:
        raise ValueError("This item is not linked to a catalogue bundle with known contents")
    manual_weights = manual_weights or {}
    parts: list[SplitPart] = []
    groups: dict[str, list[BundleComponent]] = {}
    for c in product.components:
        if not c.separately_sellable:
            continue
        if c.shared_sprue_group:
            groups.setdefault(c.shared_sprue_group, []).append(c)
        else:
            parts.append(_part_for(s, c.component, c.quantity, bundle_item.condition, manual_weights))
    for group, members in groups.items():
        candidates = [_part_for(s, m.component, m.quantity, bundle_item.condition, manual_weights) for m in members]
        best = max(candidates, key=lambda p: p.unit_value_p or -1)
        names = " / ".join(m.component.name for m in members)
        parts.append(SplitPart(name=f"{names} (shared sprue: build one)", product_id=best.product_id,
                               quantity=min(m.quantity for m in members), unit_value_p=best.unit_value_p,
                               basis=best.basis, note=f"Sprue group {group}: counted once"))
    if not parts:
        raise ValueError("Bundle has no separately sellable components")
    return parts


def _part_for(s: Session, comp: Product, qty: int, condition: str, manual: dict[int, int]) -> SplitPart:
    if comp.id in manual:
        return SplitPart(comp.name, comp.id, qty, manual[comp.id], "manual_weight")
    v = val.current(s, comp.id, condition) or val.compute(s, comp, condition)
    if v.effective_expected_p and v.confidence != "insufficient":
        return SplitPart(comp.name, comp.id, qty, v.effective_expected_p, "expected_value")
    if comp.rrp_p:
        return SplitPart(comp.name, comp.id, qty, comp.rrp_p, "rrp", "No reliable sold data; RRP used as weight")
    return SplitPart(comp.name, comp.id, qty, None, "missing")


def split_bundle(s: Session, bundle_item: InventoryItem, manual_weights: dict[int, int] | None = None,
                 on: date | None = None) -> list[InventoryItem]:
    """Split ONE unit of a bundle into component inventory items. Cost moves, it is never duplicated."""
    if bundle_item.quantity_on_hand < 1:
        raise ValueError("Nothing on hand to split")
    parts = plan_split(s, bundle_item, manual_weights)
    missing = [p.name for p in parts if p.unit_value_p is None]
    if missing:
        raise ValueError("No value basis (sold data, RRP or manual weight) for: " + ", ".join(missing))
    unit_cost = release_cost(bundle_item, 1)
    weights = [p.unit_value_p * p.quantity for p in parts]
    shares = allocate(unit_cost, weights)
    move(s, bundle_item, "split_out", -1, -unit_cost, "split", bundle_item.id, "Split into components")
    if bundle_item.quantity_on_hand == 0:
        bundle_item.status = "split"
    children = []
    for part, share in zip(parts, shares):
        child = InventoryItem(sku=new_sku(s), product_id=part.product_id, parent_item_id=bundle_item.id,
                              name=part.name, condition=bundle_item.condition, quantity_initial=part.quantity,
                              quantity_on_hand=0, purchase_cost_p=share, allocated_costs_p=0, cost_basis_p=share,
                              remaining_cost_p=0, status="ready_to_list", acquired_on=bundle_item.acquired_on,
                              source_key=bundle_item.source_key, order_ref=bundle_item.order_ref,
                              listing_url=bundle_item.listing_url, est_resale_p=part.unit_value_p,
                              notes=part.note)
        s.add(child)
        s.flush()
        move(s, child, "split_in", part.quantity, share, "split", bundle_item.id, f"From {bundle_item.sku}")
        s.add(CostAllocation(source_item_id=bundle_item.id, target_item_id=child.id, method=part.basis,
                             basis_value_p=part.unit_value_p * part.quantity, allocated_p=share))
        children.append(child)
    audit(s, "inventory_item", bundle_item.id, "split",
          {"unit_cost_p": unit_cost, "children": [c.sku for c in children], "shares_p": shares})
    s.flush()
    return children


def adjust(s: Session, item: InventoryItem, qty_delta: int, reason: str, write_off: bool = False) -> None:
    """Quantity correction. Removing stock releases its cost (a write-off is a realised loss, not cash)."""
    if not reason.strip():
        raise ValueError("Adjustments need a reason")
    if qty_delta < 0:
        cost = release_cost(item, -qty_delta)
        move(s, item, "write_off" if write_off else "adjust", qty_delta, -cost, note=reason)
        if item.quantity_on_hand == 0 and write_off:
            item.status = "written_off"
    elif qty_delta > 0:
        move(s, item, "adjust", qty_delta, 0, note=reason + " (found stock, zero cost)")
    audit(s, "inventory_item", item.id, "adjust", {"qty_delta": qty_delta, "reason": reason})
