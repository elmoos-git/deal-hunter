"""Valuation engine: turns recorded sold/hammer observations into a valuation with an explicit confidence.

Only completed sales (sold, auction_hammer) count as comparables. Asking prices never do.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from statistics import mean, median

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Product, SoldObservation, Valuation
from app.money import CURRENCY_EXP, convert_minor

# Conditions that are interchangeable for valuation purposes.
CONDITION_GROUPS = {
    "sealed": "new", "new_in_box": "new", "new_on_sprue": "nos", "complete_opened": "opened",
    "assembled": "built", "primed": "built", "painted": "painted", "incomplete": "incomplete",
    "damaged": "damaged", "unknown": "unknown",
}
GROUP_LABELS = {"new": "New / sealed", "nos": "New on sprue", "opened": "Complete, opened", "built": "Assembled / primed",
                "painted": "Painted", "incomplete": "Incomplete", "damaged": "Damaged", "unknown": "Unknown"}
COMPLETED_TYPES = ("sold", "auction_hammer")
PROVENANCE_RELIABILITY = {"manual_verified": 1.0, "product_research_manual": 1.0, "ebay_order_report": 1.0,
                          "csv_import": 0.8, "demo": 0.8}
WINDOWS = [30, 90, 180, 365, 0]


def condition_group(condition: str) -> str:
    return CONDITION_GROUPS.get(condition, "unknown")


def obs_dedupe_key(product_id: int, platform: str, observed_on: date, price_minor: int, source_url: str | None,
                   title: str | None) -> str:
    basis = source_url.strip().lower() if source_url else f"{platform}|{observed_on}|{price_minor}|{(title or '').lower()}"
    return f"{product_id}:" + hashlib.sha256(basis.encode()).hexdigest()[:40]


def add_observation(s: Session, *, product_id: int, observation_type: str, observed_on: date, price_minor: int,
                    platform: str, provenance: str, condition: str = "unknown", currency: str = "GBP",
                    fx_rate: str = "1", shipping_p: int | None = None, source_url: str | None = None,
                    title: str | None = None, edition: str | None = None, data_label: str = "verified",
                    is_multi_item_lot: bool = False, offer_accepted_unconfirmed: bool = False) -> SoldObservation | None:
    """Insert an observation. Returns None if it is a duplicate of one already stored."""
    if observation_type not in ("sold", "auction_hammer", "unsold", "asking", "retail"):
        raise ValueError(f"Unknown observation type {observation_type}")
    if data_label not in ("verified", "test"):
        raise ValueError("data_label must be 'verified' or 'test'")
    if price_minor < 0:
        raise ValueError("Price cannot be negative")
    key = obs_dedupe_key(product_id, platform, observed_on, price_minor, source_url, title)
    if s.scalar(select(SoldObservation.id).where(SoldObservation.dedupe_key == key)):
        return None
    price_p = price_minor if currency == "GBP" else convert_minor(price_minor, fx_rate, CURRENCY_EXP.get(currency, 2))
    o = SoldObservation(product_id=product_id, observation_type=observation_type, condition=condition, edition=edition,
                        observed_on=observed_on, price_minor=price_minor, currency=currency, fx_rate=str(fx_rate),
                        price_p=price_p, shipping_p=shipping_p, platform=platform, source_url=source_url, title=title,
                        provenance=provenance, data_label=data_label, is_multi_item_lot=is_multi_item_lot,
                        offer_accepted_unconfirmed=offer_accepted_unconfirmed, dedupe_key=key)
    s.add(o)
    s.flush()
    return o


def _percentile(sorted_vals: list[int], q: float) -> int:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo))


def compute(s: Session, product: Product, condition: str, window_days: int = 90, today: date | None = None,
            persist: bool = True) -> Valuation:
    today = today or date.today()
    group = condition_group(condition)
    since = today - timedelta(days=window_days) if window_days else date.min
    rows = list(s.scalars(select(SoldObservation).where(
        SoldObservation.product_id == product.id, SoldObservation.observed_on >= since,
        SoldObservation.observed_on <= today)))

    explanation: list[str] = []
    excluded: list[tuple[SoldObservation, str]] = []
    comps: list[SoldObservation] = []
    unsold = 0
    for o in rows:
        if o.observation_type == "unsold":
            if condition_group(o.condition) == group:
                unsold += 1
            continue
        if o.observation_type not in COMPLETED_TYPES:
            continue  # asking/retail prices are context, never comparables
        reason = None
        if o.excluded:
            reason = o.exclude_reason or "manually excluded"
        elif condition_group(o.condition) != group:
            reason = f"condition mismatch ({o.condition})"
        elif o.is_multi_item_lot:
            reason = "multi-item lot"
        elif o.offer_accepted_unconfirmed:
            reason = "best offer accepted, final price unconfirmed"
        elif product.edition and o.edition and o.edition.strip().lower() != product.edition.strip().lower():
            reason = f"different edition ({o.edition})"
        if reason:
            excluded.append((o, reason))
        else:
            comps.append(o)

    # Outliers (Tukey fences) only once there is enough data to judge.
    if len(comps) >= 5:
        vals = sorted(o.price_p for o in comps)
        q1, q3 = _percentile(vals, 0.25), _percentile(vals, 0.75)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        kept = []
        for o in comps:
            if o.price_p < lo or o.price_p > hi:
                excluded.append((o, "statistical outlier"))
            else:
                kept.append(o)
        comps = kept

    n = len(comps)
    mismatch_count = sum(1 for _, r in excluded if r.startswith("condition mismatch"))
    other_excluded = len(excluded) - mismatch_count
    explanation.append(f"{n} comparable completed sale(s) in {GROUP_LABELS[group].lower()} condition"
                       f" over {'all history' if not window_days else f'{window_days} days'}.")
    if other_excluded:
        explanation.append(f"{other_excluded} sale(s) excluded: " + ", ".join(sorted({r for _, r in excluded if not r.startswith('condition')})) + ".")
    if any(o.data_label == "test" for o in comps):
        explanation.append("Includes TEST data — not real market evidence.")

    v = Valuation(product_id=product.id, condition_group=group, window_days=window_days, n_comparables=n,
                  n_excluded=len(excluded), confidence="insufficient", confidence_score=0)
    if n:
        vals = sorted(o.price_p for o in comps)
        v.median_p = round(median(vals))
        v.mean_p = round(mean(vals))
        v.low_p, v.high_p = vals[0], vals[-1]
        if n >= 4:
            v.conservative_p, v.optimistic_p = _percentile(vals, 0.25), _percentile(vals, 0.75)
        else:
            v.conservative_p, v.optimistic_p = vals[0], vals[-1]
        v.expected_p = v.median_p
        span_days = window_days or max(30, (today - min(o.observed_on for o in comps)).days)
        v.sales_per_30d = round(n * 30 / span_days, 2)
        if n + unsold and unsold:
            v.sell_through_pct = round(100 * n / (n + unsold), 1)
        if v.sales_per_30d >= 1 and n >= 5:
            v.est_days_to_sell = round(30 / v.sales_per_30d)
        if n >= 6:
            ordered = sorted(comps, key=lambda o: o.observed_on)
            half = n // 2
            old_m, new_m = median([o.price_p for o in ordered[:half]]), median([o.price_p for o in ordered[half:]])
            if old_m:
                v.trend_pct = round((new_m - old_m) * 100 / old_m, 1)

        # --- confidence score (0-100) ---
        count_pts = min(n, 10) * 4  # up to 40
        newest_age = (today - max(o.observed_on for o in comps)).days
        recency_pts = 20 if newest_age <= 30 else 12 if newest_age <= 90 else 5 if newest_age <= 365 else 0
        spread = (v.optimistic_p - v.conservative_p) / v.median_p if v.median_p else 1
        consistency_pts = 20 if spread <= 0.25 else 12 if spread <= 0.5 else 5 if spread <= 1 else 0
        reliability = sum(PROVENANCE_RELIABILITY.get(o.provenance, 0.6) for o in comps) / n
        reliability_pts = round(20 * reliability)
        score = count_pts + recency_pts + consistency_pts + reliability_pts
        if any(o.data_label == "test" for o in comps):
            score = min(score, 60)
        v.confidence_score = score
        if n < 3:
            v.confidence = "insufficient"
            explanation.append("Fewer than 3 comparable sales: insufficient evidence for a reliable value.")
        elif score >= 75 and n >= 8:
            v.confidence = "high"
        elif score >= 50:
            v.confidence = "medium"
        else:
            v.confidence = "low"
        explanation.append(f"Confidence {score}/100 = count {count_pts} + recency {recency_pts} + consistency "
                           f"{consistency_pts} + source reliability {reliability_pts}.")
    else:
        explanation.append("No comparable completed sales recorded: value unknown.")
    v.explanation = explanation

    if persist:
        prev = s.scalar(select(Valuation).where(Valuation.product_id == product.id, Valuation.condition_group == group,
                                                Valuation.window_days == window_days, Valuation.is_current.is_(True)))
        if prev and prev.override_p is not None:
            v.override_p, v.override_reason = prev.override_p, prev.override_reason  # overrides carry forward
        s.execute(update(Valuation).where(Valuation.product_id == product.id, Valuation.condition_group == group,
                                          Valuation.window_days == window_days).values(is_current=False))
        s.add(v)
        s.flush()
    return v


def current(s: Session, product_id: int, condition: str, window_days: int = 90) -> Valuation | None:
    return s.scalar(select(Valuation).where(Valuation.product_id == product_id,
                                            Valuation.condition_group == condition_group(condition),
                                            Valuation.window_days == window_days, Valuation.is_current.is_(True)))


def get_or_compute(s: Session, product: Product, condition: str, window_days: int = 90) -> Valuation:
    return current(s, product.id, condition, window_days) or compute(s, product, condition, window_days)


def set_override(s: Session, valuation: Valuation, override_p: int | None, reason: str) -> None:
    if override_p is not None and not reason.strip():
        raise ValueError("An override needs a reason")
    from app.services.audit import audit
    audit(s, "valuation", valuation.id, "override",
          {"computed_expected_p": valuation.expected_p, "old_override_p": valuation.override_p,
           "new_override_p": override_p, "reason": reason})
    valuation.override_p = override_p
    valuation.override_reason = reason.strip() or None
