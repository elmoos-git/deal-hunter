from datetime import date, timedelta

from app.services import catalogue
from app.services import valuation as val

TODAY = date(2026, 11, 26)


def product(s, name="Test Battleforce Kit", **kw):
    return catalogue.create_product(s, name=name, category=kw.pop("category", "Warhammer miniatures"), **kw)


def add_sales(s, p, prices, condition="new_in_box", start_days_ago=5, label="test", platform="ebay_uk"):
    for i, price in enumerate(prices):
        val.add_observation(s, product_id=p.id, observation_type="sold", observed_on=TODAY - timedelta(days=start_days_ago + i),
                            price_minor=price, platform=platform, provenance="manual_verified", condition=condition,
                            data_label=label, title=f"{p.name} sale {i}")
