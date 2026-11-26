"""Cash ledger and capital records."""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Expense, FinancialTransaction
from app.services.audit import audit


def post(s: Session, kind: str, amount_p: int, description: str, on: date, ref_type: str | None = None,
         ref_id: int | None = None) -> FinancialTransaction:
    t = FinancialTransaction(kind=kind, amount_p=amount_p, description=description[:300], occurred_on=on,
                             ref_type=ref_type, ref_id=ref_id)
    s.add(t)
    s.flush()
    return t


def cash_balance(s: Session) -> int:
    return int(s.scalar(select(func.coalesce(func.sum(FinancialTransaction.amount_p), 0))))


def add_capital(s: Session, amount_p: int, on: date, note: str = "Capital contribution") -> FinancialTransaction:
    if amount_p <= 0:
        raise ValueError("Capital contribution must be positive")
    t = post(s, "capital_in", amount_p, note, on)
    audit(s, "ledger", t.id, "capital_in", {"amount_p": amount_p})
    return t


def withdraw(s: Session, amount_p: int, on: date, note: str = "Owner withdrawal") -> FinancialTransaction:
    if amount_p <= 0:
        raise ValueError("Withdrawal must be positive")
    if amount_p > cash_balance(s):
        raise ValueError("Withdrawal exceeds available cash")
    return post(s, "withdrawal", -amount_p, note, on)


def record_expense(s: Session, amount_p: int, on: date, category: str, description: str) -> Expense:
    if amount_p <= 0:
        raise ValueError("Expense must be positive")
    e = Expense(spent_on=on, category=category, description=description, amount_p=amount_p)
    s.add(e)
    s.flush()
    post(s, "expense", -amount_p, f"Overhead: {description}", on, "expense", e.id)
    return e
