"""Deal pipeline: ingest listing → normalise → match → dedupe → value → cost → profit → score → feed/alert."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, DealOpportunity, InventoryItem, ListingSnapshot, MarketListing, Product, Source, utcnow
from app.money import CURRENCY_EXP, convert_minor, pct_of
from app.services import catalogue, ledger
from app.services import valuation as val
from app.services.audit import all_settings
from app.services.fees import auction_cost, max_hammer, schedule_from_dict, selling_costs
from app.services.safety import clean_text, safe_external_url

INCOMPLETE_HINTS = re.compile(r"\b(incomplete|missing|spares|parts only|bits|unboxed|no box|damaged|broken|"
                              r"partially|part built|some built|job ?lot|joblot|bundle|collection|lot of)\b", re.I)


@dataclass
class ListingIn:
    source_key: str
    title: str
    price_minor: int
    currency: str = "GBP"
    fx_rate: str = "1"
    shipping_p: int = 0
    external_id: str | None = None
    url: str | None = None
    description: str | None = None
    image_url: str | None = None
    condition: str = "unknown"
    listing_type: str = "fixed"
    quantity: int = 1
    seller: str | None = None
    location: str | None = None
    ends_at: datetime | None = None
    product_id: int | None = None  # user-confirmed match
    provenance: str = "manual"


@dataclass
class IngestResult:
    listing: MarketListing
    opportunity: DealOpportunity | None
    created: bool
    price_changed: bool = False
    warnings: list[str] = field(default_factory=list)


def external_id_for(data: ListingIn) -> str:
    if data.external_id:
        return data.external_id.strip()
    if data.url:
        u = re.sub(r"[?#].*$", "", data.url.strip().lower()).rstrip("/")
        return "url:" + hashlib.sha256(u.encode()).hexdigest()[:32]
    basis = f"{data.title.strip().lower()}|{data.seller or ''}|{data.price_minor}"
    return "manual:" + hashlib.sha256(basis.encode()).hexdigest()[:32]


def ingest(s: Session, data: ListingIn, today: date | None = None) -> IngestResult:
    """Idempotent: the same (source, external id) updates the existing listing instead of creating another."""
    source = s.scalar(select(Source).where(Source.key == data.source_key))
    if source is None:
        raise ValueError(f"Unknown source {data.source_key}")
    title = clean_text(data.title, 500)
    if not title:
        raise ValueError("Listing title is required")
    if data.price_minor < 0 or data.shipping_p < 0:
        raise ValueError("Prices cannot be negative")
    url = safe_external_url(data.url) if data.url else None
    image = safe_external_url(data.image_url, image=True) if data.image_url else None
    price_p = data.price_minor if data.currency == "GBP" else convert_minor(
        data.price_minor, data.fx_rate, CURRENCY_EXP.get(data.currency, 2))
    ext = external_id_for(data)
    listing = s.scalar(select(MarketListing).where(MarketListing.source_id == source.id, MarketListing.external_id == ext))
    created, changed = False, False
    if listing:
        listing.last_seen_at = utcnow()
        if listing.price_p != price_p or listing.shipping_p != data.shipping_p:
            changed = True
            listing.price_minor, listing.price_p, listing.shipping_p = data.price_minor, price_p, data.shipping_p
            s.add(ListingSnapshot(listing_id=listing.id, price_p=price_p, shipping_p=data.shipping_p, note="price change"))
        if data.product_id:
            listing.product_id, listing.match_score, listing.match_method = data.product_id, 100, "user"
    else:
        created = True
        listing = MarketListing(
            source_id=source.id, external_id=ext, url=url, title=title,
            description=clean_text(data.description, 20000) if data.description else None, image_url=image,
            condition=data.condition, listing_type=data.listing_type, price_minor=data.price_minor,
            currency=data.currency, fx_rate=str(data.fx_rate), fx_date=today or date.today(), price_p=price_p,
            shipping_p=data.shipping_p, quantity=max(1, data.quantity), seller=clean_text(data.seller, 200),
            location=clean_text(data.location, 200), ends_at=data.ends_at, provenance=data.provenance)
        if data.product_id:
            listing.product_id, listing.match_score, listing.match_method = data.product_id, 100, "user"
        else:
            m = catalogue.confident_match(catalogue.match_title(s, title))
            if m:
                listing.product_id, listing.match_score, listing.match_method = m.product.id, m.score, m.method
        s.add(listing)
        s.flush()
        s.add(ListingSnapshot(listing_id=listing.id, price_p=price_p, shipping_p=data.shipping_p, note="first seen"))
        source.listings_found += 1
        listing.possible_duplicate_of = cross_source_duplicate(s, listing)
    s.flush()
    opp = evaluate(s, listing, today=today)
    if created and opp and opp.score >= all_settings(s)["scoring"]["thresholds"]["strong"]:
        raise_alert(s, "new_opportunity", f"Strong opportunity ({opp.score}): {listing.title[:120]}",
                    f"opportunity:{opp.id}", f"/deals/{opp.id}", "high")
    if opp and listing.product_id:
        from app.models import WatchlistEntry
        for w in s.scalars(select(WatchlistEntry).where(WatchlistEntry.product_id == listing.product_id,
                                                        WatchlistEntry.target_price_p.is_not(None))):
            if opp.acquisition_p <= w.target_price_p:
                raise_alert(s, "watch_target", f"Below your target ({opp.acquisition_p/100:.2f} ≤ {w.target_price_p/100:.2f}): "
                            f"{listing.title[:120]}", f"watch:{w.id}:{listing.id}:{opp.acquisition_p}", f"/deals/{opp.id}",
                            "high" if w.priority == "high" else "normal")
    if changed and opp:
        raise_alert(s, "price_change", f"Price changed to £{price_p/100:.2f}: {listing.title[:120]}",
                    f"price:{listing.id}:{price_p}", f"/deals/{opp.id}")
    return IngestResult(listing, opp, created, changed)


def cross_source_duplicate(s: Session, listing: MarketListing) -> int | None:
    """Flag (not merge) a likely duplicate on another source: same matched product, same seller name or same title."""
    norm = catalogue.normalise(listing.title)
    for other in s.scalars(select(MarketListing).where(MarketListing.id != listing.id,
                                                       MarketListing.source_id != listing.source_id)):
        same_title = catalogue.normalise(other.title) == norm
        same_seller_product = (listing.seller and other.seller == listing.seller and listing.product_id
                               and other.product_id == listing.product_id)
        if same_title or same_seller_product:
            return other.id
    return None


def raise_alert(s: Session, kind: str, message: str, key: str, link: str | None = None, priority: str = "normal") -> bool:
    if s.scalar(select(Alert.id).where(Alert.dedupe_key == key)):
        return False
    s.add(Alert(kind=kind, message=message[:500], dedupe_key=key, link=link, priority=priority))
    return True


@dataclass
class Profitability:
    acquisition_p: int
    acquisition_lines: list[tuple[str, int]]
    resale_conservative_p: int | None
    resale_expected_p: int | None
    selling_costs_p: int | None
    profit_conservative_p: int | None
    profit_expected_p: int | None
    roi_pct: float | None
    valuation: object | None
    fee_profile: object | None = None
    max_bid_p: int | None = None  # auctions with a fee profile: highest hammer meeting budget rules at conservative value
    notes: list[str] = field(default_factory=list)


def fee_profile_for(s: Session, source_key: str):
    from app.models import AuctionFeeProfile
    return s.scalar(select(AuctionFeeProfile).where(AuctionFeeProfile.source_key == source_key)
                    .order_by(AuctionFeeProfile.updated_at.desc()))


def profitability(s: Session, listing: MarketListing, settings: dict | None = None) -> Profitability:
    settings = settings or all_settings(s)
    src = listing.source
    notes: list[str] = []
    fp = sch = None
    if listing.listing_type == "auction":
        fp = fee_profile_for(s, src.key)
    if fp:
        sch = schedule_from_dict(fp.schedule)
        cost = auction_cost(listing.price_p, sch)
        lines = [(k, v) for k, v in cost.lines() if v or k == "Hammer price"]
        if listing.shipping_p and not sch.shipping_p:
            lines.append(("Shipping (listing)", listing.shipping_p))
        notes.append(f"Fees from “{fp.name}” profile; cost shown at the current bid, the final hammer will be higher.")
    else:
        lines = [("Price", listing.price_p), ("Shipping", listing.shipping_p)]
        buyer_fee = pct_of(listing.price_p, src.buyer_fee_bp) + (src.buyer_fee_fixed_p if src.buyer_fee_fixed_p else 0)
        if buyer_fee:
            lines.append(("Buyer fee (source profile)", buyer_fee))
        if listing.listing_type == "auction" and src.kind == "auction":
            notes.append("No fee profile for this auction house: buyer's premium and VAT are NOT included. "
                         "Add one in the Auction & Listing Analyser.")
    acq = sum(v for _, v in lines)
    v = None
    if listing.product_id:
        product = s.get(Product, listing.product_id)
        v = val.get_or_compute(s, product, listing.condition, settings.get("valuation_window_days", 90))
    if not v or v.confidence == "insufficient" or v.effective_expected_p is None:
        return Profitability(acq, lines, None, None, None, None, None, None, v, fp, None, notes)
    qty = listing.quantity
    exp_unit = v.effective_expected_p
    cons_unit = min(v.conservative_p or exp_unit, exp_unit)
    profile = settings["selling_profiles"][settings["selling_profile"]]
    post, pack = settings["default_postage_p"], settings["default_packaging_p"]
    cost_exp = qty * selling_costs(exp_unit, profile, post, pack).total_p
    cost_cons = qty * selling_costs(cons_unit, profile, post, pack).total_p
    profit_exp = qty * exp_unit - cost_exp - acq
    profit_cons = qty * cons_unit - cost_cons - acq
    max_bid = None
    if sch is not None:
        budget = settings["budget"]
        extra = listing.shipping_p if listing.shipping_p and not sch.shipping_p else 0
        max_bid = max_hammer(qty * cons_unit - cost_cons - extra, sch, target_profit_p=budget["min_profit_p"],
                             target_roi_pct=budget["min_roi_pct"])
        notes.append("Max bid meets your minimum profit and ROI at the conservative valuation."
                     if max_bid is not None else "No bid meets your minimum profit and ROI at the conservative valuation.")
    return Profitability(acq, lines, qty * cons_unit, qty * exp_unit, cost_exp, profit_cons, profit_exp,
                         round(profit_exp * 100 / acq, 1) if acq else None, v, fp, max_bid, notes)


def classify(score: int, thresholds: dict) -> str:
    if score >= thresholds["exceptional"]:
        return "Exceptional opportunity"
    if score >= thresholds["strong"]:
        return "Strong opportunity"
    if score >= thresholds["investigate"]:
        return "Worth investigating"
    if score >= thresholds["marginal"]:
        return "Marginal opportunity"
    return "Low priority"


def score_listing(s: Session, listing: MarketListing, pf: Profitability, settings: dict) -> tuple[int, list[str], list[str]]:
    cfg = settings["scoring"]
    w = cfg["weights"]
    wsum = sum(w.values()) or 1
    expl: list[str] = []
    warnings: list[str] = []
    v = pf.valuation
    if not listing.product_id:
        expl.append("No confident catalogue match: value unknown. Confirm the product to score this listing.")
        return 0, expl, ["Unmatched product"]
    if pf.profit_expected_p is None:
        expl.append("Insufficient sold-price evidence for this product/condition: score capped at 0. "
                    "Record verified sold prices to value it.")
        return 0, expl, ["No valuation evidence"]

    parts = {}
    parts["profit"] = max(0.0, min(pf.profit_expected_p / cfg["profit_full_marks_p"], 1.0))
    roi = pf.roi_pct or 0
    parts["roi"] = max(0.0, min(roi / cfg["roi_full_marks_pct"], 1.0))
    parts["confidence"] = {"high": 1.0, "medium": 0.65, "low": 0.3}.get(v.confidence, 0.0)
    spm = v.sales_per_30d or 0
    parts["velocity"] = max(0.0, min(spm / cfg["velocity_full_marks_per_30d"], 1.0))
    budget = settings["budget"]
    parts["capital"] = max(0.0, 1 - pf.acquisition_p / max(budget["max_per_deal_p"], 1))
    raw = sum(parts[k] * w.get(k, 0) for k in parts) * 100 / wsum
    labels = {"profit": f"expected profit £{pf.profit_expected_p/100:.2f}", "roi": f"ROI {roi:.0f}%",
              "confidence": f"{v.confidence} valuation confidence ({v.n_comparables} comps)",
              "velocity": f"{spm:.1f} sales / 30 days", "capital": f"ties up £{pf.acquisition_p/100:.2f}"}
    for k in parts:
        expl.append(f"{labels[k]}: {parts[k]*w.get(k,0)*100/wsum:.0f} of {w.get(k,0)*100/wsum:.0f} pts")
    pen = cfg["penalties"]
    penalty = 0
    if listing.listing_type == "auction":
        penalty += pen["auction"]
        expl.append(f"Auction: final price uncertain (−{pen['auction']})")
    if INCOMPLETE_HINTS.search(listing.title or "") or INCOMPLETE_HINTS.search(listing.description or ""):
        penalty += pen["incomplete_risk"]
        expl.append(f"Title/description suggests lot, missing or incomplete parts (−{pen['incomplete_risk']})")
    if listing.condition == "unknown":
        penalty += pen["condition_unknown"]
        expl.append(f"Condition not stated (−{pen['condition_unknown']})")
    owned = s.scalar(select(InventoryItem.id).where(InventoryItem.product_id == listing.product_id,
                                                    InventoryItem.quantity_on_hand > 0))
    if owned:
        penalty += 5
        expl.append("Already holding this product (−5)")
    score = round(max(0, min(100, raw - penalty)))
    if pf.profit_expected_p <= 0:
        score = min(score, 20)
        expl.append("No expected profit: capped at 20")
    if v.confidence == "low":
        score = min(score, 54)
        expl.append("Low valuation confidence: capped at 54 (marginal)")
    if listing.listing_type == "auction" and listing.source.kind == "auction" and pf.fee_profile is None:
        warnings.append("Auction fees not included (no fee profile)")
    if pf.max_bid_p is not None and listing.price_p > pf.max_bid_p:
        warnings.append(f"Current bid is above your max bid (£{pf.max_bid_p/100:.2f})")
    # Purchasing rules
    cash = ledger.cash_balance(s)
    if pf.acquisition_p > cash:
        warnings.append(f"Exceeds available cash (£{cash/100:.2f})")
    elif cash - pf.acquisition_p < budget["min_cash_reserve_p"]:
        warnings.append("Would breach minimum cash reserve")
    if pf.acquisition_p > budget["max_per_deal_p"]:
        warnings.append("Above maximum per-deal commitment")
    if roi < budget["min_roi_pct"]:
        warnings.append(f"Below minimum ROI ({budget['min_roi_pct']}%)")
    if pf.profit_expected_p < budget["min_profit_p"]:
        warnings.append("Below minimum absolute profit")
    if pf.profit_conservative_p is not None and pf.profit_conservative_p < 0:
        warnings.append("Loses money at the conservative valuation")
    return score, expl, warnings


def evaluate(s: Session, listing: MarketListing, today: date | None = None) -> DealOpportunity:
    settings = all_settings(s)
    pf = profitability(s, listing, settings)
    score, expl, warnings = score_listing(s, listing, pf, settings)
    opp = s.scalar(select(DealOpportunity).where(DealOpportunity.listing_id == listing.id))
    if opp is None:
        opp = DealOpportunity(listing_id=listing.id, status="new")
        s.add(opp)
    opp.product_id = listing.product_id
    opp.acquisition_p = pf.acquisition_p
    opp.resale_conservative_p, opp.resale_expected_p = pf.resale_conservative_p, pf.resale_expected_p
    opp.selling_costs_p = pf.selling_costs_p
    opp.profit_conservative_p, opp.profit_expected_p = pf.profit_conservative_p, pf.profit_expected_p
    opp.roi_pct = pf.roi_pct
    opp.score = score
    opp.classification = classify(score, settings["scoring"]["thresholds"])
    opp.confidence = pf.valuation.confidence if pf.valuation else "insufficient"
    opp.explanation = expl
    opp.warnings = warnings
    s.flush()
    return opp


def reidentify(s: Session, opp: DealOpportunity, product_id: int | None) -> DealOpportunity:
    opp.listing.product_id = product_id
    opp.listing.match_score = 100 if product_id else None
    opp.listing.match_method = "user" if product_id else None
    return evaluate(s, opp.listing)


def set_status(s: Session, opp: DealOpportunity, status: str, reason: str | None = None) -> None:
    if status not in ("new", "saved", "watching", "dismissed", "purchased"):
        raise ValueError("Unknown status")
    if status == "dismissed" and not (reason or "").strip():
        raise ValueError("Record why this opportunity was rejected")
    opp.status = status
    opp.reject_reason = reason if status == "dismissed" else None
