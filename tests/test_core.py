"""Acceptance tests mapped to spec section 24 (numbers in test names)."""
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.db import session_factory
from app.models import (
    DealOpportunity, FinancialTransaction, InventoryItem, MarketListing, PurchaseOrder, ScanJob, Source,
)
from app.money import allocate
from app.services import catalogue, deals, finance, imports, inventory, ledger, purchasing, sales, sources
from app.services import valuation as val
from app.services.fees import AuctionFeeSchedule, PremiumTier, auction_cost, max_hammer
from app.services.safety import UnsafeURL, safe_external_url
from tests.helpers import TODAY, add_sales, product


def buy(s, ref="ORD-1", price=5000, shipping=400, product_id=None, qty=1, condition="new_in_box"):
    return purchasing.record_purchase(s, purchasing.PurchaseIn(
        source_key="ebay_uk", order_ref=ref, purchased_on=TODAY, shipping_p=shipping,
        lines=[purchasing.LineIn("Item", price, qty, product_id, condition)]))


# 1 -------------------------------------------------------------------------------------------
def test_01_inventory_and_finance_persist_after_restart(db_url):
    s1 = session_factory(db_url)()
    sources.seed_sources(s1)
    ledger.add_capital(s1, 40000, TODAY)
    buy(s1)
    s1.commit()
    s1.close()
    from app import db as dbmod
    for eng in dbmod._engines.values():
        eng.dispose()
    dbmod._engines.clear()
    s2 = session_factory(db_url)()
    assert ledger.cash_balance(s2) == 40000 - 5400
    item = s2.scalar(select(InventoryItem))
    assert item.cost_basis_p == 5400 and item.quantity_on_hand == 1


# 2 -------------------------------------------------------------------------------------------
def test_02_duplicate_purchase_imports_do_not_duplicate_inventory(s):
    csv1 = b"source,order_ref,date,description,quantity,unit_price,shipping\nebay_uk,A100,2026-11-20,Kit,2,10.00,3.00\n"
    imports.run_import(s, "purchases", "p.csv", csv1)
    with pytest.raises(imports.DuplicateFile):
        imports.run_import(s, "purchases", "p.csv", csv1)
    # Same order in a different file (e.g. a re-export) is skipped by order reference.
    csv2 = csv1 + b"ebay_uk,A101,2026-11-21,Other,1,5.00,0\n"
    job = imports.run_import(s, "purchases", "p2.csv", csv2)
    assert job.rows_duplicate == 1 and job.rows_created == 1
    assert s.scalar(select(func.count(InventoryItem.id))) == 2
    assert s.scalar(select(func.count(PurchaseOrder.id))) == 2
    with pytest.raises(purchasing.DuplicatePurchase):
        purchasing.record_purchase(s, purchasing.PurchaseIn("ebay_uk", "A100", TODAY, [purchasing.LineIn("x", 1)]))


# 3 -------------------------------------------------------------------------------------------
def test_03_auction_fees_itemised_correctly():
    sch = AuctionFeeSchedule(premium_tiers=[PremiumTier(50000, 2500), PremiumTier(None, 2000)], premium_marginal=True,
                             min_premium_p=500, vat_on_premium=True, online_surcharge_bp=300, vat_on_surcharge=True,
                             per_lot_fee_p=150, payment_fee_bp=0, shipping_p=1200)
    c = auction_cost(10000, sch)  # £100 hammer
    assert c.premium_p == 2500 and c.vat_on_premium_p == 500
    assert c.surcharge_p == 300 and c.vat_on_surcharge_p == 60
    assert c.total_p == 10000 + 2500 + 500 + 300 + 60 + 150 + 1200
    # Marginal tiers: £600 hammer → 25% of £500 + 20% of £100
    assert auction_cost(60000, sch).premium_p == 12500 + 2000
    # Minimum premium applies on cheap lots
    assert auction_cost(1000, sch).premium_p == 500
    # Whole-hammer tiering
    sch.premium_marginal = False
    assert auction_cost(60000, sch).premium_p == 12000


# 4 -------------------------------------------------------------------------------------------
def test_04_max_bid_respects_target_profit_and_roi():
    sch = AuctionFeeSchedule(premium_tiers=[PremiumTier(None, 2500)], vat_on_premium=True, shipping_p=1000)
    net_resale = 20000
    h = max_hammer(net_resale, sch, target_profit_p=3000)
    assert net_resale - auction_cost(h, sch).total_p >= 3000
    assert net_resale - auction_cost(h + 1, sch).total_p < 3000
    h_roi = max_hammer(net_resale, sch, target_roi_pct=50)
    cost = auction_cost(h_roi, sch).total_p
    assert (net_resale - cost) * 100 >= 50 * cost
    cost_next = auction_cost(h_roi + 1, sch).total_p
    assert (net_resale - cost_next) * 100 < 50 * cost_next
    assert max_hammer(500, sch, target_profit_p=0) is None  # shipping alone kills it


# 5 -------------------------------------------------------------------------------------------
def test_05_products_match_alternative_names(s):
    p = product(s, "Combat Patrol: Space Marines", aliases=["SM Combat Patrol", "Space Marine CP"])
    product(s, "Combat Patrol: Necrons")
    m = catalogue.confident_match(catalogue.match_title(s, "Warhammer 40k SM combat patrol NEW SEALED"))
    assert m and m.product.id == p.id
    m2 = catalogue.confident_match(catalogue.match_title(s, "space marine cp box"))
    assert m2 and m2.product.id == p.id
    # Ambiguous title is not auto-matched
    assert catalogue.confident_match(catalogue.match_title(s, "combat patrol box")) is None
    # Identifier wins
    catalogue.add_identifier(s, p, "EAN", "5011921123456")
    assert catalogue.match_title(s, "Box 5011921123456")[0].method.startswith("identifier")


# 6 -------------------------------------------------------------------------------------------
def test_06_bundle_split_allocates_cost_without_duplication(s):
    a = product(s, "Kit A", rrp_p=3000)
    b = product(s, "Kit B", rrp_p=1000)
    c1 = product(s, "Hero Build One", rrp_p=2000)
    c2 = product(s, "Hero Build Two", rrp_p=2500)
    card = product(s, "Datacards", rrp_p=500)
    box = product(s, "Battleforce X")
    catalogue.set_bundle_components(s, box, [
        {"product_id": a.id, "quantity": 2}, {"product_id": b.id, "quantity": 1},
        {"product_id": c1.id, "quantity": 1, "shared_sprue_group": "hero"},
        {"product_id": c2.id, "quantity": 1, "shared_sprue_group": "hero"},
        {"product_id": card.id, "quantity": 1, "separately_sellable": False}])
    po = buy(s, price=9999, shipping=601, product_id=box.id, qty=2)
    item = s.scalar(select(InventoryItem).where(InventoryItem.order_ref == po.order_ref))
    assert item.cost_basis_p == 9999 * 2 + 601
    total_before = s.scalar(select(func.sum(InventoryItem.remaining_cost_p)))
    children = inventory.split_bundle(s, item)
    total_after = s.scalar(select(func.sum(InventoryItem.remaining_cost_p)))
    assert total_after == total_before  # cost moved, not created
    assert len(children) == 3  # A, B, one shared-sprue hero (card dropped)
    assert sum(ch.cost_basis_p for ch in children) == item.cost_basis_p - item.remaining_cost_p
    hero = [ch for ch in children if "shared sprue" in ch.name][0]
    assert hero.product_id == c2.id  # most valuable alternative build chosen
    assert item.quantity_on_hand == 1
    # Weights: A 2×3000, B 1000, hero 2500 → A gets 6000/9500 of the unit cost
    unit_cost = sum(ch.cost_basis_p for ch in children)
    assert [ch.cost_basis_p for ch in children] == allocate(unit_cost, [6000, 1000, 2500])


def test_06b_split_refuses_without_value_basis(s):
    a = product(s, "Kit No Data")
    box = product(s, "Box No Data")
    catalogue.set_bundle_components(s, box, [{"product_id": a.id, "quantity": 1}])
    buy(s, product_id=box.id)
    item = s.scalar(select(InventoryItem))
    with pytest.raises(ValueError, match="No value basis"):
        inventory.split_bundle(s, item)
    inventory.split_bundle(s, item, manual_weights={a.id: 1})


# 7 -------------------------------------------------------------------------------------------
def test_07_sale_creates_inventory_and_financial_records(s):
    ledger.add_capital(s, 40000, TODAY)
    buy(s, price=3000, shipping=0, qty=3)
    item = s.scalar(select(InventoryItem))
    so = sales.record_sale(s, sales.SaleIn("ebay_uk", "S-1", TODAY, [sales.SaleLineIn(item.id, 2, 2500)],
                                           buyer_shipping_p=400, platform_fees_p=700, postage_p=400, packaging_p=50))
    f = sales.figures(so)
    assert item.quantity_on_hand == 1 and item.remaining_cost_p == 3000
    assert f.cogs_p == 6000 and f.gross_p == 5400 and f.net_proceeds_p == 5400 - 1150
    assert f.profit_p == 4250 - 6000
    assert ledger.cash_balance(s) == 40000 - 9000 + 5400 - 1150
    with pytest.raises(ValueError, match="only 1 on hand"):
        sales.record_sale(s, sales.SaleIn("ebay_uk", "S-2", TODAY, [sales.SaleLineIn(item.id, 2, 2500)]))
    with pytest.raises(sales.DuplicateSale):
        sales.record_sale(s, sales.SaleIn("ebay_uk", "S-1", TODAY, [sales.SaleLineIn(item.id, 1, 2500)]))
    # Same item twice in one order cannot oversell either
    with pytest.raises(ValueError):
        sales.record_sale(s, sales.SaleIn("ebay_uk", "S-3", TODAY, [sales.SaleLineIn(item.id, 1, 1),
                                                                    sales.SaleLineIn(item.id, 1, 1)]))


# 8 -------------------------------------------------------------------------------------------
def test_08_refunds_and_returns_reverse_transactions(s):
    ledger.add_capital(s, 40000, TODAY)
    buy(s, price=2000, shipping=0, qty=2)
    item = s.scalar(select(InventoryItem))
    so = sales.record_sale(s, sales.SaleIn("ebay_uk", "S-9", TODAY, [sales.SaleLineIn(item.id, 2, 3000)],
                                           platform_fees_p=600, postage_p=300))
    cash_after_sale = ledger.cash_balance(s)
    line = so.items[0]
    sales.refund(s, so, 3000, returns={line.id: 1}, restock=True, reason="Not as described")
    assert item.quantity_on_hand == 1 and item.remaining_cost_p == 2000
    assert ledger.cash_balance(s) == cash_after_sale - 3000
    f = sales.figures(so)
    assert f.cogs_p == 2000 and f.profit_p == 6000 - 900 - 3000 - 2000
    assert so.status == "partially_refunded"
    with pytest.raises(ValueError):
        sales.refund(s, so, 4000)  # more than paid
    # Full cancellation of another order restores everything including fees
    so2 = sales.record_sale(s, sales.SaleIn("ebay_uk", "S-10", TODAY, [sales.SaleLineIn(item.id, 1, 3000)],
                                            platform_fees_p=300))
    cash_before = ledger.cash_balance(s) + 300 - 3000
    sales.cancel(s, so2)
    assert so2.status == "cancelled" and item.quantity_on_hand == 1
    assert ledger.cash_balance(s) == cash_before
    assert sales.figures(so2).profit_p == 0
    d = finance.compute(s, TODAY)
    assert d.reconciles, d.reconciliation_gap_p


def test_08b_damaged_return_is_a_loss_not_restock(s):
    ledger.add_capital(s, 10000, TODAY)
    buy(s, price=2000, shipping=0)
    item = s.scalar(select(InventoryItem))
    so = sales.record_sale(s, sales.SaleIn("ebay_uk", "D-1", TODAY, [sales.SaleLineIn(item.id, 1, 3000)]))
    sales.refund(s, so, 3000, returns={so.items[0].id: 1}, restock=False)
    assert item.quantity_on_hand == 0 and item.status == "damaged"
    assert sales.figures(so).profit_p == -2000
    assert finance.compute(s, TODAY).reconciles


# 9 -------------------------------------------------------------------------------------------
class FakeAdapter:
    source_key = "ebay_uk"

    def __init__(self, listings, fail_times=0, permanent=False):
        self.listings, self.fail_times, self.permanent, self.calls = listings, fail_times, permanent, 0

    def fetch(self, params):
        self.calls += 1
        if self.permanent:
            raise RuntimeError("HTTP 403 from source")
        if self.calls <= self.fail_times:
            raise sources.TransientError("timeout")
        yield from self.listings


def _listing(title="Combat Patrol: Space Marines sealed", price=3000, ext="111"):
    return deals.ListingIn(source_key="ebay_uk", title=title, price_minor=price, external_id=ext,
                           url=f"https://www.ebay.co.uk/itm/{ext}", condition="sealed")


def test_09_repeated_scans_do_not_duplicate_opportunities(s):
    product(s, "Combat Patrol: Space Marines")
    sources.run_scan(s, "ebay_uk", adapter=FakeAdapter([_listing(), _listing(ext="222", price=2500)]))
    out = sources.run_scan(s, "ebay_uk", adapter=FakeAdapter([_listing(price=2800), _listing(ext="222", price=2500)]))
    assert out.new == 0 and out.seen == 2
    assert s.scalar(select(func.count(DealOpportunity.id))) == 2
    assert s.scalar(select(func.count(MarketListing.id))) == 2
    listing = s.scalar(select(MarketListing).where(MarketListing.external_id == "111"))
    assert listing.price_p == 2800  # price change tracked as a snapshot
    from app.models import ListingSnapshot
    assert s.scalar(select(func.count(ListingSnapshot.id)).where(ListingSnapshot.listing_id == listing.id)) == 2
    # Same listing on another source is flagged as a possible duplicate
    r = deals.ingest(s, deals.ListingIn(source_key="gumtree", title="Combat Patrol: Space Marines sealed", price_minor=2900))
    assert r.listing.possible_duplicate_of == listing.id


# 10 ------------------------------------------------------------------------------------------
def test_10_missing_evidence_gives_no_confidence(s):
    p = product(s, "Rare Thing")
    v = val.compute(s, p, "sealed", today=TODAY)
    assert v.confidence == "insufficient" and v.expected_p is None and v.confidence_score == 0
    add_sales(s, p, [5000, 5200])
    v = val.compute(s, p, "sealed", today=TODAY)
    assert v.confidence == "insufficient" and v.n_comparables == 2
    # Asking prices and painted sales never count for a sealed valuation
    val.add_observation(s, product_id=p.id, observation_type="asking", observed_on=TODAY, price_minor=9000,
                        platform="ebay_uk", provenance="manual_verified", condition="sealed", title="ask")
    val.add_observation(s, product_id=p.id, observation_type="sold", observed_on=TODAY, price_minor=2000,
                        platform="ebay_uk", provenance="manual_verified", condition="painted", title="painted")
    assert val.compute(s, p, "sealed", today=TODAY).n_comparables == 2
    r = deals.ingest(s, deals.ListingIn(source_key="manual", title="Rare Thing", price_minor=100, condition="sealed",
                                        product_id=p.id))
    assert r.opportunity.score == 0 and r.opportunity.confidence == "insufficient"
    assert r.opportunity.profit_expected_p is None


def test_10b_valuation_excludes_misleading_comps_and_outliers(s):
    p = product(s, "Kit V", edition="2nd edition")
    add_sales(s, p, [4000, 4100, 4200, 4300, 4400, 4500, 4600, 4700, 30000])
    val.add_observation(s, product_id=p.id, observation_type="sold", observed_on=TODAY, price_minor=9000,
                        platform="ebay_uk", provenance="manual_verified", condition="new_in_box",
                        is_multi_item_lot=True, title="lot")
    val.add_observation(s, product_id=p.id, observation_type="sold", observed_on=TODAY, price_minor=1000,
                        platform="ebay_uk", provenance="manual_verified", condition="new_in_box", edition="1st edition",
                        title="old ed")
    v = val.compute(s, p, "sealed", today=TODAY)
    assert v.n_comparables == 8 and v.n_excluded == 3
    assert v.median_p == 4350 and v.high_p == 4700
    assert v.confidence in ("medium", "high")
    assert any("TEST" in e for e in v.explanation)
    # Override keeps the computed value and needs a reason
    with pytest.raises(ValueError):
        val.set_override(s, v, 5000, "")
    val.set_override(s, v, 5000, "Known recent price rise")
    assert v.expected_p == 4350 and v.effective_expected_p == 5000


# 11 ------------------------------------------------------------------------------------------
def test_11_scanner_failures_visible_and_recoverable(s):
    flaky = FakeAdapter([_listing()], fail_times=2)
    out = sources.run_scan(s, "ebay_uk", adapter=flaky)
    assert out.job.status == "succeeded" and out.job.attempts == 3 and out.new == 1
    broken = sources.run_scan(s, "ebay_uk", adapter=FakeAdapter([], permanent=True))
    assert broken.job.status == "failed" and "403" in broken.job.error
    src = s.scalar(select(Source).where(Source.key == "ebay_uk"))
    assert "403" in src.last_error
    always = sources.run_scan(s, "ebay_uk", adapter=FakeAdapter([_listing()], fail_times=99))
    assert always.job.status == "failed" and always.job.attempts == 3
    ok = sources.run_scan(s, "ebay_uk", adapter=FakeAdapter([_listing(ext="333")]))
    assert ok.job.status == "succeeded" and src.last_error is None
    assert s.scalar(select(func.count(ScanJob.id))) == 4  # full job history kept


# 12 ------------------------------------------------------------------------------------------
def test_12_dashboard_reconciles_with_records(s):
    ledger.add_capital(s, 40000, TODAY)
    p = product(s, "Kit D")
    add_sales(s, p, [4000] * 5)
    buy(s, "O1", 2000, 300, p.id, qty=2)
    item = s.scalar(select(InventoryItem))
    sales.record_sale(s, sales.SaleIn("ebay_uk", "S1", TODAY, [sales.SaleLineIn(item.id, 1, 4000)], platform_fees_p=500))
    ledger.record_expense(s, 999, TODAY, "packaging", "Bubble wrap")
    d = finance.compute(s, TODAY)
    ledger_sum = s.scalar(select(func.sum(FinancialTransaction.amount_p)))
    assert d.cash_p == ledger_sum == 40000 - 4300 + 4000 - 500 - 999
    assert d.inventory_cost_p == 2150
    assert d.realised_profit_p == 4000 - 500 - 2150
    assert d.business_profit_p == d.realised_profit_p - 999
    assert d.inventory_est_value_p is None or d.inventory_est_value_p >= 0
    assert d.reconciles


# 13 ------------------------------------------------------------------------------------------
def test_13_demo_data_separated_from_live(tmp_path, monkeypatch):
    from app.services import demo
    monkeypatch.setenv("DEAL_HUNTER_WORKSPACE", "live")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'live.sqlite'}")
    with pytest.raises(RuntimeError, match="demo workspace"):
        demo.seed()
    monkeypatch.setenv("DEAL_HUNTER_WORKSPACE", "demo")
    monkeypatch.setenv("DATABASE_URL_DEMO", f"sqlite:///{tmp_path/'demo.sqlite'}")
    demo.seed()
    assert (tmp_path / "demo.sqlite").exists()
    from app.db import session_factory as sf
    sd = sf(f"sqlite:///{tmp_path/'demo.sqlite'}")()
    from app.models import Product
    assert all(p.name.startswith("[DEMO]") for p in sd.scalars(select(Product)))
    from app.models import SoldObservation
    assert all(o.data_label == "test" for o in sd.scalars(select(SoldObservation)))
    sd.close()
    demo.wipe()
    assert not (tmp_path / "demo.sqlite").exists()
    assert not (tmp_path / "live.sqlite").exists()  # live untouched (never created by demo code)


# 14 ------------------------------------------------------------------------------------------
def test_14_restricted_integrations_identified(s):
    fb = s.scalar(select(Source).where(Source.key == "facebook_marketplace"))
    assert fb.status == "restricted" and not fb.enabled
    out = sources.run_scan(s, "facebook_marketplace")
    assert out.job.status == "failed" and "No adapter" in out.job.error
    working = {x.key for x in s.scalars(select(Source).where(Source.status == "working"))}
    assert working <= {"manual", "csv", "local"}  # nothing claims to work that doesn't
    insights = s.scalar(select(Source).where(Source.key == "ebay_uk")).status_note
    assert "Limited Release" in insights


# 15 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/x", "http://127.0.0.1/", "http://10.0.0.5/a",
                                 "http://169.254.169.254/latest/meta-data", "http://user:pw@example.com/",
                                 "http://example.com:8080/", "gopher://example.com", "http://[::1]/",
                                 "javascript:alert(1)", "http://intranet/"])
def test_15_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURL):
        safe_external_url(url)


def test_15b_safe_urls_accepted_and_images_need_https():
    assert safe_external_url("https://www.ebay.co.uk/itm/123")
    with pytest.raises(UnsafeURL):
        safe_external_url("http://img.example.com/a.jpg", image=True)


def test_15c_listing_with_unsafe_url_is_rejected(s):
    with pytest.raises(UnsafeURL):
        deals.ingest(s, deals.ListingIn(source_key="manual", title="x", price_minor=1, url="http://192.168.1.1/admin"))


# Budget rules --------------------------------------------------------------------------------
def test_budget_warnings(s):
    ledger.add_capital(s, 10000, TODAY)
    p = product(s, "Big Box")
    add_sales(s, p, [40000] * 6, condition="sealed", label="test")
    r = deals.ingest(s, deals.ListingIn(source_key="manual", title="Big Box", price_minor=20000, condition="sealed",
                                        product_id=p.id))
    assert any("Exceeds available cash" in w for w in r.opportunity.warnings)
    assert any("maximum per-deal" in w for w in r.opportunity.warnings)


def test_money_allocate_exact():
    assert sum(allocate(1001, [1, 1, 1])) == 1001
    assert allocate(100, [0, 0]) == [50, 50]
    assert sum(allocate(7, [3, 0, 1])) == 7 and allocate(7, [3, 0, 1])[1] == 0


def test_migrate_creates_missing_data_folder(tmp_path):
    """A fresh download has no data/ folder; startup must create it rather than crash."""
    from app.db import migrate
    target = tmp_path / "missing" / "nested" / "main.sqlite"
    migrate(f"sqlite:///{target}")
    assert target.exists()
