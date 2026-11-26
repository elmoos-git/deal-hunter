"""Auction house fee profiles: saved terms flow into auction opportunity costs and max bids."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import AuctionFeeProfile, DealOpportunity
from app.services import deals
from app.services.fees import AuctionFeeSchedule, PremiumTier, auction_cost, schedule_from_dict, schedule_to_dict
from tests.helpers import add_sales, product


def test_schedule_round_trip():
    sch = AuctionFeeSchedule(premium_tiers=[PremiumTier(100000, 2500), PremiumTier(None, 2000)], min_premium_p=300,
                             online_surcharge_bp=300, per_lot_fee_p=150)
    back = schedule_from_dict(schedule_to_dict(sch))
    for hammer in (0, 999, 50000, 150000):
        assert auction_cost(hammer, back).total_p == auction_cost(hammer, sch).total_p


def _setup(s):
    p = product(s, "Fee Test Battleforce")
    add_sales(s, p, [9000, 9200, 9100, 8900, 9300, 9050], condition="sealed")
    s.commit()
    return p


def test_auction_listing_without_profile_warns(s):
    p = _setup(s)
    r = deals.ingest(s, deals.ListingIn(source_key="the_saleroom", title="Fee Test Battleforce", price_minor=3000,
                                        listing_type="auction", condition="sealed", product_id=p.id))
    assert r.opportunity.acquisition_p == 3000
    assert any("fees not included" in w for w in r.opportunity.warnings)


def test_profile_fees_included_and_max_bid(s):
    p = _setup(s)
    sch = AuctionFeeSchedule(premium_tiers=[PremiumTier(None, 2500)], vat_on_premium=True, online_surcharge_bp=300,
                             vat_on_surcharge=True, shipping_p=1500)
    s.add(AuctionFeeProfile(name="Test House", source_key="the_saleroom", schedule=schedule_to_dict(sch)))
    s.commit()
    r = deals.ingest(s, deals.ListingIn(source_key="the_saleroom", title="Fee Test Battleforce", price_minor=3000,
                                        listing_type="auction", condition="sealed", product_id=p.id))
    assert r.opportunity.acquisition_p == auction_cost(3000, sch).total_p == 3000 + 750 + 150 + 90 + 18 + 1500
    pf = deals.profitability(s, r.listing)
    assert pf.max_bid_p is not None and pf.max_bid_p > 3000
    # The max bid really meets the budget rules at the conservative value
    # conservative net resale (after selling costs) = conservative profit + current landed cost
    net_cons = pf.profit_conservative_p + pf.acquisition_p
    assert net_cons - auction_cost(pf.max_bid_p, sch).total_p >= 1000  # default minimum profit £10
    assert net_cons - auction_cost(pf.max_bid_p + 1, sch).total_p < 1000 or \
        (net_cons - auction_cost(pf.max_bid_p + 1, sch).total_p) * 100 < 25 * auction_cost(pf.max_bid_p + 1, sch).total_p
    assert not any("fees not included" in w for w in r.opportunity.warnings)
    # Fixed-price listings from the same source ignore the auction profile
    r2 = deals.ingest(s, deals.ListingIn(source_key="the_saleroom", title="Fee Test Battleforce", price_minor=3000,
                                         listing_type="fixed", condition="sealed", product_id=p.id, external_id="fx"))
    assert r2.opportunity.acquisition_p == 3000


def test_profile_ui_save_load_and_recost(db_url):
    c = TestClient(create_app(db_url))
    from app.db import session_factory
    s = session_factory(db_url)()
    p = _setup(s)
    r = deals.ingest(s, deals.ListingIn(source_key="vectis", title="Fee Test Battleforce", price_minor=2000,
                                        listing_type="auction", condition="sealed", product_id=p.id))
    s.commit()
    oid = r.opportunity.id
    resp = c.post("/analyser/profiles", data={"house": "Vectis (example)", "source_key": "vectis", "t0_rate": "22.5",
                                              "vat_premium": "1", "lot_fee": "1.50", "checked_on": "2026-11-26",
                                              "terms_url": "https://www.example.com/terms"}, follow_redirects=True)
    assert "saved" in resp.text and "22.5%" in resp.text
    fp = s.scalar(select(AuctionFeeProfile))
    assert fp.schedule["premium_tiers"][0]["rate_bp"] == 2250
    s.expire_all()
    assert s.get(DealOpportunity, oid).acquisition_p == 2000 + 450 + 90 + 150  # re-costed on save
    page = c.get(f"/analyser?profile_id={fp.id}&bid=20&resale=90").text
    assert "Total landed cost" in page and "£26.90" in page
    assert "Recommended max bid" in c.get(f"/deals/{oid}").text
    bad = c.post("/analyser/profiles", data={"house": "X", "terms_url": "http://127.0.0.1/x"}, follow_redirects=True)
    assert "not allowed" in bad.text
    s.close()
