"""Business metrics. Every figure is derived from stored records; nothing is estimated unless labelled so."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    DealOpportunity, Expense, FinancialTransaction, InventoryItem, InventoryMovement, Product, SalesOrder,
)
from app.services import ledger
from app.services import valuation as val
from app.services.sales import figures

ACTIVE_STATUSES = ("purchased", "awaiting_delivery", "received", "requires_inspection", "ready_to_list", "listed",
                   "reserved", "returned")


@dataclass
class Dashboard:
    cash_p: int
    capital_in_p: int
    withdrawals_p: int
    inventory_cost_p: int
    inventory_est_value_p: int | None
    inventory_valued_cost_p: int
    unvalued_items: int
    potential_profit_p: int | None
    revenue_p: int
    realised_profit_p: int
    overheads_p: int
    write_offs_p: int
    business_profit_p: int
    roi_pct: float | None
    margin_pct: float | None
    units_on_hand: int
    listed_items: int
    avg_holding_days: float | None
    sales_count: int
    open_opportunities: int
    reconciles: bool
    reconciliation_gap_p: int
    monthly: list[dict] = field(default_factory=list)
    by_category: list[dict] = field(default_factory=list)
    by_source: list[dict] = field(default_factory=list)
    ageing: list[dict] = field(default_factory=list)
    capital_series: list[dict] = field(default_factory=list)
    profit_by_category: list[dict] = field(default_factory=list)


def _sum(s: Session, kind: str) -> int:
    return int(s.scalar(select(func.coalesce(func.sum(FinancialTransaction.amount_p), 0))
                        .where(FinancialTransaction.kind == kind)))


def write_offs(s: Session) -> int:
    # Write-offs from refunds are already inside that sale's profit; only count standalone ones.
    return -int(s.scalar(select(func.coalesce(func.sum(InventoryMovement.cost_delta_p), 0)).where(
        InventoryMovement.kind == "write_off", InventoryMovement.ref_type.is_(None))))


def compute(s: Session, today: date | None = None) -> Dashboard:
    today = today or date.today()
    cash = ledger.cash_balance(s)
    capital_in = _sum(s, "capital_in")
    withdrawals = -_sum(s, "withdrawal")
    overheads = int(s.scalar(select(func.coalesce(func.sum(Expense.amount_p), 0))))
    wo = write_offs(s)

    items = list(s.scalars(select(InventoryItem).where(InventoryItem.quantity_on_hand > 0)
                           .options(selectinload(InventoryItem.product).selectinload(Product.category))))
    inv_cost = sum(i.remaining_cost_p for i in items)
    est_value, valued_cost, unvalued = 0, 0, 0
    ageing_buckets = {"0–30d": 0, "31–60d": 0, "61–90d": 0, "91–180d": 0, "181–365d": 0, "365d+": 0}
    cat_value: dict[str, int] = defaultdict(int)
    holding = []
    for i in items:
        unit_val = None
        if i.product_id:
            v = val.current(s, i.product_id, i.condition)
            if v and v.confidence != "insufficient" and v.effective_expected_p:
                unit_val = v.effective_expected_p
        if unit_val is None and i.est_resale_p:
            unit_val = i.est_resale_p
        if unit_val is None:
            unvalued += 1
        else:
            est_value += unit_val * i.quantity_on_hand
            valued_cost += i.remaining_cost_p
        days = (today - i.acquired_on).days
        holding.append(days)
        b = ("0–30d" if days <= 30 else "31–60d" if days <= 60 else "61–90d" if days <= 90 else "91–180d"
             if days <= 180 else "181–365d" if days <= 365 else "365d+")
        ageing_buckets[b] += i.remaining_cost_p
        cat_value[(i.product.category.name if i.product and i.product.category else "Uncategorised")] += i.remaining_cost_p

    sales = list(s.scalars(select(SalesOrder).options(selectinload(SalesOrder.items), selectinload(SalesOrder.refunds))))
    revenue, realised, cogs_total = 0, 0, 0
    monthly: dict[str, dict] = defaultdict(lambda: {"revenue_p": 0, "profit_p": 0, "expenses_p": 0, "purchases_p": 0})
    by_source: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)
    for so in sales:
        f = figures(so)
        revenue += f.gross_p - f.refunds_p
        realised += f.profit_p
        cogs_total += f.cogs_p
        m = so.sold_on.strftime("%Y-%m")
        monthly[m]["revenue_p"] += f.gross_p - f.refunds_p
        monthly[m]["profit_p"] += f.profit_p
        # attribute profit to source/category by line revenue share
        line_vals = [ln.unit_price_p * ln.quantity for ln in so.items]
        total_lines = sum(line_vals) or 1
        for ln, lv in zip(so.items, line_vals):
            share = f.profit_p * lv // total_lines
            by_source[ln.item.source_key or "unknown"] += share
            prod = ln.item.product
            by_category[prod.category.name if prod and prod.category else "Uncategorised"] += share
    for e in s.scalars(select(Expense)):
        monthly[e.spent_on.strftime("%Y-%m")]["expenses_p"] += e.amount_p
    for t in s.scalars(select(FinancialTransaction).where(FinancialTransaction.kind == "purchase")):
        monthly[t.occurred_on.strftime("%Y-%m")]["purchases_p"] += -t.amount_p

    # Cash balance over time, straight from the ledger.
    series = []
    events: dict[date, int] = defaultdict(int)
    for t in s.scalars(select(FinancialTransaction)):
        events[t.occurred_on] += t.amount_p
    running = 0
    for d in sorted(events):
        running += events[d]
        series.append({"date": d.isoformat(), "value_p": running})

    business_profit = realised - overheads - wo
    gap = (cash + inv_cost) - (capital_in - withdrawals + business_profit)
    opps = int(s.scalar(select(func.count(DealOpportunity.id)).where(DealOpportunity.status.in_(("new", "saved", "watching")))))
    return Dashboard(
        cash_p=cash, capital_in_p=capital_in, withdrawals_p=withdrawals, inventory_cost_p=inv_cost,
        inventory_est_value_p=est_value if valued_cost or est_value else None, inventory_valued_cost_p=valued_cost,
        unvalued_items=unvalued, potential_profit_p=(est_value - valued_cost) if valued_cost else None,
        revenue_p=revenue, realised_profit_p=realised, overheads_p=overheads, write_offs_p=wo,
        business_profit_p=business_profit,
        roi_pct=round(realised * 100 / cogs_total, 1) if cogs_total else None,
        margin_pct=round(realised * 100 / revenue, 1) if revenue else None,
        units_on_hand=sum(i.quantity_on_hand for i in items),
        listed_items=sum(1 for i in items if i.status == "listed"),
        avg_holding_days=round(sum(holding) / len(holding), 1) if holding else None,
        sales_count=len(sales), open_opportunities=opps, reconciles=gap == 0, reconciliation_gap_p=gap,
        monthly=[{"month": k, **v} for k, v in sorted(monthly.items())],
        by_category=[{"label": k, "cost_p": v} for k, v in sorted(cat_value.items(), key=lambda x: -x[1])],
        by_source=[{"label": k, "profit_p": v} for k, v in sorted(by_source.items(), key=lambda x: -x[1])],
        ageing=[{"label": k, "cost_p": v} for k, v in ageing_buckets.items()],
        capital_series=series,
        profit_by_category=[{"label": k, "profit_p": v} for k, v in sorted(by_category.items(), key=lambda x: -x[1])],
    )


