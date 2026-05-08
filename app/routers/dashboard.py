"""
API REST para el dashboard web — /api/*
"""
from calendar import monthrange
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.dashboard import MovimientoUpdate, PresupuestoIn

router = APIRouter()


def _norm_sub1(s: str) -> str:
    s = s.strip()
    return s[0].upper() + s[1:] if s else s


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
    body: PresupuestoIn,
    db: AsyncSession = Depends(get_db),
):
    sub1 = _norm_sub1(body.subcategoria1)
    await db.execute(
        text("""
            INSERT INTO monthly_budgets (subcategoria1, limit_amount, updated_at)
            VALUES (:sub1, :amount, NOW())
            ON CONFLICT (subcategoria1)
            DO UPDATE SET limit_amount = :amount, updated_at = NOW()
        """),
        {"sub1": sub1, "amount": body.limit_amount},
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


@router.patch("/movimientos/{tx_id}")
async def update_movimiento(
    tx_id: str,
    body: MovimientoUpdate,
    db: AsyncSession = Depends(get_db),
):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="Sin campos para actualizar")

    if "subcategoria1" in fields:
        fields["subcategoria1"] = _norm_sub1(fields["subcategoria1"])

    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    params = {**fields, "id": tx_id}

    result = await db.execute(
        text(f"""
            UPDATE transactions
            SET {set_clause}, updated_at = NOW()
            WHERE id = :id AND deleted_at IS NULL
            RETURNING id, transaction_date, tipo, amount, currency,
                      categoria, subcategoria1, subcategoria2, subcategoria3,
                      nota, origen, llm_confidence
        """),
        params,
    )
    row = result.first()
    if not row:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Movimiento no encontrado")
    await db.commit()

    return {
        "id": str(row.id),
        "transaction_date": row.transaction_date.isoformat(),
        "tipo": row.tipo,
        "amount": float(row.amount),
        "currency": row.currency,
        "categoria": row.categoria,
        "subcategoria1": row.subcategoria1,
        "subcategoria2": row.subcategoria2,
        "subcategoria3": row.subcategoria3,
        "nota": row.nota,
        "origen": row.origen,
        "llm_confidence": float(row.llm_confidence) if row.llm_confidence else None,
    }


@router.delete("/movimientos/{tx_id}")
async def delete_movimiento(tx_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text("""
            UPDATE transactions
            SET deleted_at = NOW(), updated_at = NOW()
            WHERE id = :id AND deleted_at IS NULL
            RETURNING id
        """),
        {"id": tx_id},
    )
    if not result.first():
        await db.rollback()
        raise HTTPException(status_code=404, detail="Movimiento no encontrado")
    await db.commit()
    return {"ok": True}


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


@router.get("/gastos-fijos")
async def get_gastos_fijos(
    anio: int = Query(default=None),
    mes: int = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now()
    anio = anio or now.year
    mes = mes or now.month

    rows = (await db.execute(
        text("""
            SELECT subcategoria1, subcategoria2, total
            FROM v_gastos_fijos
            WHERE anio = :anio AND mes = :mes
            ORDER BY total DESC
        """),
        {"anio": anio, "mes": mes},
    )).all()

    return [
        {
            "subcategoria1": r.subcategoria1,
            "subcategoria2": r.subcategoria2,
            "total": float(r.total),
        }
        for r in rows
    ]


@router.get("/tendencia")
async def get_tendencia(
    subcategoria1: Optional[str] = None,
    tipo: str = Query(default="gasto", pattern=r"^(ingreso|gasto)$"),
    meses: int = Query(default=12, ge=1, le=36),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now()
    desde_mes = now.month - meses + 1
    desde_anio = now.year
    while desde_mes <= 0:
        desde_mes += 12
        desde_anio -= 1

    if subcategoria1:
        rows = (await db.execute(
            text("""
                SELECT anio, mes, subcategoria1, total
                FROM v_tendencia_subcategoria1
                WHERE tipo = :tipo
                  AND LOWER(subcategoria1) = LOWER(:sub1)
                  AND (anio > :da OR (anio = :da AND mes >= :dm))
                ORDER BY anio, mes
            """),
            {"tipo": tipo, "sub1": subcategoria1, "da": desde_anio, "dm": desde_mes},
        )).all()
    else:
        top = (await db.execute(
            text("""
                SELECT subcategoria1
                FROM v_tendencia_subcategoria1
                WHERE tipo = :tipo
                  AND (anio > :da OR (anio = :da AND mes >= :dm))
                GROUP BY subcategoria1
                ORDER BY SUM(total) DESC
                LIMIT 5
            """),
            {"tipo": tipo, "da": desde_anio, "dm": desde_mes},
        )).all()
        top_subs = [r.subcategoria1 for r in top]
        if not top_subs:
            return {"subcategorias": [], "series": []}
        rows = (await db.execute(
            text("""
                SELECT anio, mes, subcategoria1, total
                FROM v_tendencia_subcategoria1
                WHERE tipo = :tipo
                  AND subcategoria1 = ANY(:subs)
                  AND (anio > :da OR (anio = :da AND mes >= :dm))
                ORDER BY anio, mes
            """),
            {"tipo": tipo, "subs": top_subs, "da": desde_anio, "dm": desde_mes},
        )).all()

    serie = [
        {
            "anio": r.anio,
            "mes": r.mes,
            "subcategoria1": r.subcategoria1,
            "total": float(r.total),
        }
        for r in rows
    ]
    subs_unicas = sorted({r["subcategoria1"] for r in serie})
    return {"subcategorias": subs_unicas, "series": serie}


@router.get("/insights")
async def get_insights(
    anio: int = Query(default=None),
    mes: int = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now()
    anio = anio or now.year
    mes = mes or now.month

    insights: list[dict] = []

    mayor = (await db.execute(
        text("""
            SELECT subcategoria1, SUM(total) AS total
            FROM v_gastos_variables
            WHERE anio = :anio AND mes = :mes
            GROUP BY subcategoria1
            ORDER BY total DESC
            LIMIT 1
        """),
        {"anio": anio, "mes": mes},
    )).first()
    if mayor and mayor.total:
        insights.append({
            "kind": "top_gasto",
            "icon": "🥇",
            "title": "Mayor gasto del mes",
            "text": f"{mayor.subcategoria1}: €{float(mayor.total):,.0f}",
        })

        promedio = (await db.execute(
            text("""
                SELECT AVG(total) AS avg6
                FROM v_tendencia_subcategoria1
                WHERE tipo = 'gasto'
                  AND subcategoria1 = :sub
                  AND (anio < :anio OR (anio = :anio AND mes < :mes))
                  AND (anio * 12 + mes) >= (:anio * 12 + :mes - 6)
            """),
            {"sub": mayor.subcategoria1, "anio": anio, "mes": mes},
        )).first()
        if promedio and promedio.avg6:
            avg = float(promedio.avg6)
            actual = float(mayor.total)
            if avg > 0:
                pct = (actual - avg) / avg * 100
                signo = "más" if pct >= 0 else "menos"
                insights.append({
                    "kind": "vs_promedio",
                    "icon": "📊",
                    "title": f"{mayor.subcategoria1} vs promedio 6m",
                    "text": f"{abs(pct):.0f}% {signo} que tu promedio (€{avg:,.0f})",
                    "trend": "up" if pct >= 0 else "down",
                })

    crecimiento = (await db.execute(
        text("""
            WITH actual AS (
                SELECT subcategoria1, SUM(total) AS total
                FROM v_tendencia_subcategoria1
                WHERE tipo = 'gasto' AND anio = :anio AND mes = :mes
                GROUP BY subcategoria1
            ),
            previo AS (
                SELECT subcategoria1, SUM(total) AS total
                FROM v_tendencia_subcategoria1
                WHERE tipo = 'gasto'
                  AND ((anio = :anio AND mes = :mes - 1)
                       OR (anio = :anio - 1 AND :mes = 1 AND mes = 12))
                GROUP BY subcategoria1
            )
            SELECT a.subcategoria1, a.total AS actual, COALESCE(p.total, 0) AS previo,
                   (a.total - COALESCE(p.total, 0)) AS delta
            FROM actual a LEFT JOIN previo p USING (subcategoria1)
            WHERE COALESCE(p.total, 0) > 0
            ORDER BY delta DESC
            LIMIT 1
        """),
        {"anio": anio, "mes": mes},
    )).first()
    if crecimiento and float(crecimiento.delta) > 0:
        pct = float(crecimiento.delta) / float(crecimiento.previo) * 100
        insights.append({
            "kind": "crecimiento",
            "icon": "📈",
            "title": "Mayor crecimiento mes a mes",
            "text": f"{crecimiento.subcategoria1}: +€{float(crecimiento.delta):,.0f} (+{pct:.0f}%)",
            "trend": "up",
        })

    days_in_month = monthrange(anio, mes)[1]
    today_in_period = now.day if (anio == now.year and mes == now.month) else days_in_month
    if today_in_period > 0 and today_in_period < days_in_month:
        gastado_a_hoy = (await db.execute(
            text("""
                SELECT COALESCE(SUM(amount), 0) AS total
                FROM transactions
                WHERE deleted_at IS NULL
                  AND tipo = 'gasto'
                  AND EXTRACT(year FROM transaction_date) = :anio
                  AND EXTRACT(month FROM transaction_date) = :mes
            """),
            {"anio": anio, "mes": mes},
        )).scalar()
        gastado_a_hoy = float(gastado_a_hoy or 0)
        if gastado_a_hoy > 0:
            proyeccion = gastado_a_hoy / today_in_period * days_in_month
            insights.append({
                "kind": "proyeccion",
                "icon": "🔮",
                "title": "Proyección al cierre del mes",
                "text": f"Al ritmo actual: €{proyeccion:,.0f} ({days_in_month - today_in_period} días restantes)",
            })

    excedidos = (await db.execute(
        text("""
            SELECT COUNT(*) AS n
            FROM monthly_budgets b
            JOIN (
                SELECT subcategoria1, SUM(total) AS total
                FROM v_gastos_variables
                WHERE anio = :anio AND mes = :mes
                GROUP BY subcategoria1
            ) g ON LOWER(g.subcategoria1) = LOWER(b.subcategoria1)
            WHERE g.total > b.limit_amount
        """),
        {"anio": anio, "mes": mes},
    )).scalar()
    if excedidos and int(excedidos) > 0:
        insights.append({
            "kind": "presupuestos",
            "icon": "⚠️",
            "title": "Presupuestos excedidos",
            "text": f"{int(excedidos)} categoría(s) por encima del límite",
            "trend": "up",
        })

    return insights
