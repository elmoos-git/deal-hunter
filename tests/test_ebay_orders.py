"""eBay orders report import: SKU/title matching, idempotency, review queue, estimated fees, cost correction."""
import pytest
from sqlalchemy import func, select

from app.models import ImportReview, InventoryItem, SalesOrder
from app.services import finance, imports, ledger, purchasing, sales
from tests.helpers import TODAY, product

REPORT = """Seller Hub orders report
"Sales Record Number","Order Number","Buyer Username","Item Number","Item Title","Custom Label","Quantity","Sold For","Postage And Packaging","Total Price","Sale Date"
"101","12-34567-00001","buyer1","1111","Space Marine Combat Patrol","{sku}","1","£45.00","£3.99","£48.99","20-Nov-26"
"102","12-34567-00002","buyer2","2222","Kit Beta sealed","","2","£10.00","£2.50","£22.50","21-Nov-26"
"103","12-34567-00003","buyer3","3333","Something we never bought","","1","£5.00","£1.00","£6.00","21-Nov-26"

3 record(s) downloaded
"""


@pytest.fixture
def stock(s):
    ledger.add_capital(s, 40000, TODAY)
    beta = product(s, "Kit Beta")
    purchasing.record_purchase(s, purchasing.PurchaseIn("manual", "P1", TODAY, [
        purchasing.LineIn("SM Combat Patrol", 3000, 1), purchasing.LineIn("Kit Beta", 400, 3, beta.id, "sealed")]))
    s.commit()
    sm = s.scalar(select(InventoryItem).where(InventoryItem.name == "SM Combat Patrol"))
    return sm


def test_orders_import_matches_and_is_idempotent(s, stock):
    content = REPORT.format(sku=stock.sku).encode()
    job = imports.run_import(s, "ebay_orders", "orders.csv", content)
    assert job.rows_created == 2 and job.rows_review == 1 and job.status == "needs_review"
    assert stock.quantity_on_hand == 0
    so = s.scalar(select(SalesOrder).where(SalesOrder.order_ref == "12-34567-00001"))
    assert so.sold_on.isoformat() == "2026-11-20" and so.buyer_shipping_p == 399 and so.platform_fees_p > 0
    assert "ESTIMATES" in so.notes
    beta = s.scalar(select(SalesOrder).where(SalesOrder.order_ref == "12-34567-00002"))
    assert beta.items[0].quantity == 2 and beta.items[0].unit_price_p == 1000  # matched by title, one stock item
    review = s.scalar(select(ImportReview))
    assert "12-34567-00003" in review.issue
    # Same file again: refused; re-export with same orders: skipped as duplicates
    with pytest.raises(imports.DuplicateFile):
        imports.run_import(s, "ebay_orders", "orders.csv", content)
    job2 = imports.run_import(s, "ebay_orders", "orders2.csv", content + b"\n")
    assert job2.rows_created == 0 and job2.rows_duplicate == 2
    assert s.scalar(select(func.count(SalesOrder.id))) == 2
    assert finance.compute(s, TODAY).reconciles


def test_orders_import_reports_missing_columns(s):
    job = imports.run_import(s, "ebay_orders", "x.csv", b"Order Number,Foo\n1,2\n")
    assert job.rows_error and any("price" in m for m in job.summary)
    job = imports.run_import(s, "ebay_orders", "y.csv", b"a,b\n1,2\n")
    assert any("header row" in m for m in job.summary)


def test_insufficient_stock_goes_to_review_not_partial(s, stock):
    content = REPORT.format(sku=stock.sku).replace('"1","£45.00"', '"2","£45.00"').encode()
    imports.run_import(s, "ebay_orders", "o.csv", content)
    assert not s.scalar(select(SalesOrder).where(SalesOrder.order_ref == "12-34567-00001"))
    assert stock.quantity_on_hand == 1


def test_cost_correction_posts_difference(s, stock):
    so = sales.record_sale(s, sales.SaleIn("ebay_uk", "C1", TODAY, [sales.SaleLineIn(stock.id, 1, 5000)],
                                           platform_fees_p=600, postage_p=350))
    cash = ledger.cash_balance(s)
    diff = sales.adjust_costs(s, so, platform_fees_p=655, postage_p=299)
    assert diff == 55 - 51 and ledger.cash_balance(s) == cash - 4
    assert sales.figures(so).selling_costs_p == 655 + 299
    assert finance.compute(s, TODAY).reconciles
    with pytest.raises(ValueError):
        sales.adjust_costs(s, so, postage_p=-1)
