from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent, Setting


def audit(s: Session, entity: str, entity_id: int | None, action: str, details: dict | None = None) -> None:
    s.add(AuditEvent(entity=entity, entity_id=entity_id, action=action, details=details))


DEFAULT_SETTINGS: dict = {
    "initial_capital_p": 40000,
    "budget": {
        "max_per_deal_p": 15000,
        "min_cash_reserve_p": 5000,
        "min_roi_pct": 25,
        "min_profit_p": 1000,
        "target_monthly_profit_p": 10000,
        "reinvest_pct": 100,
        "max_category_exposure_pct": 60,
    },
    # Editable assumptions. Verify against eBay's current UK fee pages before relying on them.
    "selling_profile": "ebay_uk_business",
    "selling_profiles": {
        "ebay_uk_business": {"label": "eBay UK business seller (UNVERIFIED default — check current fee table)",
                             "final_value_bp": 1290, "per_order_fixed_p": 30, "verified": False},
        "ebay_uk_private": {"label": "eBay UK private seller (comparison only; not for trading stock)",
                            "final_value_bp": 0, "per_order_fixed_p": 0, "verified": False},
    },
    "default_postage_p": 350,
    "default_packaging_p": 50,
    "vat_registered": False,
    "ageing_thresholds_days": [30, 60, 90, 180, 365],
    "scoring": {
        "weights": {"profit": 25, "roi": 20, "confidence": 20, "velocity": 20, "capital": 15},
        "profit_full_marks_p": 4000,
        "roi_full_marks_pct": 100,
        "velocity_full_marks_per_30d": 4,
        "penalties": {"auction": 5, "incomplete_risk": 15, "condition_unknown": 8, "no_match": 0},
        "thresholds": {"exceptional": 85, "strong": 70, "investigate": 55, "marginal": 40},
    },
    "valuation_window_days": 90,
}


def get_setting(s: Session, key: str):
    row = s.get(Setting, key)
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return row.value


def set_setting(s: Session, key: str, value) -> None:
    row = s.get(Setting, key)
    old = row.value if row else None
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
    audit(s, "setting", None, "update", {"key": key, "old": old, "new": value})


def all_settings(s: Session) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for row in s.scalars(select(Setting)):
        out[row.key] = row.value
    return out
