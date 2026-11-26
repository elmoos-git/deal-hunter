"""Money helpers. All amounts are integer minor units (pence for GBP)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


class MoneyError(ValueError):
    pass


def to_pence(value: str | int | float | Decimal | None) -> int | None:
    """Parse '£12.50', '12.5', '1,200' into pence. Floats are converted via str to avoid binary error."""
    if value is None:
        return None
    if isinstance(value, int):
        return value * 100
    s = str(value).strip().replace("£", "").replace(",", "").replace("GBP", "").strip()
    if s == "":
        return None
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise MoneyError(f"Not a money amount: {value!r}") from e
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt(p: int | None, dash: str = "—") -> str:
    if p is None:
        return dash
    sign = "-" if p < 0 else ""
    p = abs(p)
    return f"{sign}£{p // 100:,}.{p % 100:02d}"


def pct_of(amount_p: int, rate_bp: int) -> int:
    """rate in basis points (1250 = 12.5%). Rounded half-up to the penny."""
    return int((Decimal(amount_p) * Decimal(rate_bp) / Decimal(10000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ratio_pct(num: int, den: int) -> float | None:
    """Percentage for display. Returns None when the denominator is zero (safe division)."""
    if not den:
        return None
    return round(num * 100 / den, 1)


def convert_minor(amount_minor: int, rate: str | Decimal, from_exp: int = 2) -> int:
    """Convert foreign minor units to pence using a decimal rate (GBP per 1 unit of foreign currency).
    from_exp: minor-unit exponent of the source currency (JPY = 0, PLN/EUR/USD = 2)."""
    major = Decimal(amount_minor) / (Decimal(10) ** from_exp)
    return int((major * Decimal(str(rate)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


CURRENCY_EXP = {"GBP": 2, "EUR": 2, "USD": 2, "PLN": 2, "JPY": 0}


def allocate(total: int, weights: list[int]) -> list[int]:
    """Split `total` pence across weights using the largest-remainder method.
    The parts always sum exactly to total. Zero weights get zero, unless all are zero (then equal split)."""
    n = len(weights)
    if n == 0:
        return []
    if any(w < 0 for w in weights):
        raise MoneyError("Weights must be non-negative")
    wsum = sum(weights)
    if wsum == 0:
        weights = [1] * n
        wsum = n
    raw = [total * w for w in weights]
    parts = [r // wsum for r in raw]
    remainders = sorted(range(n), key=lambda i: (raw[i] % wsum, weights[i]), reverse=True)
    for i in remainders[: total - sum(parts)]:
        parts[i] += 1
    return parts
