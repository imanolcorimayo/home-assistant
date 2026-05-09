"""
API REST de tareas / TODOs — /api/tareas

Las tareas pueden estar asignadas a Hector, Luisiana, o quedar sin asignar
(asignado_a NULL = "cualquiera de la familia"). Se filtran por miembro y
por estado.

La tabla `tasks` ya existía del schema original. Reusamos sus columnas:
- title (varchar 500)
- description
- assigned_to (UUID → family_members)
- due_datetime (timestamptz)
- task_status enum: pendiente | en_progreso | completada | cancelada
- prioridad: baja | normal | alta (agregada en migración 013)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

PRIORIDADES = {"baja", "normal", "alta"}
ESTADOS     = {"pendiente", "en_progreso", "completada", "cancelada"}


class TareaIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=1000)
    assigned_to: Optional[str] = None
    due_datetime: Optional[datetime] = None
    prioridad: str = Field(default="normal")
    created_by: Optional[str] = None


class TareaUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=1000)
    assigned_to: Optional[str] = None
    due_datetime: Optional[datetime] = None
    prioridad: Optional[str] = None
    task_status: Optional[str] = None


def _row_to_dict(r) -> dict:
    return {
        "id":              str(r.id),
        "title":           r.title,
        "description":     r.description,
        "assigned_to":     str(r.assigned_to) if r.assigned_to else None,
        "assigned_name":   r.assigned_name if hasattr(r, "assigned_name") else None,
        "due_datetime":    r.due_datetime.isoformat() if r.due_datetime else None,
        "task_status":     r.task_status,
        "prioridad":       r.prioridad,
        "created_at":      r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/tareas")
async def list_tareas(
    asignado:   Optional[str] = Query(default=None,
                                       description="UUID, 'sin_asignar', 'todas' (default: pendientes)"),
    estado:     Optional[str] = Query(default="pendiente"),
    db: AsyncSession = Depends(get_db),
):
    where = ["t.deleted_at IS NULL"]
    params: dict = {}
    if estado and estado != "todos":
        if estado not in ESTADOS:
            raise HTTPException(400, f"estado inválido: {estado}")
        where.append("t.task_status = :st")
        params["st"] = estado
    if asignado == "sin_asignar":
        where.append("t.assigned_to IS NULL")
    elif asignado and asignado != "todas":
        where.append("t.assigned_to = :a")
        params["a"] = asignado

    rows = (await db.execute(text(f"""
        SELECT t.id, t.title, t.description, t.assigned_to,
               t.due_datetime, t.task_status, t.prioridad, t.created_at,
               fm.full_name AS assigned_name
        FROM tasks t
        LEFT JOIN family_members fm ON fm.id = t.assigned_to
        WHERE {' AND '.join(where)}
        ORDER BY
          CASE t.prioridad WHEN 'alta' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
          t.due_datetime NULLS LAST,
          t.created_at DESC
        LIMIT 500
    """), params)).all()
    return [_row_to_dict(r) for r in rows]


@router.post("/tarea")
async def create_tarea(payload: TareaIn, db: AsyncSession = Depends(get_db)):
    if payload.prioridad not in PRIORIDADES:
        raise HTTPException(400, f"prioridad inválida: {payload.prioridad}")

    created_by = payload.created_by
    if not created_by:
        # Default: Hector (admin del sistema) cuando no viene de Telegram
        row = (await db.execute(text(
            "SELECT id FROM family_members WHERE full_name = 'Hector Marioni' LIMIT 1"
        ))).first()
        if row:
            created_by = str(row.id)
        else:
            raise HTTPException(500, "No se encontró usuario default para created_by")

    result = await db.execute(text("""
        INSERT INTO tasks (title, description, assigned_to, due_datetime,
                            prioridad, task_status, created_by)
        VALUES (:t, :d, :a, :dd, :p, 'pendiente', :cb)
        RETURNING id
    """), {
        "t": payload.title.strip(), "d": payload.description,
        "a": payload.assigned_to, "dd": payload.due_datetime,
        "p": payload.prioridad, "cb": created_by,
    })
    new_id = str(result.scalar())
    await db.commit()
    return {"id": new_id}


@router.patch("/tarea/{tid}")
async def update_tarea(tid: str, payload: TareaUpdate, db: AsyncSession = Depends(get_db)):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Sin cambios")
    if "prioridad" in fields and fields["prioridad"] not in PRIORIDADES:
        raise HTTPException(400, "prioridad inválida")
    if "task_status" in fields and fields["task_status"] not in ESTADOS:
        raise HTTPException(400, "task_status inválido")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = tid
    result = await db.execute(text(f"""
        UPDATE tasks SET {sets}, updated_at = now()
        WHERE id = :id AND deleted_at IS NULL
        RETURNING id
    """), fields)
    if not result.first():
        raise HTTPException(404, "Tarea no encontrada")
    await db.commit()
    return {"ok": True}


@router.patch("/tarea/{tid}/done")
async def marcar_done(tid: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        UPDATE tasks SET task_status = 'completada', updated_at = now()
        WHERE id = :id AND deleted_at IS NULL AND task_status <> 'completada'
        RETURNING id
    """), {"id": tid})
    if not result.first():
        raise HTTPException(404, "Tarea no encontrada o ya completada")
    await db.commit()
    return {"ok": True}


@router.patch("/tarea/{tid}/undo")
async def desmarcar_done(tid: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        UPDATE tasks SET task_status = 'pendiente', updated_at = now()
        WHERE id = :id AND deleted_at IS NULL AND task_status = 'completada'
        RETURNING id
    """), {"id": tid})
    if not result.first():
        raise HTTPException(404, "Tarea no encontrada o no estaba completada")
    await db.commit()
    return {"ok": True}


@router.delete("/tarea/{tid}")
async def delete_tarea(tid: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        UPDATE tasks SET deleted_at = now()
        WHERE id = :id AND deleted_at IS NULL RETURNING id
    """), {"id": tid})
    if not result.first():
        raise HTTPException(404, "Tarea no encontrada")
    await db.commit()
    return {"ok": True}
