"""
API REST de listas familiares — /api/compras (lista de compras).
Estructura compartida (sin family_member_id en filtro).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


class CompraIn(BaseModel):
    texto: str = Field(min_length=1, max_length=120)
    cantidad: Optional[float] = Field(default=None, ge=0)
    unidad: Optional[str] = Field(default=None, max_length=10)
    created_by: Optional[str] = None


class CompraUpdate(BaseModel):
    texto: Optional[str] = Field(default=None, min_length=1, max_length=120)
    cantidad: Optional[float] = Field(default=None, ge=0)
    unidad: Optional[str] = Field(default=None, max_length=10)


def _row_to_dict(r) -> dict:
    return {
        "id":           str(r.id),
        "texto":        r.texto,
        "cantidad":     float(r.cantidad) if r.cantidad is not None else None,
        "unidad":       r.unidad,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "created_at":   r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/compras")
async def list_compras(
    incluir_completados: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    where = "" if incluir_completados else "WHERE completed_at IS NULL"
    rows = (await db.execute(text(f"""
        SELECT id, texto, cantidad, unidad, completed_at, created_at
        FROM shopping_list_items
        {where}
        ORDER BY completed_at NULLS FIRST, created_at DESC
        LIMIT 200
    """))).all()
    return [_row_to_dict(r) for r in rows]


@router.post("/compra")
async def create_compra(payload: CompraIn, db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        INSERT INTO shopping_list_items (texto, cantidad, unidad, created_by)
        VALUES (:t, :c, :u, :cb)
        RETURNING id, texto, cantidad, unidad, completed_at, created_at
    """), {"t": payload.texto.strip(), "c": payload.cantidad, "u": payload.unidad,
           "cb": payload.created_by})
    row = result.first()
    await db.commit()
    return _row_to_dict(row)


@router.patch("/compra/{cid}")
async def update_compra(cid: str, payload: CompraUpdate, db: AsyncSession = Depends(get_db)):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Sin cambios")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = cid
    result = await db.execute(text(f"""
        UPDATE shopping_list_items SET {sets}, updated_at = now()
        WHERE id = :id
        RETURNING id
    """), fields)
    if not result.first():
        raise HTTPException(404, "Item no encontrado")
    await db.commit()
    return {"ok": True}


@router.patch("/compra/{cid}/done")
async def marcar_done(cid: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        UPDATE shopping_list_items
        SET completed_at = now(), updated_at = now()
        WHERE id = :id AND completed_at IS NULL
        RETURNING id
    """), {"id": cid})
    if not result.first():
        raise HTTPException(404, "Item no encontrado o ya completado")
    await db.commit()
    return {"ok": True}


@router.patch("/compra/{cid}/undo")
async def desmarcar_done(cid: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        UPDATE shopping_list_items
        SET completed_at = NULL, updated_at = now()
        WHERE id = :id AND completed_at IS NOT NULL
        RETURNING id
    """), {"id": cid})
    if not result.first():
        raise HTTPException(404, "Item no encontrado o no estaba completado")
    await db.commit()
    return {"ok": True}


@router.delete("/compra/{cid}")
async def delete_compra(cid: str, db: AsyncSession = Depends(get_db)):
    """Borrado real (no soft-delete) — son items efímeros, no es necesario el historial."""
    result = await db.execute(text("DELETE FROM shopping_list_items WHERE id = :id RETURNING id"),
                              {"id": cid})
    if not result.first():
        raise HTTPException(404, "Item no encontrado")
    await db.commit()
    return {"ok": True}


@router.post("/compras/vaciar")
async def vaciar_compras(db: AsyncSession = Depends(get_db)):
    """Marca todos los pendientes como completados (vuelta del super)."""
    result = await db.execute(text("""
        UPDATE shopping_list_items SET completed_at = now(), updated_at = now()
        WHERE completed_at IS NULL
        RETURNING id
    """))
    n = len(result.all())
    await db.commit()
    return {"ok": True, "completados": n}


@router.delete("/compras/historial")
async def borrar_historial(db: AsyncSession = Depends(get_db)):
    """Borra los items completados (no afecta los pendientes)."""
    result = await db.execute(text("DELETE FROM shopping_list_items WHERE completed_at IS NOT NULL RETURNING id"))
    n = len(result.all())
    await db.commit()
    return {"ok": True, "borrados": n}
