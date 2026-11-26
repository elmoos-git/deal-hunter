"""Import an eBay orders report (Seller Hub → Orders → Download report, CSV) as sales matched to inventory.

eBay's column names vary between report versions, so headers are matched by a list of known aliases and the import
says which required columns it could not find. Assumptions, stated in the import summary:
- An order with no matching stock for every line goes to the review queue; nothing partial is created.
- Lines are matched by Custom Label = our inventory SKU first, then by catalogue title match with exactly one
  candidate stock item.
- The orders report does not contain eBay's fees or your label cost: fees are estimated with the active selling-fee
  profile and postage uses the default from Settings. Both are marked as estimates on the sale.
"""
from __future__ import annotations

import csv
import io
import re
from collections import OrderedDict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ImportJob, InventoryItem, SalesOrder
from app.money import MoneyError, to_pence
from app.services import catalogue, sales
from app.services.audit import all_settings
from app.services.fees import selling_costs

ALIASES = {
    "order_ref": ["order number", "order no", "order id", "sales record number"],
    "date": ["sale date", "paid on date", "order date", "date sold", "paid on"],
    "title": ["item title", "title", "item name"],
    "sku": ["custom label", "custom label (sku)", "sku"],
    "item_number": ["item number", "item id"],
    "quantity": ["quantity", "qty"],
    "price": ["sold for", "item price", "sale price", "price"],
    "postage": ["postage and packaging", "postage & packaging", "shipping and handling", "p&p", "delivery charged"],
    "total": ["total price", "total"],
    "buyer": ["buyer username", "buyer user id", "buyer name"],
}
REQUIRED = ("order_ref", "date", "price")


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").replace("﻿", "").strip().lower())


def read_report(content: bytes) -> tuple[list[dict], dict, list[str]]:
    """Returns (rows keyed by our field names, header map, problems). Skips preamble and footer lines."""
    text = content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if any(a in _norm(ln) for a in ALIASES["order_ref"]) and "," in ln), None)
    if start is None:
        return [], {}, ["Could not find a header row with an order number column. Is this an eBay orders report?"]
    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    header = [_norm(h) for h in next(reader)]
    hmap = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in header:
                hmap[field] = header.index(n)
                break
    problems = [f"Missing column for {f.replace('_', ' ')} (looked for: {', '.join(ALIASES[f])})" for f in REQUIRED
                if f not in hmap]
    rows = []
    for raw in reader:
        if not any(c.strip() for c in raw):
            continue
        row = {f: (raw[i].strip() if i < len(raw) else "") for f, i in hmap.items()}
        rows.append(row)
    return rows, hmap, problems


def _money(v: str) -> int | None:
    v = (v or "").replace("GBP", "").replace("£", "").strip()
    return to_pence(v) if v else None


def _date(v: str) -> date:
    v = v.strip()
    for fmt in ("%d-%b-%y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%b-%d-%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognised date {v!r}")


def _match_item(s: Session, row: dict, qty: int, reserved: dict[int, int]) -> tuple[InventoryItem | None, str]:
    def free(it):
        return it.quantity_on_hand - reserved.get(it.id, 0)
    sku = row.get("sku", "")
    if sku:
        it = s.scalar(select(InventoryItem).where(InventoryItem.sku == sku))
        if it and free(it) >= qty:
            return it, "sku"
        if it:
            return None, f"SKU {sku} has only {free(it)} in stock"
    m = catalogue.confident_match(catalogue.match_title(s, row.get("title", "")))
    if not m:
        return None, f"No stock matches “{row.get('title', '')[:80]}”" + (f" (SKU {sku} unknown)" if sku else "")
    cands = [it for it in s.scalars(select(InventoryItem).where(InventoryItem.product_id == m.product.id,
                                                               InventoryItem.quantity_on_hand > 0)) if free(it) >= qty]
    if len(cands) == 1:
        return cands[0], "title"
    if not cands:
        return None, f"Matched {m.product.name} but no stock on hand"
    return None, f"Matched {m.product.name} but {len(cands)} stock items could fit: set Custom Label to the SKU"


def import_orders(s: Session, job: ImportJob, content: bytes, review) -> None:
    rows, hmap, problems = read_report(content)
    job.rows_total = len(rows)
    if problems:
        job.rows_error = len(rows) or 1
        job.summary.extend(problems)
        return
    orders: "OrderedDict[str, list[tuple[int, dict]]]" = OrderedDict()
    last_ref = None
    for n, r in enumerate(rows, start=2):
        ref = r.get("order_ref") or last_ref
        if not ref:
            continue
        last_ref = ref
        # Summary rows of multi-item orders carry no item price; skip them
        if not (r.get("title") or r.get("sku")) or _money(r.get("price", "")) is None:
            continue
        orders.setdefault(ref, []).append((n, r))
    st = all_settings(s)
    profile = st["selling_profiles"][st["selling_profile"]]
    for ref, lines in orders.items():
        if s.scalar(select(SalesOrder.id).where(SalesOrder.platform == "ebay_uk", SalesOrder.order_ref == ref)):
            job.rows_duplicate += len(lines)
            continue
        reserved: dict[int, int] = {}
        sale_lines, issues = [], []
        try:
            for n, r in lines:
                qty = int(r.get("quantity") or 1)
                price_total = _money(r["price"])
                # eBay's "Sold for" is the unit price on item rows
                item, how = _match_item(s, r, qty, reserved)
                if item is None:
                    issues.append(how)
                    continue
                reserved[item.id] = reserved.get(item.id, 0) + qty
                sale_lines.append(sales.SaleLineIn(item.id, qty, price_total))
            if issues:
                review(job, lines[0][0], f"eBay order {ref}: " + "; ".join(issues) + ". Record it on the Sales page.",
                       {"order_ref": ref, "lines": [r for _, r in lines]})
                continue
            postage_charged = sum(_money(r.get("postage", "")) or 0 for _, r in lines[:1])
            gross = sum(ln.unit_price_p * ln.quantity for ln in sale_lines)
            fees = selling_costs(gross, profile, 0, 0, postage_charged).total_p
            with s.begin_nested():
                sales.record_sale(s, sales.SaleIn(
                    platform="ebay_uk", order_ref=ref, sold_on=_date(lines[0][1]["date"]), lines=sale_lines,
                    buyer_shipping_p=postage_charged, platform_fees_p=fees, postage_p=st["default_postage_p"],
                    packaging_p=st["default_packaging_p"],
                    notes=f"Imported from eBay orders report ({job.filename}). Fees and postage cost are ESTIMATES: "
                          "correct them from your eBay payments report."))
            job.rows_created += len(lines)
        except (ValueError, MoneyError, KeyError) as e:
            job.rows_error += len(lines)
            job.summary.append(f"Order {ref}: {e}")
    if job.rows_created:
        job.summary.append("Fees and postage costs on imported sales are estimates; edit them if they differ.")
