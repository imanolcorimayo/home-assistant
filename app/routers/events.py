"""
API REST de agenda familiar — /api/eventos

Eventos compartidos: sin filtro por miembro. Categorías: medico|colegio|
burocracia|familia|otro.

Cuando se crea o edita un evento, se programan recordatorios en la cola
`notifications` para todos los miembros con telegram_user_id, según el
campo `recordatorio_horas_antes`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

CATEGORIAS = {"medico", "colegio", "burocracia", "familia", "otro"}
DEFAULT_REMINDER_HOURS = {
    "medico":     24,
    "colegio":    24,
    "burocracia": 24,
    "familia":     2,
    "otro":        2,
}


class EventoIn(BaseModel):
    titulo: str = Field(min_length=1, max_length=120)
    fecha: date
    hora: Optional[time] = None
    fin_fecha: Optional[date] = None
    fin_hora: Optional[time] = None
    categoria: str = Field(default="otro")
    descripcion: Optional[str] = Field(default=None, max_length=500)
    ubicacion: Optional[str] = Field(default=None, max_length=200)
    recordatorio_horas_antes: Optional[int] = Field(default=None, ge=0, le=168)
    created_by: Optional[str] = None


class EventoUpdate(BaseModel):
    titulo: Optional[str] = Field(default=None, min_length=1, max_length=120)
    fecha: Optional[date] = None
    hora: Optional[time] = None
    fin_fecha: Optional[date] = None
    fin_hora: Optional[time] = None
    categoria: Optional[str] = None
    descripcion: Optional[str] = Field(default=None, max_length=500)
    ubicacion: Optional[str] = Field(default=None, max_length=200)
    recordatorio_horas_antes: Optional[int] = Field(default=None, ge=0, le=168)


def _row_to_dict(r) -> dict:
    return {
        "id":                       str(r.id),
        "titulo":                   r.titulo,
        "fecha":                    r.fecha.isoformat() if r.fecha else None,
        "hora":                     r.hora.strftime("%H:%M") if r.hora else None,
        "fin_fecha":                r.fin_fecha.isoformat() if r.fin_fecha else None,
        "fin_hora":                 r.fin_hora.strftime("%H:%M") if r.fin_hora else None,
        "categoria":                r.categoria,
        "descripcion":              r.descripcion,
        "ubicacion":                r.ubicacion,
        "recordatorio_horas_antes": int(r.recordatorio_horas_antes) if r.recordatorio_horas_antes is not None else 2,
        "created_at":               r.created_at.isoformat() if r.created_at else None,
    }


async def _schedule_reminder(db: AsyncSession, event_id: str) -> int:
    """Lee el evento y encola notifications para todos los miembros con telegram_user_id.

    Borra primero recordatorios previos (por dedupe_key) — al editar un evento
    re-encolamos con la nueva fecha/hora. Usamos dedupe_key 'event-{id}-{chat}'
    para que un mismo evento solo tenga 1 recordatorio activo por chat.
    """
    ev = (await db.execute(text("""
        SELECT id, titulo, fecha, hora, categoria, ubicacion, recordatorio_horas_antes
        FROM events WHERE id = :id AND deleted_at IS NULL
    """), {"id": event_id})).first()
    if not ev or not ev.recordatorio_horas_antes:
        return 0

    if not ev.hora:
        # Evento de día completo: recordar el día anterior 18:00
        scheduled_at = datetime.combine(ev.fecha, time(8, 0)) - timedelta(days=1)
    else:
        scheduled_at = datetime.combine(ev.fecha, ev.hora) - timedelta(hours=int(ev.recordatorio_horas_antes))

    if scheduled_at <= datetime.now():
        return 0  # ya pasó el momento — no recordar

    miembros = (await db.execute(text(
        "SELECT telegram_user_id FROM family_members WHERE telegram_user_id IS NOT NULL AND is_active"
    ))).all()

    icon = {"medico": "🏥", "colegio": "🏫", "burocracia": "📋",
            "familia": "👨‍👩‍👧", "otro": "📅"}.get(ev.categoria, "📅")
    hora_str = ev.hora.strftime("%H:%M") if ev.hora else "todo el día"
    title = f"{icon} {ev.titulo}"
    body  = f"{ev.fecha.strftime('%d/%m')} · {hora_str}"
    if ev.ubicacion:
        body += f" · {ev.ubicacion}"

    encolados = 0
    for m in miembros:
        # Borrar recordatorio previo del mismo evento+chat (al editar)
        await db.execute(text("""
            DELETE FROM notifications
            WHERE related_entity_type = 'event' AND related_entity_id = :ei
              AND target_chat_id = :cid AND sent_at IS NULL
        """), {"ei": event_id, "cid": m.telegram_user_id})

        await db.execute(text("""
            INSERT INTO notifications
                (target_chat_id, kind, title, body, scheduled_at,
                 related_entity_type, related_entity_id, dedupe_key)
            VALUES (:cid, 'reminder', :t, :b, :s,
                    'event', :ei, :dk)
            ON CONFLICT (dedupe_key) DO NOTHING
        """), {
            "cid": m.telegram_user_id, "t": title, "b": body, "s": scheduled_at,
            "ei": event_id,
            "dk": f"event-{event_id}-{m.telegram_user_id}-{scheduled_at.isoformat()}",
        })
        encolados += 1

    return encolados


@router.get("/eventos")
async def list_eventos(
    desde: Optional[date] = Query(default=None),
    hasta: Optional[date] = Query(default=None),
    categoria: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Lista eventos en el rango [desde, hasta]. Default: próximos 30 días."""
    if not desde:
        desde = date.today()
    if not hasta:
        hasta = desde + timedelta(days=30)

    where = "deleted_at IS NULL AND fecha BETWEEN :d AND :h"
    params: dict = {"d": desde, "h": hasta}
    if categoria:
        where += " AND categoria = :c"
        params["c"] = categoria

    rows = (await db.execute(text(f"""
        SELECT id, titulo, fecha, hora, fin_fecha, fin_hora, categoria,
               descripcion, ubicacion, recordatorio_horas_antes, created_at
        FROM events
        WHERE {where}
        ORDER BY fecha, hora NULLS LAST
        LIMIT 500
    """), params)).all()
    return [_row_to_dict(r) for r in rows]


@router.post("/evento")
async def create_evento(payload: EventoIn, db: AsyncSession = Depends(get_db)):
    if payload.categoria not in CATEGORIAS:
        raise HTTPException(400, f"categoria inválida: {payload.categoria}")

    rec_h = payload.recordatorio_horas_antes
    if rec_h is None:
        rec_h = DEFAULT_REMINDER_HOURS.get(payload.categoria, 2)

    result = await db.execute(text("""
        INSERT INTO events
            (titulo, fecha, hora, fin_fecha, fin_hora, categoria,
             descripcion, ubicacion, recordatorio_horas_antes, created_by)
        VALUES (:t, :f, :h, :ff, :fh, :c, :d, :u, :rh, :cb)
        RETURNING id
    """), {
        "t": payload.titulo.strip(), "f": payload.fecha, "h": payload.hora,
        "ff": payload.fin_fecha, "fh": payload.fin_hora,
        "c": payload.categoria, "d": payload.descripcion, "u": payload.ubicacion,
        "rh": rec_h, "cb": payload.created_by,
    })
    new_id = str(result.scalar())
    await db.commit()

    encolados = await _schedule_reminder(db, new_id)
    await db.commit()
    return {"id": new_id, "recordatorios_encolados": encolados}


@router.patch("/evento/{eid}")
async def update_evento(eid: str, payload: EventoUpdate, db: AsyncSession = Depends(get_db)):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Sin cambios")
    if "categoria" in fields and fields["categoria"] not in CATEGORIAS:
        raise HTTPException(400, f"categoria inválida")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = eid
    result = await db.execute(text(f"""
        UPDATE events SET {sets}, updated_at = now()
        WHERE id = :id AND deleted_at IS NULL
        RETURNING id
    """), fields)
    if not result.first():
        raise HTTPException(404, "Evento no encontrado")
    await db.commit()

    encolados = await _schedule_reminder(db, eid)
    await db.commit()
    return {"ok": True, "recordatorios_encolados": encolados}


@router.delete("/evento/{eid}")
async def delete_evento(eid: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        UPDATE events SET deleted_at = now()
        WHERE id = :id AND deleted_at IS NULL RETURNING id
    """), {"id": eid})
    if not result.first():
        raise HTTPException(404, "Evento no encontrado")
    # Cancelar recordatorios pendientes
    await db.execute(text("""
        DELETE FROM notifications
        WHERE related_entity_type = 'event' AND related_entity_id = :id AND sent_at IS NULL
    """), {"id": eid})
    await db.commit()
    return {"ok": True}
