"""Fee engines: selling fees (configurable profiles) and auction buyer costs (per-auctioneer schedules).

Every function is pure and works in integer pence. Nothing here is a statement of any platform's real fees:
profiles are user-editable configuration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.money import pct_of


@dataclass
class SellingCosts:
    final_value_fee_p: int
    fixed_fee_p: int
    postage_p: int
    packaging_p: int

    @property
    def total_p(self) -> int:
        return self.final_value_fee_p + self.fixed_fee_p + self.postage_p + self.packaging_p


def selling_costs(sale_price_p: int, profile: dict, postage_p: int, packaging_p: int,
                  buyer_shipping_p: int = 0) -> SellingCosts:
    """eBay-style fee: percentage of total paid by buyer (item + shipping) plus fixed per-order fee."""
    gross = sale_price_p + buyer_shipping_p
    return SellingCosts(final_value_fee_p=pct_of(gross, int(profile.get("final_value_bp", 0))),
                        fixed_fee_p=int(profile.get("per_order_fixed_p", 0)) if gross else 0,
                        postage_p=postage_p, packaging_p=packaging_p)


def net_sale_proceeds(sale_price_p: int, profile: dict, postage_p: int, packaging_p: int) -> int:
    return sale_price_p - selling_costs(sale_price_p, profile, postage_p, packaging_p).total_p


@dataclass
class PremiumTier:
    up_to_p: int | None  # None = no upper bound
    rate_bp: int


@dataclass
class AuctionFeeSchedule:
    """Buyer-side costs of an auction lot. Fill from the auctioneer's own terms for each sale."""
    name: str = "Custom"
    premium_tiers: list[PremiumTier] = field(default_factory=lambda: [PremiumTier(None, 0)])
    premium_marginal: bool = True  # tiers apply marginally (like tax bands) vs whole-hammer at the reached tier
    min_premium_p: int = 0
    vat_on_premium: bool = True
    vat_on_hammer: bool = False
    vat_rate_bp: int = 2000
    online_surcharge_bp: int = 0  # e.g. live-bidding platform fee, % of hammer
    online_surcharge_fixed_p: int = 0
    vat_on_surcharge: bool = True
    per_lot_fee_p: int = 0
    payment_fee_bp: int = 0  # card fee on the invoice total
    shipping_p: int = 0
    collection_p: int = 0  # travel/fuel if collecting
    fx_and_import_p: int = 0
    verified_source: str | None = None  # URL of the auctioneer's terms, if checked


@dataclass
class AuctionCost:
    hammer_p: int
    premium_p: int
    vat_on_premium_p: int
    vat_on_hammer_p: int
    surcharge_p: int
    vat_on_surcharge_p: int
    per_lot_fee_p: int
    payment_fee_p: int
    shipping_p: int
    collection_p: int
    fx_and_import_p: int

    @property
    def total_p(self) -> int:
        return sum(v for k, v in asdict(self).items())

    def lines(self) -> list[tuple[str, int]]:
        labels = {"hammer_p": "Hammer price", "premium_p": "Buyer's premium", "vat_on_premium_p": "VAT on premium",
                  "vat_on_hammer_p": "VAT on hammer", "surcharge_p": "Online bidding surcharge",
                  "vat_on_surcharge_p": "VAT on surcharge", "per_lot_fee_p": "Per-lot fee",
                  "payment_fee_p": "Payment fee", "shipping_p": "Shipping", "collection_p": "Collection costs",
                  "fx_and_import_p": "FX / import costs"}
        return [(labels[k], v) for k, v in asdict(self).items()]


def premium(hammer_p: int, sch: AuctionFeeSchedule) -> int:
    tiers = sch.premium_tiers
    if sch.premium_marginal:
        total, lower = 0, 0
        for t in tiers:
            upper = t.up_to_p if t.up_to_p is not None else max(hammer_p, lower)
            band = max(0, min(hammer_p, upper) - lower)
            total += pct_of(band, t.rate_bp)
            lower = upper
            if hammer_p <= lower:
                break
    else:
        rate = tiers[-1].rate_bp
        for t in tiers:
            if t.up_to_p is None or hammer_p <= t.up_to_p:
                rate = t.rate_bp
                break
        total = pct_of(hammer_p, rate)
    return max(total, sch.min_premium_p) if hammer_p > 0 else 0


def auction_cost(hammer_p: int, sch: AuctionFeeSchedule) -> AuctionCost:
    prem = premium(hammer_p, sch)
    surcharge = (pct_of(hammer_p, sch.online_surcharge_bp) + sch.online_surcharge_fixed_p) if hammer_p else 0
    vat = sch.vat_rate_bp
    pre_payment = (hammer_p + prem + (pct_of(prem, vat) if sch.vat_on_premium else 0)
                   + (pct_of(hammer_p, vat) if sch.vat_on_hammer else 0) + surcharge
                   + (pct_of(surcharge, vat) if sch.vat_on_surcharge else 0) + sch.per_lot_fee_p)
    return AuctionCost(
        hammer_p=hammer_p, premium_p=prem,
        vat_on_premium_p=pct_of(prem, vat) if sch.vat_on_premium else 0,
        vat_on_hammer_p=pct_of(hammer_p, vat) if sch.vat_on_hammer else 0,
        surcharge_p=surcharge, vat_on_surcharge_p=pct_of(surcharge, vat) if sch.vat_on_surcharge else 0,
        per_lot_fee_p=sch.per_lot_fee_p, payment_fee_p=pct_of(pre_payment, sch.payment_fee_bp),
        shipping_p=sch.shipping_p, collection_p=sch.collection_p, fx_and_import_p=sch.fx_and_import_p)


def max_hammer(net_resale_p: int, sch: AuctionFeeSchedule, *, target_profit_p: int = 0,
               target_roi_pct: float | None = None, target_margin_pct: float | None = None,
               gross_resale_p: int | None = None) -> int | None:
    """Largest hammer bid (pence) that still meets every given target. None if even £0 fails.

    profit(h) = net_resale - total_cost(h); ROI = profit / total_cost; margin = profit / gross_resale.
    total_cost is monotonic in h, so a binary search over pence is exact.
    """
    gross = gross_resale_p if gross_resale_p is not None else net_resale_p

    def ok(h: int) -> bool:
        cost = auction_cost(h, sch).total_p
        profit = net_resale_p - cost
        if profit < target_profit_p:
            return False
        if target_roi_pct is not None and (cost <= 0 or profit * 100 < target_roi_pct * cost):
            return False
        if target_margin_pct is not None and (gross <= 0 or profit * 100 < target_margin_pct * gross):
            return False
        return True

    if not ok(0):
        return None
    lo, hi = 0, max(net_resale_p, 1)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def schedule_from_dict(d: dict) -> AuctionFeeSchedule:
    tiers = [PremiumTier(t.get("up_to_p"), int(t["rate_bp"])) for t in d.get("premium_tiers", [])] or [PremiumTier(None, 0)]
    kw = {k: v for k, v in d.items() if k != "premium_tiers" and k in AuctionFeeSchedule.__dataclass_fields__}
    return AuctionFeeSchedule(premium_tiers=tiers, **kw)
