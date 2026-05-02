"""
Endpoints internos del módulo financiero.
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.finance import Transaction
from app.schemas.finance import TransactionOut, TransactionSummaryItem

router = APIRouter()


@router.get("/transactions", response_model=list[TransactionOut])
async def list_transactions(
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    tipo: Optional[str] = None,
    categoria: Optional[str] = None,
    subcategoria1: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[Transaction]:
    stmt = select(Transaction).where(Transaction.deleted_at.is_(None))
    if from_date:
        stmt = stmt.where(Transaction.transaction_date >= from_date)
    if to_date:
        stmt = stmt.where(Transaction.transaction_date <= to_date)
    if tipo:
        stmt = stmt.where(Transaction.tipo == tipo)
    if categoria:
        stmt = stmt.where(Transaction.categoria == categoria)
    if subcategoria1:
        stmt = stmt.where(Transaction.subcategoria1 == subcategoria1)
    stmt = stmt.order_by(Transaction.transaction_date.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/summary", response_model=list[TransactionSummaryItem])
async def monthly_summary(
    year: Optional[int] = None,
    month: Optional[int] = None,
    tipo: Optional[str] = "gasto",
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    now = datetime.now()
    stmt = (
        select(Transaction.subcategoria1, func.sum(Transaction.amount).label("total"))
        .where(
            Transaction.deleted_at.is_(None),
            Transaction.tipo == tipo,
            func.extract("year",  Transaction.transaction_date) == (year  or now.year),
            func.extract("month", Transaction.transaction_date) == (month or now.month),
        )
        .group_by(Transaction.subcategoria1)
        .order_by(func.sum(Transaction.amount).desc())
    )
    result = await db.execute(stmt)
    return [{"subcategoria1": s, "total": float(t)} for s, t in result.all()]
