"""
API REST para el dashboard web — /api/*
"""
from calendar import monthrange
from datetime import date as date_cls, datetime, timedelta
from typing import Optional

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.dashboard import (
    CuentaUpdate,
    InstallmentPlanIn,
    MovimientoUpdate,
    PrestamoIn,
    PrestamoUpdate,
    PresupuestoIn,
    SuscripcionIn,
    SuscripcionUpdate,
)
from app.services import installment_generator

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


@router.get("/salud-financiera")
async def get_salud_financiera(db: AsyncSession = Depends(get_db)):
    """Métricas de salud financiera: tasa de ahorro mensual y YTD, runway,
    promedio de gastos.
    """
    today = datetime.now().date()
    anio = today.year

    mes_row = (await db.execute(text("""
        SELECT ingresos, gastos, balance, pct_gasto_sobre_ingreso
        FROM v_balance_mensual
        WHERE anio = :a AND mes = :m
    """), {"a": today.year, "m": today.month})).first()
    mes_ingresos = float(mes_row.ingresos) if mes_row else 0.0
    mes_gastos   = float(mes_row.gastos)   if mes_row else 0.0
    mes_ahorro   = mes_ingresos - mes_gastos
    mes_tasa     = (mes_ahorro / mes_ingresos * 100) if mes_ingresos > 0 else None

    ytd_row = (await db.execute(text("""
        SELECT COALESCE(SUM(ingresos), 0) AS ingresos,
               COALESCE(SUM(gastos), 0)   AS gastos
        FROM v_balance_mensual
        WHERE anio = :a AND mes <= :m
    """), {"a": anio, "m": today.month})).first()
    ytd_ingresos = float(ytd_row.ingresos) if ytd_row else 0.0
    ytd_gastos   = float(ytd_row.gastos)   if ytd_row else 0.0
    ytd_ahorro   = ytd_ingresos - ytd_gastos
    ytd_tasa     = (ytd_ahorro / ytd_ingresos * 100) if ytd_ingresos > 0 else None

    avg_row = (await db.execute(text("""
        SELECT AVG(gastos) AS avg6
        FROM (
            SELECT gastos
            FROM v_balance_mensual
            WHERE (anio < :a OR (anio = :a AND mes < :m))
            ORDER BY anio DESC, mes DESC
            LIMIT 6
        ) AS sub
    """), {"a": today.year, "m": today.month})).first()
    avg_gastos_6m = float(avg_row.avg6) if avg_row and avg_row.avg6 else 0.0

    pat_row = (await db.execute(text("SELECT patrimonio_neto FROM v_patrimonio_neto"))).first()
    patrimonio = float(pat_row.patrimonio_neto) if pat_row and pat_row.patrimonio_neto else 0.0

    runway_meses = (patrimonio / avg_gastos_6m) if avg_gastos_6m > 0 else None

    return {
        "mes": {
            "ingresos": mes_ingresos,
            "gastos":   mes_gastos,
            "ahorro":   mes_ahorro,
            "tasa_ahorro": mes_tasa,
        },
        "ytd": {
            "anio":     anio,
            "ingresos": ytd_ingresos,
            "gastos":   ytd_gastos,
            "ahorro":   ytd_ahorro,
            "tasa_ahorro": ytd_tasa,
        },
        "runway": {
            "patrimonio": patrimonio,
            "avg_gastos_6m": avg_gastos_6m,
            "meses": round(runway_meses, 1) if runway_meses else None,
        },
    }


@router.get("/tasa-ahorro-historico")
async def get_tasa_ahorro_historico(meses: int = Query(default=12, ge=1, le=36),
                                     db: AsyncSession = Depends(get_db)):
    """Evolución mensual de la tasa de ahorro: últimos N meses con datos (solo pasado)."""
    today = datetime.now().date()
    rows = (await db.execute(text("""
        SELECT anio, mes, ingresos, gastos, balance
        FROM v_balance_mensual
        WHERE (anio < :a) OR (anio = :a AND mes <= :m)
        ORDER BY anio DESC, mes DESC
        LIMIT :n
    """), {"a": today.year, "m": today.month, "n": meses})).all()

    series = []
    for r in reversed(rows):
        ing = float(r.ingresos)
        gas = float(r.gastos)
        tasa = ((ing - gas) / ing * 100) if ing > 0 else None
        series.append({
            "anio": int(r.anio), "mes": int(r.mes),
            "ingresos": ing, "gastos": gas, "ahorro": ing - gas,
            "tasa_ahorro": round(tasa, 1) if tasa is not None else None,
        })
    return series


@router.get("/export")
async def export_csv(
    anio: int = Query(default=None),
    mes: int = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Exporta las transactions del mes (o todo el año si mes=None) a CSV."""
    now = datetime.now()
    anio = anio or now.year

    if mes:
        where = "EXTRACT(year FROM transaction_date) = :a AND EXTRACT(month FROM transaction_date) = :m"
        params = {"a": anio, "m": mes}
        filename = f"sovereignbox-{anio}-{mes:02d}.csv"
    else:
        where = "EXTRACT(year FROM transaction_date) = :a"
        params = {"a": anio}
        filename = f"sovereignbox-{anio}.csv"

    rows = (await db.execute(text(f"""
        SELECT t.transaction_date, t.tipo, t.amount, t.currency,
               t.categoria, t.subcategoria1, t.subcategoria2, t.subcategoria3,
               t.nota, t.origen, fm.full_name AS miembro, a.nombre AS cuenta
        FROM transactions t
        JOIN family_members fm ON t.family_member_id = fm.id
        LEFT JOIN accounts a   ON t.account_id = a.id
        WHERE t.deleted_at IS NULL AND {where}
        ORDER BY t.transaction_date, t.created_at
    """), params)).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "fecha", "tipo", "monto", "moneda",
        "categoria", "subcategoria1", "subcategoria2", "subcategoria3",
        "nota", "origen", "miembro", "cuenta",
    ])
    for r in rows:
        writer.writerow([
            r.transaction_date.isoformat(),
            r.tipo or "",
            f"{float(r.amount):.2f}",
            r.currency,
            r.categoria or "", r.subcategoria1 or "", r.subcategoria2 or "", r.subcategoria3 or "",
            r.nota or "", r.origen or "", r.miembro, r.cuenta or "",
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/anomalias")
async def get_anomalias(db: AsyncSession = Depends(get_db)):
    """Transactions del mes actual cuyo monto supere avg + 2σ histórico de la subcategoría."""
    rows = (await db.execute(text("""
        WITH stats AS (
            SELECT subcategoria1,
                   AVG(amount) AS avg_a,
                   STDDEV_POP(amount) AS sd_a
            FROM transactions
            WHERE deleted_at IS NULL AND tipo = 'gasto'
              AND subcategoria1 IS NOT NULL
              AND transaction_date >= CURRENT_DATE - INTERVAL '6 months'
              AND transaction_date <  date_trunc('month', CURRENT_DATE)
            GROUP BY subcategoria1
            HAVING COUNT(*) >= 3
        )
        SELECT t.id, t.transaction_date, t.amount, t.subcategoria1, t.subcategoria2, t.nota,
               s.avg_a, s.sd_a
        FROM transactions t
        JOIN stats s USING (subcategoria1)
        WHERE t.deleted_at IS NULL AND t.tipo = 'gasto'
          AND t.transaction_date >= date_trunc('month', CURRENT_DATE)
          AND t.amount > s.avg_a + 2 * COALESCE(s.sd_a, 0)
          AND t.amount > s.avg_a * 1.5
        ORDER BY t.amount DESC
        LIMIT 10
    """))).all()
    return [
        {
            "id": str(r.id),
            "fecha": r.transaction_date.isoformat(),
            "amount": float(r.amount),
            "subcategoria1": r.subcategoria1,
            "subcategoria2": r.subcategoria2,
            "nota": r.nota,
            "avg_historico": round(float(r.avg_a), 2),
            "sd_historico":  round(float(r.sd_a or 0), 2),
            "exceso_pct":    round((float(r.amount) - float(r.avg_a)) / float(r.avg_a) * 100, 0),
        }
        for r in rows
    ]


@router.get("/recurrencias-detectadas")
async def get_recurrencias_detectadas(db: AsyncSession = Depends(get_db)):
    """Patrones (sub1, sub2, monto aprox) que se repiten ≥3 meses y son
    candidatos razonables a suscripción.

    Filtra:
    - Categorías que ya son recurrentes por naturaleza (Gastos Fijos)
    - Subcategorías muy genéricas (Supermercado: tiene compras múltiples por mes)
    - Montos pequeños (<€10) — ruido estadístico
    """
    rows = (await db.execute(text("""
        WITH meses AS (
            SELECT DISTINCT
                subcategoria1,
                COALESCE(subcategoria2, '') AS subcategoria2,
                ROUND(amount::NUMERIC, 0)   AS monto_aprox,
                EXTRACT(YEAR  FROM transaction_date)::INT AS y,
                EXTRACT(MONTH FROM transaction_date)::INT AS m
            FROM transactions
            WHERE deleted_at IS NULL AND tipo='gasto'
              AND recurring_charge_id IS NULL
              AND loan_id IS NULL
              AND installment_plan_id IS NULL
              AND card_statement_id IS NULL
              AND categoria <> 'Gastos Fijos'
              AND subcategoria1 NOT IN ('Supermercado', 'Vestimenta', 'Bazar')
              AND amount >= 10
              AND transaction_date >= CURRENT_DATE - INTERVAL '6 months'
              AND transaction_date <= CURRENT_DATE
        ),
        agrupado AS (
            SELECT subcategoria1, subcategoria2, monto_aprox,
                   COUNT(DISTINCT (y, m)) AS meses
            FROM meses
            GROUP BY subcategoria1, subcategoria2, monto_aprox
        )
        SELECT subcategoria1, subcategoria2, monto_aprox, meses
        FROM agrupado
        WHERE meses >= 3
        ORDER BY meses DESC, monto_aprox DESC
        LIMIT 10
    """))).all()
    return [
        {
            "subcategoria1": r.subcategoria1,
            "subcategoria2": r.subcategoria2 or None,
            "monto_aprox":   float(r.monto_aprox),
            "meses":         int(r.meses),
        }
        for r in rows
    ]


@router.get("/categoria/detalle")
async def get_categoria_detalle(
    sub1: str = Query(...),
    meses: int = Query(default=12, ge=1, le=36),
    db: AsyncSession = Depends(get_db),
):
    """Detalle de una subcategoría: serie mensual, ticket promedio, últimas transactions."""
    sub1_n = sub1.strip()

    serie = (await db.execute(text("""
        SELECT anio, mes, total
        FROM v_tendencia_subcategoria1
        WHERE LOWER(subcategoria1) = LOWER(:s) AND tipo = 'gasto'
        ORDER BY anio DESC, mes DESC
        LIMIT :n
    """), {"s": sub1_n, "n": meses})).all()

    stats = (await db.execute(text("""
        SELECT COUNT(*) AS n, AVG(amount) AS ticket_promedio,
               SUM(amount) AS total_periodo, MIN(amount) AS min_amount, MAX(amount) AS max_amount
        FROM transactions
        WHERE deleted_at IS NULL AND tipo = 'gasto'
          AND LOWER(subcategoria1) = LOWER(:s)
          AND transaction_date >= CURRENT_DATE - INTERVAL '1 year'
    """), {"s": sub1_n})).first()

    ultimas = (await db.execute(text("""
        SELECT id, transaction_date, amount, subcategoria2, subcategoria3, nota
        FROM transactions
        WHERE deleted_at IS NULL AND tipo = 'gasto'
          AND LOWER(subcategoria1) = LOWER(:s)
        ORDER BY transaction_date DESC
        LIMIT 30
    """), {"s": sub1_n})).all()

    return {
        "subcategoria1": sub1_n,
        "serie": [
            {"anio": int(r.anio), "mes": int(r.mes), "total": float(r.total)}
            for r in reversed(serie)
        ],
        "stats": {
            "transacciones": int(stats.n) if stats and stats.n else 0,
            "ticket_promedio": float(stats.ticket_promedio) if stats and stats.ticket_promedio else 0,
            "total_anio": float(stats.total_periodo) if stats and stats.total_periodo else 0,
            "min": float(stats.min_amount) if stats and stats.min_amount else 0,
            "max": float(stats.max_amount) if stats and stats.max_amount else 0,
        },
        "ultimas": [
            {
                "id": str(u.id), "fecha": u.transaction_date.isoformat(),
                "amount": float(u.amount), "subcategoria2": u.subcategoria2,
                "subcategoria3": u.subcategoria3, "nota": u.nota,
            } for u in ultimas
        ],
    }


@router.get("/distribucion-miembro")
async def get_distribucion_miembro(
    anio: int = Query(default=None),
    mes: int = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Distribución de ingresos y gastos por miembro de la familia para el mes dado."""
    now = datetime.now()
    anio = anio or now.year
    mes  = mes  or now.month

    rows = (await db.execute(text("""
        SELECT miembro,
               COALESCE(SUM(CASE WHEN tipo='ingreso' THEN total ELSE 0 END), 0) AS ingresos,
               COALESCE(SUM(CASE WHEN tipo='gasto'   THEN total ELSE 0 END), 0) AS gastos
        FROM v_resumen_mensual
        WHERE anio = :a AND mes = :m AND miembro IS NOT NULL
        GROUP BY miembro
        ORDER BY miembro
    """), {"a": anio, "m": mes})).all()

    miembros = [
        {
            "miembro": r.miembro,
            "ingresos": float(r.ingresos),
            "gastos":   float(r.gastos),
            "neto":     float(r.ingresos) - float(r.gastos),
        }
        for r in rows
    ]
    total_ingresos = sum(m["ingresos"] for m in miembros)
    total_gastos   = sum(m["gastos"]   for m in miembros)
    for m in miembros:
        m["pct_ingresos"] = (m["ingresos"] / total_ingresos * 100) if total_ingresos > 0 else 0
        m["pct_gastos"]   = (m["gastos"]   / total_gastos   * 100) if total_gastos   > 0 else 0

    return {"anio": anio, "mes": mes, "miembros": miembros, "total_ingresos": total_ingresos, "total_gastos": total_gastos}


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

    prestamos = (await db.execute(
        text("""
            SELECT
                COUNT(*) AS n,
                SUM(GREATEST(cuotas_total - cuotas_generadas, 0) * monto_cuota) AS pendiente,
                SUM(monto_cuota) AS cuota_mensual
            FROM v_loans_status
            WHERE activo AND cuotas_total > cuotas_generadas
        """),
    )).first()
    if prestamos and prestamos.n and int(prestamos.n) > 0:
        pendiente = float(prestamos.pendiente or 0)
        mensual   = float(prestamos.cuota_mensual or 0)
        insights.append({
            "kind": "prestamos",
            "icon": "🏦",
            "title": "Préstamos activos",
            "text": f"{int(prestamos.n)} en curso · €{mensual:,.0f}/mes · €{pendiente:,.0f} pendientes",
        })

    anomalia = (await db.execute(text("""
        WITH stats AS (
            SELECT subcategoria1, AVG(amount) AS avg_a, STDDEV_POP(amount) AS sd_a
            FROM transactions
            WHERE deleted_at IS NULL AND tipo='gasto' AND subcategoria1 IS NOT NULL
              AND transaction_date >= CURRENT_DATE - INTERVAL '6 months'
              AND transaction_date <  date_trunc('month', CURRENT_DATE)
            GROUP BY subcategoria1
            HAVING COUNT(*) >= 3
        )
        SELECT t.amount, t.subcategoria1, s.avg_a
        FROM transactions t
        JOIN stats s USING (subcategoria1)
        WHERE t.deleted_at IS NULL AND t.tipo='gasto'
          AND t.transaction_date >= date_trunc('month', CURRENT_DATE)
          AND t.amount > s.avg_a + 2 * COALESCE(s.sd_a, 0)
          AND t.amount > s.avg_a * 1.5
        ORDER BY t.amount DESC LIMIT 1
    """))).first()
    if anomalia:
        excess = (float(anomalia.amount) - float(anomalia.avg_a)) / float(anomalia.avg_a) * 100
        insights.append({
            "kind": "anomalia",
            "icon": "🔍",
            "title": "Gasto inusual detectado",
            "text": f"{anomalia.subcategoria1}: €{float(anomalia.amount):,.0f} ({excess:+.0f}% vs habitual)",
            "trend": "up",
        })

    rec = (await db.execute(text("""
        WITH meses AS (
            SELECT DISTINCT subcategoria1, COALESCE(subcategoria2, '') AS sub2,
                   ROUND(amount::NUMERIC, 0) AS monto,
                   EXTRACT(YEAR FROM transaction_date)::INT AS y,
                   EXTRACT(MONTH FROM transaction_date)::INT AS m
            FROM transactions
            WHERE deleted_at IS NULL AND tipo='gasto'
              AND recurring_charge_id IS NULL AND loan_id IS NULL
              AND installment_plan_id IS NULL AND card_statement_id IS NULL
              AND categoria <> 'Gastos Fijos'
              AND subcategoria1 NOT IN ('Supermercado', 'Vestimenta', 'Bazar')
              AND amount >= 10
              AND transaction_date >= CURRENT_DATE - INTERVAL '6 months'
              AND transaction_date <= CURRENT_DATE
        )
        SELECT subcategoria1, sub2, monto, COUNT(DISTINCT (y, m)) AS meses
        FROM meses
        GROUP BY subcategoria1, sub2, monto
        HAVING COUNT(DISTINCT (y, m)) >= 3
        ORDER BY meses DESC, monto DESC LIMIT 1
    """))).first()
    if rec:
        nombre = rec.sub2 if rec.sub2 else rec.subcategoria1
        insights.append({
            "kind": "recurrencia",
            "icon": "🔁",
            "title": "Posible suscripción detectada",
            "text": f"{nombre}: €{float(rec.monto):,.0f} repetido {int(rec.meses)} meses",
        })

    return insights


# ─────────────────────────────────────────────────────────────────
# Cuentas y patrimonio
# ─────────────────────────────────────────────────────────────────

@router.get("/cuentas")
async def get_cuentas(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        text("""
            SELECT id, nombre, tipo, family_member_id, miembro, moneda,
                   saldo_inicial, saldo_fecha, saldo_actual, activa,
                   cierre_dia, vencimiento_dia, cuenta_pago_id
            FROM v_saldo_cuentas
            WHERE activa
            ORDER BY
              CASE tipo WHEN 'corriente' THEN 1 WHEN 'efectivo' THEN 2 WHEN 'tarjeta_credito' THEN 3 ELSE 9 END,
              nombre
        """),
    )).all()
    return [
        {
            "id": str(r.id),
            "nombre": r.nombre,
            "tipo": r.tipo,
            "miembro": r.miembro,
            "moneda": r.moneda,
            "saldo_inicial": float(r.saldo_inicial),
            "saldo_fecha": r.saldo_fecha.isoformat() if r.saldo_fecha else None,
            "saldo_actual": float(r.saldo_actual),
            "activa": r.activa,
            "cierre_dia": r.cierre_dia,
            "vencimiento_dia": r.vencimiento_dia,
            "cuenta_pago_id": str(r.cuenta_pago_id) if r.cuenta_pago_id else None,
        }
        for r in rows
    ]


@router.patch("/cuentas/{account_id}")
async def update_cuenta(
    account_id: str,
    payload: CuentaUpdate,
    db: AsyncSession = Depends(get_db),
):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Sin cambios")

    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = account_id
    result = await db.execute(
        text(f"UPDATE accounts SET {sets}, updated_at = now() WHERE id = :id RETURNING id"),
        fields,
    )
    if not result.first():
        raise HTTPException(404, "Cuenta no encontrada")
    await db.commit()
    return {"ok": True}


@router.post("/cuentas/{account_id}/recalcular-saldo")
async def recalcular_saldo(account_id: str, db: AsyncSession = Depends(get_db)):
    """Pone saldo_fecha = MIN(transaction_date) - 1 día.

    Útil cuando cargás transacciones retroactivas que el modelo de saldos
    ignoraría. Después del recálculo, todo el historial computa al saldo
    actual (asumiendo que saldo_inicial era el saldo en el inicio del histórico).
    """
    cuenta = (await db.execute(
        text("SELECT id, saldo_fecha FROM accounts WHERE id = :id"),
        {"id": account_id},
    )).first()
    if not cuenta:
        raise HTTPException(404, "Cuenta no encontrada")

    min_row = (await db.execute(text("""
        SELECT MIN(transaction_date) AS min_date
        FROM transactions
        WHERE account_id = :id AND deleted_at IS NULL
    """), {"id": account_id})).first()

    if not min_row or min_row.min_date is None:
        raise HTTPException(400, "La cuenta no tiene transacciones — nada que recalcular")

    nueva_fecha = min_row.min_date - timedelta(days=1)
    await db.execute(
        text("UPDATE accounts SET saldo_fecha = :f, updated_at = now() WHERE id = :id"),
        {"f": nueva_fecha, "id": account_id},
    )
    await db.commit()
    return {
        "ok": True,
        "saldo_fecha_anterior": cuenta.saldo_fecha.isoformat() if cuenta.saldo_fecha else None,
        "saldo_fecha_nueva":    nueva_fecha.isoformat(),
        "primera_transaccion":  min_row.min_date.isoformat(),
    }


@router.get("/patrimonio")
async def get_patrimonio(db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text("SELECT activos, pasivos, patrimonio_neto FROM v_patrimonio_neto"))).first()
    return {
        "activos":         float(row.activos)         if row and row.activos         is not None else 0,
        "pasivos":         float(row.pasivos)         if row and row.pasivos         is not None else 0,
        "patrimonio_neto": float(row.patrimonio_neto) if row and row.patrimonio_neto is not None else 0,
    }


# ─────────────────────────────────────────────────────────────────
# Préstamos
# ─────────────────────────────────────────────────────────────────

def _row_to_prestamo(r) -> dict:
    cuotas_total     = int(r.cuotas_total)
    cuotas_generadas = int(r.cuotas_generadas)
    # cuotas_restantes = las que aún faltan generar (saldo real pendiente)
    cuotas_restantes = max(cuotas_total - cuotas_generadas, 0)
    monto_cuota = float(r.monto_cuota)
    monto_ult = float(r.monto_ultima_cuota) if r.monto_ultima_cuota is not None else None
    if monto_ult is not None and cuotas_restantes >= 1:
        saldo_pendiente = (cuotas_restantes - 1) * monto_cuota + monto_ult
    else:
        saldo_pendiente = cuotas_restantes * monto_cuota
    return {
        "id": str(r.id),
        "nombre": r.nombre,
        "cuenta_pago_id": str(r.cuenta_pago_id),
        "cuenta_pago_nombre": r.cuenta_pago_nombre,
        "monto_cuota": monto_cuota,
        "monto_ultima_cuota": monto_ult,
        "dia_vencimiento": r.dia_vencimiento,
        "fecha_inicio": r.fecha_inicio.isoformat() if r.fecha_inicio else None,
        "fecha_fin": r.fecha_fin.isoformat() if r.fecha_fin else None,
        "notas": r.notas,
        "activo": r.activo,
        "cuotas_total": cuotas_total,
        "cuotas_restantes": cuotas_restantes,
        "cuotas_generadas": cuotas_generadas,
        "saldo_pendiente": round(saldo_pendiente, 2),
    }


@router.get("/prestamos")
async def get_prestamos(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        text("SELECT * FROM v_loans_status WHERE activo ORDER BY fecha_fin")
    )).all()
    return [_row_to_prestamo(r) for r in rows]


@router.post("/prestamo")
async def create_prestamo(payload: PrestamoIn, db: AsyncSession = Depends(get_db)):
    if payload.fecha_fin < payload.fecha_inicio:
        raise HTTPException(400, "fecha_fin debe ser >= fecha_inicio")
    result = await db.execute(
        text("""
            INSERT INTO loans (nombre, cuenta_pago_id, monto_cuota, dia_vencimiento,
                               fecha_inicio, fecha_fin, monto_ultima_cuota, notas)
            VALUES (:nombre, :cpi, :mc, :dv, :fi, :ff, :muc, :notas)
            RETURNING id
        """),
        {
            "nombre": payload.nombre, "cpi": payload.cuenta_pago_id,
            "mc": payload.monto_cuota, "dv": payload.dia_vencimiento,
            "fi": payload.fecha_inicio, "ff": payload.fecha_fin,
            "muc": payload.monto_ultima_cuota, "notas": payload.notas,
        },
    )
    new_id = result.scalar()
    await db.commit()
    return {"id": str(new_id)}


@router.patch("/prestamo/{loan_id}")
async def update_prestamo(loan_id: str, payload: PrestamoUpdate, db: AsyncSession = Depends(get_db)):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Sin cambios")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = loan_id
    result = await db.execute(
        text(f"UPDATE loans SET {sets}, updated_at = now() WHERE id = :id RETURNING id"),
        fields,
    )
    if not result.first():
        raise HTTPException(404, "Préstamo no encontrado")
    await db.commit()
    return {"ok": True}


@router.delete("/prestamo/{loan_id}")
async def delete_prestamo(loan_id: str, db: AsyncSession = Depends(get_db)):
    """Soft-delete: marca activo=false. Las cuotas ya generadas se conservan."""
    result = await db.execute(
        text("UPDATE loans SET activo = false, updated_at = now() WHERE id = :id RETURNING id"),
        {"id": loan_id},
    )
    if not result.first():
        raise HTTPException(404, "Préstamo no encontrado")
    await db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────
# Compras en cuotas
# ─────────────────────────────────────────────────────────────────

@router.get("/cuotas")
async def get_cuotas(db: AsyncSession = Depends(get_db)):
    """Lista planes activos con cuotas pendientes (transaction_date > hoy)."""
    rows = (await db.execute(text("""
        SELECT ip.id, ip.account_id, a.nombre AS account_nombre,
               ip.fecha_compra, ip.descripcion, ip.monto_total, ip.cuotas_total,
               ip.monto_cuota, ip.categoria, ip.subcategoria1, ip.subcategoria2,
               ip.notas, ip.activo,
               COALESCE(SUM(CASE WHEN t.fecha_valor <= CURRENT_DATE THEN 1 ELSE 0 END), 0) AS cuotas_pagadas,
               COALESCE(SUM(CASE WHEN t.fecha_valor >  CURRENT_DATE THEN t.amount ELSE 0 END), 0) AS pendiente
        FROM installment_plans ip
        LEFT JOIN accounts a ON a.id = ip.account_id
        LEFT JOIN transactions t
               ON t.installment_plan_id = ip.id AND t.deleted_at IS NULL
        WHERE ip.activo
        GROUP BY ip.id, a.nombre
        ORDER BY ip.fecha_compra DESC
    """))).all()
    return [
        {
            "id": str(r.id),
            "account_id": str(r.account_id),
            "account_nombre": r.account_nombre,
            "fecha_compra": r.fecha_compra.isoformat(),
            "descripcion": r.descripcion,
            "monto_total": float(r.monto_total),
            "cuotas_total": int(r.cuotas_total),
            "monto_cuota": float(r.monto_cuota),
            "categoria": r.categoria,
            "subcategoria1": r.subcategoria1,
            "subcategoria2": r.subcategoria2,
            "notas": r.notas,
            "activo": r.activo,
            "cuotas_pagadas": int(r.cuotas_pagadas),
            "pendiente": round(float(r.pendiente), 2),
        }
        for r in rows
    ]


@router.post("/cuota")
async def create_cuota(payload: InstallmentPlanIn, db: AsyncSession = Depends(get_db)):
    cuenta = (await db.execute(
        text("SELECT tipo FROM accounts WHERE id = :id AND activa"),
        {"id": payload.account_id},
    )).first()
    if not cuenta:
        raise HTTPException(404, "Cuenta no encontrada")
    if cuenta.tipo != "tarjeta_credito":
        raise HTTPException(400, "Las compras en cuotas solo aplican a cuentas de tipo 'tarjeta_credito'")

    monto_cuota = payload.monto_cuota or round(payload.monto_total / payload.cuotas_total, 2)

    result = await db.execute(
        text("""
            INSERT INTO installment_plans
                (account_id, fecha_compra, descripcion, monto_total, cuotas_total,
                 monto_cuota, categoria, subcategoria1, subcategoria2, notas)
            VALUES (:aid, :fc, :desc, :mt, :ct, :mc, :cat, :s1, :s2, :nt)
            RETURNING id
        """),
        {
            "aid": payload.account_id, "fc": payload.fecha_compra,
            "desc": payload.descripcion, "mt": payload.monto_total,
            "ct": payload.cuotas_total, "mc": monto_cuota,
            "cat": payload.categoria, "s1": payload.subcategoria1,
            "s2": payload.subcategoria2, "nt": payload.notas,
        },
    )
    plan_id = result.scalar()
    await db.commit()

    # Expandir el plan en N transactions inmediatamente
    created = installment_generator.expand_plan(plan_id)
    return {"id": str(plan_id), "cuotas_generadas": len(created)}


@router.delete("/cuota/{plan_id}")
async def delete_cuota(plan_id: str, db: AsyncSession = Depends(get_db)):
    """Soft-delete del plan + soft-delete de las cuotas FUTURAS (transaction_date > hoy).
    Las cuotas pasadas se conservan porque ya impactaron tu historial.
    """
    result = await db.execute(
        text("UPDATE installment_plans SET activo = false, updated_at = now() WHERE id = :id RETURNING id"),
        {"id": plan_id},
    )
    if not result.first():
        raise HTTPException(404, "Plan no encontrado")
    await db.execute(
        text("""
            UPDATE transactions
            SET deleted_at = now()
            WHERE installment_plan_id = :id
              AND deleted_at IS NULL
              AND fecha_valor > CURRENT_DATE
        """),
        {"id": plan_id},
    )
    await db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────
# Suscripciones recurrentes
# ─────────────────────────────────────────────────────────────────

@router.get("/suscripciones")
async def get_suscripciones(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(text("""
        SELECT r.id, r.account_id, a.nombre AS account_nombre, r.nombre, r.monto,
               r.dia_mes, r.categoria, r.subcategoria1, r.subcategoria2,
               r.fecha_inicio, r.fecha_fin, r.activo
        FROM recurring_charges r
        LEFT JOIN accounts a ON a.id = r.account_id
        WHERE r.activo
        ORDER BY r.dia_mes, r.nombre
    """))).all()
    return [
        {
            "id": str(r.id),
            "account_id": str(r.account_id),
            "account_nombre": r.account_nombre,
            "nombre": r.nombre,
            "monto": float(r.monto),
            "dia_mes": int(r.dia_mes),
            "categoria": r.categoria,
            "subcategoria1": r.subcategoria1,
            "subcategoria2": r.subcategoria2,
            "fecha_inicio": r.fecha_inicio.isoformat() if r.fecha_inicio else None,
            "fecha_fin": r.fecha_fin.isoformat() if r.fecha_fin else None,
            "activo": r.activo,
        }
        for r in rows
    ]


@router.post("/suscripcion")
async def create_suscripcion(payload: SuscripcionIn, db: AsyncSession = Depends(get_db)):
    fields = payload.model_dump(exclude_unset=True)
    cols = ", ".join(fields.keys())
    vals = ", ".join(f":{k}" for k in fields.keys())
    result = await db.execute(
        text(f"INSERT INTO recurring_charges ({cols}) VALUES ({vals}) RETURNING id"),
        fields,
    )
    new_id = result.scalar()
    await db.commit()
    return {"id": str(new_id)}


@router.patch("/suscripcion/{rid}")
async def update_suscripcion(rid: str, payload: SuscripcionUpdate, db: AsyncSession = Depends(get_db)):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Sin cambios")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = rid
    result = await db.execute(
        text(f"UPDATE recurring_charges SET {sets}, updated_at = now() WHERE id = :id RETURNING id"),
        fields,
    )
    if not result.first():
        raise HTTPException(404, "Suscripción no encontrada")
    await db.commit()
    return {"ok": True}


@router.delete("/suscripcion/{rid}")
async def delete_suscripcion(rid: str, db: AsyncSession = Depends(get_db)):
    """Soft-delete: marca activo=false. Las transactions ya generadas se conservan."""
    result = await db.execute(
        text("UPDATE recurring_charges SET activo = false, updated_at = now() WHERE id = :id RETURNING id"),
        {"id": rid},
    )
    if not result.first():
        raise HTTPException(404, "Suscripción no encontrada")
    await db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────
# Tarjeta: ciclo en curso, próximo pago y resúmenes
# ─────────────────────────────────────────────────────────────────

@router.get("/tarjeta/{account_id}/resumen")
async def get_card_summary(account_id: str, db: AsyncSession = Depends(get_db)):
    """Resumen del ciclo abierto + próximo vencimiento + últimos resúmenes pagados."""
    card = (await db.execute(text("""
        SELECT id, nombre, cierre_dia, vencimiento_dia, cuenta_pago_id
        FROM accounts
        WHERE id = :id AND tipo = 'tarjeta_credito' AND activa
    """), {"id": account_id})).first()
    if not card:
        raise HTTPException(404, "Tarjeta no encontrada")
    if card.cierre_dia is None or card.vencimiento_dia is None:
        raise HTTPException(400, "Tarjeta sin ciclo configurado")

    today = datetime.now().date()
    cierre_dia = card.cierre_dia
    vto_dia    = card.vencimiento_dia

    # Próximo cierre y próximo vencimiento
    last_day_curr = monthrange(today.year, today.month)[1]
    cierre_curr   = date_cls(today.year, today.month, min(cierre_dia, last_day_curr))
    if cierre_curr <= today:
        ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        last_day_next = monthrange(ny, nm)[1]
        proximo_cierre = date_cls(ny, nm, min(cierre_dia, last_day_next))
    else:
        proximo_cierre = cierre_curr

    py, pm = (proximo_cierre.year - 1, 12) if proximo_cierre.month == 1 else (proximo_cierre.year, proximo_cierre.month - 1)
    cierre_anterior = date_cls(py, pm, min(cierre_dia, monthrange(py, pm)[1]))

    # Vencimiento del próximo cierre = primer vencimiento_dia >= proximo_cierre
    vy, vm = (proximo_cierre.year + 1, 1) if proximo_cierre.month == 12 else (proximo_cierre.year, proximo_cierre.month + 1)
    last_day_v = monthrange(vy, vm)[1]
    proximo_vto = date_cls(vy, vm, min(vto_dia, last_day_v))

    ciclo_total = (await db.execute(text("""
        SELECT COALESCE(SUM(
            CASE WHEN tipo='gasto' THEN amount WHEN tipo='ingreso' THEN -amount ELSE 0 END
        ), 0) AS monto
        FROM transactions
        WHERE account_id = :aid
          AND deleted_at IS NULL
          AND card_statement_id IS NULL
          AND transaction_date >  :prev
          AND transaction_date <= :curr
    """), {"aid": account_id, "prev": cierre_anterior, "curr": proximo_cierre})).scalar()

    movs = (await db.execute(text("""
        SELECT id, transaction_date, amount, tipo, subcategoria1, subcategoria2, nota, origen
        FROM transactions
        WHERE account_id = :aid AND deleted_at IS NULL AND card_statement_id IS NULL
          AND transaction_date >  :prev AND transaction_date <= :curr
        ORDER BY transaction_date DESC
        LIMIT 50
    """), {"aid": account_id, "prev": cierre_anterior, "curr": proximo_cierre})).all()

    historico = (await db.execute(text("""
        SELECT id, fecha_cierre, fecha_vencimiento, monto, pagado, pagado_at
        FROM card_statements
        WHERE account_id = :aid
        ORDER BY fecha_cierre DESC
        LIMIT 12
    """), {"aid": account_id})).all()

    return {
        "tarjeta": {"id": str(card.id), "nombre": card.nombre},
        "ciclo_actual": {
            "desde": cierre_anterior.isoformat(),
            "hasta": proximo_cierre.isoformat(),
            "total": round(float(ciclo_total or 0), 2),
            "proximo_vto": proximo_vto.isoformat(),
        },
        "movimientos_ciclo": [
            {
                "id": str(m.id), "fecha": m.transaction_date.isoformat(),
                "amount": float(m.amount), "tipo": m.tipo,
                "subcategoria1": m.subcategoria1, "subcategoria2": m.subcategoria2,
                "nota": m.nota, "origen": m.origen,
            }
            for m in movs
        ],
        "historico": [
            {
                "id": str(h.id),
                "fecha_cierre": h.fecha_cierre.isoformat(),
                "fecha_vencimiento": h.fecha_vencimiento.isoformat(),
                "monto": float(h.monto),
                "pagado": h.pagado,
                "pagado_at": h.pagado_at.isoformat() if h.pagado_at else None,
            }
            for h in historico
        ],
    }
