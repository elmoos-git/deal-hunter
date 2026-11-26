"""Web layer: every page renders, XSS is escaped, and the full Phase 1 workflow runs end to end over HTTP."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_factory
from app.main import create_app
from app.models import DealOpportunity, InventoryItem, Product, SalesOrder


@pytest.fixture
def client(db_url):
    return TestClient(create_app(db_url))


PAGES = ["/", "/deals", "/watchlist", "/products", "/market", "/analyser", "/split", "/inventory", "/inventory/purchase",
         "/sales", "/sales/new", "/finance", "/imports", "/sources", "/settings", "/alerts", "/search?q=x",
         "/api/dashboard", "/api/opportunities", "/healthz", "/inventory/export.csv"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]


def test_15_untrusted_content_is_escaped(client):
    evil = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    r = client.post("/deals/add", data={"title": evil, "price": "10", "source": "manual", "description": evil},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "<script>alert" not in r.text and "onerror=alert(1)>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert "Content-Security-Policy" in r.text  # CSP meta blocks inline scripts as a second layer
    r2 = client.post("/deals/add", data={"title": "x", "price": "1", "url": "http://169.254.169.254/latest"},
                     follow_redirects=True)
    assert "not allowed" in r2.text


def test_csv_export_neutralises_formulas(client, db_url):
    s = session_factory(db_url)()
    from app.services import purchasing
    from tests.helpers import TODAY
    purchasing.record_purchase(s, purchasing.PurchaseIn("manual", "F1", TODAY, [purchasing.LineIn("=HYPERLINK(\"x\")", 100)]))
    s.commit()
    body = client.get("/inventory/export.csv").text
    assert "'=HYPERLINK" in body


def test_end_to_end_workflow(client, db_url):
    """Catalogue → labelled test sales → valuation → opportunity → profitability → purchase → inventory → split →
    sale → realised profit and available capital."""
    c = client
    # Capital
    c.post("/finance/capital", data={"amount": "400", "kind": "capital_in"})
    # Catalogue: two kits and a box containing them
    c.post("/products/new", data={"name": "E2E Kit Alpha", "category": "Warhammer miniatures", "aliases": "alpha kit"})
    c.post("/products/new", data={"name": "E2E Kit Beta", "category": "Warhammer miniatures"})
    r = c.post("/products/new", data={"name": "E2E Battle Box", "category": "Warhammer miniatures", "aliases": "e2e box"})
    s = session_factory(db_url)()
    ids = {p.name: p.id for p in s.scalars(select(Product))}
    box_id = ids["E2E Battle Box"]
    c.post(f"/products/{box_id}/components", data={"comp_id": [ids["E2E Kit Alpha"], ids["E2E Kit Beta"], ""],
                                                     "comp_qty": ["2", "1", "1"], "comp_group": ["", "", ""],
                                                     "comp_sellable": ["0", "1"]})
    # Import clearly labelled TEST sold observations
    rows = ["product,type,condition,date,price,platform,data_label,title"]
    for i, price in enumerate([60, 62, 64, 58, 61, 63, 59, 65]):
        rows.append(f"E2E Battle Box,sold,sealed,2026-11-{10 + i:02d},{price}.00,ebay_uk,test,box sale {i}")
    for i, price in enumerate([20, 21, 22, 19, 23]):
        rows.append(f"E2E Kit Alpha,sold,sealed,2026-11-{10 + i:02d},{price}.00,ebay_uk,test,alpha {i}")
    for i, price in enumerate([30, 31, 29, 32]):
        rows.append(f"E2E Kit Beta,sold,sealed,2026-11-{10 + i:02d},{price}.00,ebay_uk,test,beta {i}")
    r = c.post("/imports", data={"kind": "sold_observations"},
               files={"file": ("sold.csv", "\n".join(rows).encode(), "text/csv")}, follow_redirects=True)
    assert "17 created" in r.text
    page = c.get(f"/products/{box_id}").text
    assert "TEST" in page and "£61.50" in page  # median of 8 test sales
    # Discover an opportunity (manual listing auto-matched by alias)
    r = c.post("/deals/add", data={"title": "e2e box brand new sealed", "price": "35", "shipping": "4",
                                   "condition": "sealed", "source": "ebay_uk",
                                   "url": "https://www.ebay.co.uk/itm/999"}, follow_redirects=True)
    s.expire_all()
    opp = s.scalar(select(DealOpportunity))
    assert opp.product_id == box_id and opp.profit_expected_p > 0 and opp.score > 0
    assert opp.acquisition_p == 3900
    # Purchase it
    r = c.post("/inventory/purchase", data={"source": "ebay_uk", "order_ref": "E2E-1", "shipping": "4",
                                            "opportunity_id": str(opp.id), "l_desc": ["E2E Battle Box"],
                                            "l_product": [str(box_id)], "l_qty": ["1"], "l_price": ["35"],
                                            "l_cond": ["sealed"]}, follow_redirects=True)
    assert "Purchase recorded" in r.text
    s.expire_all()
    box_item = s.scalar(select(InventoryItem).where(InventoryItem.product_id == box_id))
    assert box_item.cost_basis_p == 3900 and s.get(DealOpportunity, opp.id).status == "purchased"
    # Same order again is refused
    r = c.post("/inventory/purchase", data={"source": "ebay_uk", "order_ref": "E2E-1", "l_desc": ["x"], "l_product": [""],
                                            "l_qty": ["1"], "l_price": ["1"], "l_cond": ["sealed"]}, follow_redirects=True)
    assert "already recorded" in r.text
    # Split the box
    r = c.post(f"/inventory/{box_item.id}/split", follow_redirects=True)
    assert "Split into 2" in r.text
    s.expire_all()
    kids = list(s.scalars(select(InventoryItem).where(InventoryItem.parent_item_id == box_item.id)))
    assert sum(k.cost_basis_p for k in kids) == 3900
    alpha = next(k for k in kids if k.product_id == ids["E2E Kit Alpha"])
    assert alpha.quantity_on_hand == 2
    # Sell one Alpha
    r = c.post("/sales/new", data={"platform": "ebay_uk", "order_ref": "E2E-S1", "buyer_shipping": "3.50", "fees": "3.20",
                                   "postage": "3.50", "packaging": "0.40", "l_item": [str(alpha.id)], "l_qty": ["1"],
                                   "l_price": ["21"]}, follow_redirects=True)
    assert "Sale recorded" in r.text
    s.expire_all()
    so = s.scalar(select(SalesOrder))
    from app.services import finance, sales
    f = sales.figures(so)
    alpha_unit_cost = alpha.cost_basis_p // 2
    assert f.cogs_p == alpha_unit_cost
    assert f.profit_p == 2100 + 350 - 320 - 350 - 40 - alpha_unit_cost
    d = finance.compute(s)
    assert d.realised_profit_p == f.profit_p
    assert d.cash_p == 40000 - 3900 + 2450 - 710
    assert d.reconciles
    home = c.get("/").text
    assert re.search(r"Books reconcile", home)
    s.close()
