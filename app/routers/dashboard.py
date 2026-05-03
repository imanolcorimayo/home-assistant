"""
API REST para el dashboard web — /api/*
Todos los endpoints son GET excepto POST /api/presupuesto.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

router = APIRouter()


@router.get("/balance")
async def get_balance(
    anio: int = Query(default=None),
    mes: int = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now()
    anio = anio or now.year
    mes = mes or now.month

    row = (await db.execute(
        text("""
            SELECT ingresos, gastos, balance, pct_gasto_sobre_ingreso
            FROM v_balance_mensual
            WHERE anio = :anio AND mes = :mes
        """),
        {"anio": anio, "mes": mes},
    )).first()

    return {
        "anio": anio,
        "mes": mes,
        "ingresos": float(row.ingresos) if row else 0,
        "gastos": float(row.gastos) if row else 0,
        "balance": float(row.balance) if row else 0,
        "pct": float(row.pct_gasto_sobre_ingreso) if row and row.pct_gasto_sobre_ingreso else 0,
    }


@router.get("/presupuestos")
async def get_presupuestos(
    anio: int = Query(default=None),
    mes: int = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now()
    anio = anio or now.year
    mes = mes or now.month

    rows = (await db.execute(
        text("""
            SELECT b.subcategoria1, b.limit_amount,
                   COALESCE(g.total, 0) AS gastado
            FROM monthly_budgets b
            LEFT JOIN (
                SELECT subcategoria1, SUM(total) AS total
                FROM v_gastos_variables
                WHERE anio = :anio AND mes = :mes
                GROUP BY subcategoria1
            ) g ON LOWER(g.subcategoria1) = LOWER(b.subcategoria1)
            ORDER BY (COALESCE(g.total, 0) / b.limit_amount) DESC
        """),
        {"anio": anio, "mes": mes},
    )).all()

    return [
        {
            "subcategoria1": r.subcategoria1,
            "limit": float(r.limit_amount),
            "gastado": float(r.gastado),
            "pct": round(float(r.gastado) / float(r.limit_amount) * 100, 1),
        }
        for r in rows
    ]


@router.post("/presupuesto")
async def set_presupuesto(
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    sub1 = str(body.get("subcategoria1", "")).strip()
    amount = float(body.get("limit_amount", 0))
    if not sub1 or amount <= 0:
        return {"ok": False, "error": "subcategoria1 y limit_amount requeridos"}
    sub1 = sub1[0].upper() + sub1[1:]
    await db.execute(
        text("""
            INSERT INTO monthly_budgets (subcategoria1, limit_amount, updated_at)
            VALUES (:sub1, :amount, NOW())
            ON CONFLICT (subcategoria1)
            DO UPDATE SET limit_amount = :amount, updated_at = NOW()
        """),
        {"sub1": sub1, "amount": amount},
    )
    await db.commit()
    return {"ok": True}


@router.delete("/presupuesto/{subcategoria1}")
async def delete_presupuesto(subcategoria1: str, db: AsyncSession = Depends(get_db)):
    await db.execute(
        text("DELETE FROM monthly_budgets WHERE LOWER(subcategoria1) = LOWER(:sub1)"),
        {"sub1": subcategoria1},
    )
    await db.commit()
    return {"ok": True}


@router.get("/comparativa")
async def get_comparativa(
    anio: int = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    anio = anio or datetime.now().year

    rows = (await db.execute(
        text("""
            SELECT anio, mes, ingresos, gastos
            FROM v_balance_mensual
            WHERE anio IN (:anio, :anio_ant)
            ORDER BY anio, mes
        """),
        {"anio": anio, "anio_ant": anio - 1},
    )).all()

    return [
        {"anio": r.anio, "mes": r.mes, "ingresos": float(r.ingresos), "gastos": float(r.gastos)}
        for r in rows
    ]


@router.get("/movimientos")
async def get_movimientos(
    anio: int = Query(default=None),
    mes: int = Query(default=None),
    tipo: Optional[str] = None,
    categoria: Optional[str] = None,
    subcategoria1: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now()
    anio = anio or now.year
    mes = mes or now.month

    filters = ["deleted_at IS NULL", "EXTRACT(year FROM transaction_date) = :anio",
               "EXTRACT(month FROM transaction_date) = :mes"]
    params: dict = {"anio": anio, "mes": mes, "limit": limit, "offset": offset}

    if tipo:
        filters.append("tipo = :tipo")
        params["tipo"] = tipo
    if categoria:
        filters.append("categoria = :categoria")
        params["categoria"] = categoria
    if subcategoria1:
        filters.append("subcategoria1 = :subcategoria1")
        params["subcategoria1"] = subcategoria1
    if search:
        filters.append("(nota ILIKE :search OR subcategoria1 ILIKE :search OR subcategoria2 ILIKE :search)")
        params["search"] = f"%{search}%"

    where = " AND ".join(filters)
    rows = (await db.execute(
        text(f"""
            SELECT id, transaction_date, tipo, amount, currency,
                   categoria, subcategoria1, subcategoria2, subcategoria3,
                   nota, origen, llm_confidence, created_at
            FROM transactions
            WHERE {where}
            ORDER BY transaction_date DESC, created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )).all()

    total = (await db.execute(
        text(f"SELECT COUNT(*) FROM transactions WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )).scalar()

    return {
        "total": total,
        "items": [
            {
                "id": str(r.id),
                "transaction_date": r.transaction_date.isoformat(),
                "tipo": r.tipo,
                "amount": float(r.amount),
                "currency": r.currency,
                "categoria": r.categoria,
                "subcategoria1": r.subcategoria1,
                "subcategoria2": r.subcategoria2,
                "subcategoria3": r.subcategoria3,
                "nota": r.nota,
                "origen": r.origen,
                "llm_confidence": float(r.llm_confidence) if r.llm_confidence else None,
            }
            for r in rows
        ],
    }


@router.get("/categorias")
async def get_categorias(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        text("""
            SELECT DISTINCT categoria, subcategoria1
            FROM transactions
            WHERE deleted_at IS NULL AND categoria IS NOT NULL AND subcategoria1 IS NOT NULL
            ORDER BY categoria, subcategoria1
        """),
    )).all()

    result: dict = {}
    for r in rows:
        if r.categoria not in result:
            result[r.categoria] = []
        result[r.categoria].append(r.subcategoria1)

    return result


@router.get("/gastos-por-categoria")
async def get_gastos_por_categoria(
    anio: int = Query(default=None),
    mes: int = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now()
    anio = anio or now.year
    mes = mes or now.month

    rows = (await db.execute(
        text("""
            SELECT subcategoria1, SUM(total) AS total
            FROM v_gastos_variables
            WHERE anio = :anio AND mes = :mes
            GROUP BY subcategoria1
            ORDER BY total DESC
        """),
        {"anio": anio, "mes": mes},
    )).all()

    return [{"subcategoria1": r.subcategoria1, "total": float(r.total)} for r in rows]
