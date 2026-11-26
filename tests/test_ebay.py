"""eBay Browse API adapter, search profiles and scheduler, tested against mocked responses shaped like eBay's docs.
No test calls the real eBay API."""
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.models import DealOpportunity, MarketListing, ScanJob, SearchProfile, Source, utcnow
from app.services import ebay, scanning
from app.services.sources import TransientError, run_scan
from tests.helpers import add_sales, product


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "test-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("EBAY_ENVIRONMENT", "production")
    ebay._TokenCache.token = None


def summary(item_id, title, price="30.00", shipping="3.99", options=("FIXED_PRICE",), bid=None, end=None, cond="1000"):
    it = {"itemId": f"v1|{item_id}|0", "title": title, "price": {"value": price, "currency": "GBP"},
          "itemWebUrl": f"https://www.ebay.co.uk/itm/{item_id}", "image": {"imageUrl": f"https://i.ebayimg.com/{item_id}.jpg"},
          "conditionId": cond, "condition": "New", "buyingOptions": list(options), "seller": {"username": "seller1"},
          "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": shipping, "currency": "GBP"}}],
          "itemLocation": {"country": "GB"}}
    if bid:
        it["currentBidPrice"] = {"value": bid, "currency": "GBP"}
    if end:
        it["itemEndDate"] = end
    return it


class FakeEbay:
    """Mock transport: token endpoint + paged search. Records every request."""

    def __init__(self, pages, token_status=200, search_statuses=None):
        self.pages, self.token_status = pages, token_status
        self.search_statuses = list(search_statuses or [])
        self.requests = []
        self.tokens_issued = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path == "/identity/v1/oauth2/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client",
                                                               "error_description": "client authentication failed"})
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens_issued}", "expires_in": 7200,
                                             "token_type": "Application Access Token"})
        if self.search_statuses:
            code = self.search_statuses.pop(0)
            if code != 200:
                return httpx.Response(code, json={"errors": [{"errorId": 1, "message": "nope"}]})
        page = int(req.url.params.get("page", "0"))
        body = {"total": sum(len(p) for p in self.pages), "itemSummaries": self.pages[page]}
        if page + 1 < len(self.pages):
            body["next"] = f"https://api.ebay.com/buy/browse/v1/item_summary/search?q=x&page={page + 1}"
        return httpx.Response(200, json=body)


def adapter_for(fake):
    return ebay.EbayBrowseAdapter(client=httpx.Client(transport=httpx.MockTransport(fake)), min_interval_s=0)


def test_requests_use_app_token_marketplace_and_filters():
    fake = FakeEbay([[summary(1, "Combat Patrol sealed")]])
    items = list(adapter_for(fake).fetch({"query": "combat patrol", "max_price_p": 5000, "new_only": True,
                                          "listing_types": "FIXED_PRICE,AUCTION"}))
    assert len(items) == 1
    tok, search = fake.requests
    assert tok.headers["Authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in tok.content and b"api_scope" in tok.content
    assert search.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"
    assert search.headers["Authorization"] == "Bearer tok1"
    f = search.url.params["filter"]
    assert "buyingOptions:{FIXED_PRICE|AUCTION}" in f and "price:[..50.00]" in f and "priceCurrency:GBP" in f
    assert "conditions:{NEW}" in f and "deliveryCountry:GB" in f
    assert search.url.params["q"] == "combat patrol"


def test_mapping_auction_fixed_and_conditions():
    fake = FakeEbay([[summary(1, "Box A", price="40.00"),
                      summary(2, "Box B auction", price="50.00", bid="12.50", options=("AUCTION",), end="2026-12-01T18:30:00.000Z"),
                      summary(3, "Box C used", cond="3000", options=("FIXED_PRICE", "BEST_OFFER")),
                      {"itemId": "v1|4|0", "title": "USD item", "price": {"value": "10", "currency": "USD"}}]])
    a, b, c = list(adapter_for(fake).fetch({"query": "box"}))
    assert (a.price_minor, a.shipping_p, a.condition, a.listing_type) == (4000, 399, "new_in_box", "fixed")
    assert a.external_id == "ebay:v1|1|0" and a.url == "https://www.ebay.co.uk/itm/1"
    assert (b.price_minor, b.listing_type, b.ends_at.isoformat()) == (1250, "auction", "2026-12-01T18:30:00")
    assert c.condition == "unknown" and c.listing_type == "offer"  # eBay "Used" is never assumed to be complete/new


def test_paging_exclusions_and_max_results():
    pages = [[summary(i, f"Kit {i}") for i in range(3)], [summary(10, "Kit painted"), summary(11, "Kit 11")]]
    fake = FakeEbay(pages)
    titles = [x.title for x in adapter_for(fake).fetch({"query": "kit", "excluded_keywords": "Painted, proxy"})]
    assert titles == ["Kit 0", "Kit 1", "Kit 2", "Kit 11"]
    fake2 = FakeEbay(pages)
    assert len(list(adapter_for(fake2).fetch({"query": "kit", "max_results": 2}))) == 2
    assert len([r for r in fake2.requests if "search" in r.url.path]) == 1  # stops paging once satisfied


def test_token_reused_and_refreshed_on_401():
    fake = FakeEbay([[summary(1, "A")]], search_statuses=[200, 401, 200])
    ad = adapter_for(fake)
    list(ad.fetch({"query": "a"}))
    list(ad.fetch({"query": "a"}))
    assert fake.tokens_issued == 2  # cached for the 2nd call, refreshed once after the 401


def test_bad_keys_and_missing_keys_are_clear_errors(s, monkeypatch):
    with pytest.raises(ebay.EbayConfigError, match="rejected the keys"):
        list(adapter_for(FakeEbay([[]], token_status=401)).fetch({"query": "a"}))
    monkeypatch.delenv("EBAY_CLIENT_SECRET")
    ebay.register()
    out = run_scan(s, "ebay_uk", {"query": "a"})
    assert out.job.status == "failed" and "EBAY_CLIENT_SECRET" in out.job.error
    assert "test-secret" not in (out.job.error or "")
    assert "EBAY_CLIENT_SECRET" in s.scalar(select(Source).where(Source.key == "ebay_uk")).last_error


def test_rate_limit_and_server_errors_are_transient():
    with pytest.raises(TransientError):
        list(adapter_for(FakeEbay([[]], search_statuses=[429])).fetch({"query": "a"}))
    with pytest.raises(TransientError):
        list(adapter_for(FakeEbay([[]], search_statuses=[503])).fetch({"query": "a"}))


def test_profile_scan_end_to_end_and_repeat_is_idempotent(s):
    p = product(s, "Combat Patrol: Space Marines", aliases=["space marine combat patrol"])
    add_sales(s, p, [6000, 6200, 6100, 5900, 6300, 6050], condition="new_in_box")
    s.commit()
    prof = SearchProfile(name="SM CP", query="space marine combat patrol", listing_types="FIXED_PRICE,AUCTION")
    s.add(prof)
    s.commit()
    page = [[summary(1, "Space Marine Combat Patrol NEW sealed", price="35.00"), summary(2, "Space Marine Combat Patrol",
                                                                                         price="58.00")]]
    out = scanning.run_profile(s, prof, adapter=adapter_for(FakeEbay(page)), backoff_s=0)
    assert out.job.status == "succeeded" and out.new == 2 and out.job.profile_id == prof.id
    opp = s.scalar(select(DealOpportunity).join(MarketListing).where(MarketListing.external_id == "ebay:v1|1|0"))
    assert opp.product_id == p.id and opp.profit_expected_p > 0 and opp.score > 0
    assert opp.listing.provenance == "ebay_browse_api"
    # Second run, one price dropped: no new rows, one price snapshot
    page2 = [[summary(1, "Space Marine Combat Patrol NEW sealed", price="32.00"), summary(2, "Space Marine Combat Patrol",
                                                                                          price="58.00")]]
    out2 = scanning.run_profile(s, prof, adapter=adapter_for(FakeEbay(page2)), backoff_s=0)
    assert out2.new == 0 and out2.seen == 2
    assert s.scalar(select(func.count(DealOpportunity.id))) == 2
    assert opp.listing.price_p == 3200


def test_transient_failure_mid_scan_keeps_earlier_results_and_retries(s):
    prof = SearchProfile(name="x", query="kit")
    s.add(prof)
    s.commit()
    pages = [[summary(1, "Kit 1")], [summary(2, "Kit 2")]]
    fake = FakeEbay(pages, search_statuses=[200, 503, 200, 200])  # page 2 fails once, then the whole fetch retries
    out = scanning.run_profile(s, prof, adapter=adapter_for(fake), backoff_s=0)
    assert out.job.status == "succeeded" and out.job.attempts == 2
    assert s.scalar(select(func.count(MarketListing.id))) == 2  # no duplicates from the retry


def test_scheduler_due_logic_and_interrupted_jobs(s):
    a = SearchProfile(name="a", query="a", frequency_min=60, last_run_at=utcnow() - timedelta(minutes=61))
    b = SearchProfile(name="b", query="b", frequency_min=60, last_run_at=utcnow() - timedelta(minutes=10))
    c = SearchProfile(name="c", query="c", enabled=False)
    d = SearchProfile(name="d", query="d")
    s.add_all([a, b, c, d])
    s.commit()
    assert {x.name for x in scanning.due(s)} == {"a", "d"}
    src = s.scalar(select(Source).where(Source.key == "ebay_uk"))
    s.add(ScanJob(source_id=src.id, status="running", profile_id=a.id))
    s.commit()
    assert scanning.run_profile(s, a) is None  # never two scans of the same profile at once
    assert scanning.recover_interrupted(s) == 1
    assert s.scalar(select(ScanJob)).status == "failed"


def test_scan_cancellation(s):
    from app.services.sources import cancel_scan
    prof = SearchProfile(name="x", query="kit")
    s.add(prof)
    s.commit()

    class CancellingAdapter:
        def fetch(self, params):
            from app.services.deals import ListingIn
            yield ListingIn(source_key="ebay_uk", title="Kit 1", price_minor=100, external_id="c1")
            job = s.scalar(select(ScanJob).order_by(ScanJob.id.desc()))
            cancel_scan(s, job)  # as if the user pressed Cancel
            s.commit()
            yield ListingIn(source_key="ebay_uk", title="Kit 2", price_minor=100, external_id="c2")

    out = scanning.run_profile(s, prof, adapter=CancellingAdapter())
    assert out.job.status == "cancelled" and out.seen == 1


def test_profiles_ui(db_url, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    c = TestClient(create_app(db_url))
    r = c.post("/profiles/new", data={"query": "warhammer battleforce", "excluded": "painted", "max_price": "80",
                                      "listing_types": ["FIXED_PRICE", "AUCTION"], "frequency_min": "5"},
               follow_redirects=True)
    assert "Search profile saved" in r.text and "eBay keys found" in r.text
    from app.db import session_factory
    sp = session_factory(db_url)().scalar(select(SearchProfile))
    assert sp.max_price_p == 8000 and sp.frequency_min == 15  # minimum interval enforced
    monkeypatch.delenv("EBAY_CLIENT_ID")
    assert "eBay keys missing" in c.get("/sources").text
