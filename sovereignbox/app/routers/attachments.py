"""
API REST de attachments — /api/attachments
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services import attachment_storage

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_ENTITIES = {"transaction", "event", "task", "shopping_item"}
ALLOWED_ROLES    = {"boleta", "comprobante", "foto", "documento", "otro"}


@router.post("/attachments")
async def upload_attachment(
    file: UploadFile = File(...),
    entity_type: Optional[str] = Form(default=None),
    entity_id:   Optional[str] = Form(default=None),
    role:        Optional[str] = Form(default=None),
    notas:       Optional[str] = Form(default=None),
    uploaded_by: Optional[str] = Form(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Sube un archivo (multipart). Si entity_type/entity_id no se mandan, queda
    huérfano (asociable después con PATCH).
    """
    if entity_type and entity_type not in ALLOWED_ENTITIES:
        raise HTTPException(400, f"entity_type inválido: {entity_type}")
    if role and role not in ALLOWED_ROLES:
        raise HTTPException(400, f"role inválido: {role}")
    if (entity_type and not entity_id) or (entity_id and not entity_type):
        raise HTTPException(400, "entity_type y entity_id deben venir juntos")

    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(413, "Archivo > 25 MB no soportado")

    row = await attachment_storage.save_file(
        db,
        content=content,
        original_name=file.filename or "archivo",
        mime_type=file.content_type,
        uploaded_by=uploaded_by,
        uploaded_via="web",
        entity_type=entity_type,
        entity_id=entity_id,
        role=role,
        notas=notas,
    )
    return row


@router.get("/attachments")
async def list_attachments(
    entity_type: Optional[str] = Query(default=None),
    entity_id:   Optional[str] = Query(default=None),
    huerfanos:   bool          = Query(default=False),
    limit:       int           = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Lista metadata. Si huerfanos=true, devuelve los attachments sin entidad asociada.
    Si se manda entity_type+entity_id, filtra por esa entidad.
    """
    where = "deleted_at IS NULL"
    params: dict = {"limit": limit}
    if huerfanos:
        where += " AND entity_type IS NULL"
    elif entity_type and entity_id:
        where += " AND entity_type = :et AND entity_id = :ei"
        params["et"] = entity_type
        params["ei"] = entity_id

    rows = (await db.execute(text(f"""
        SELECT id, file_path, original_name, mime_type, size_bytes,
               uploaded_by, uploaded_via, entity_type, entity_id, role, notas, created_at
        FROM attachments
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT :limit
    """), params)).all()
    return [attachment_storage._row_to_dict(r) for r in rows]


@router.get("/attachments/{att_id}/file")
async def download_attachment(att_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text("""
        SELECT file_path, mime_type, original_name FROM attachments
        WHERE id = :id AND deleted_at IS NULL
    """), {"id": att_id})).first()
    if not row:
        raise HTTPException(404, "Attachment no encontrado")
    try:
        path = attachment_storage.absolute_path(row.file_path)
    except ValueError:
        raise HTTPException(500, "Ruta inválida")
    if not path.exists():
        raise HTTPException(410, "Archivo perdido del disco")
    return FileResponse(path, media_type=row.mime_type, filename=row.original_name)


@router.patch("/attachments/{att_id}/asociar")
async def associate_attachment(
    att_id: str,
    entity_type: str = Form(...),
    entity_id:   str = Form(...),
    role:   Optional[str] = Form(default=None),
    db: AsyncSession = Depends(get_db),
):
    if entity_type not in ALLOWED_ENTITIES:
        raise HTTPException(400, f"entity_type inválido")
    if role and role not in ALLOWED_ROLES:
        raise HTTPException(400, f"role inválido")
    fields = {"id": att_id, "et": entity_type, "ei": entity_id}
    sets = "entity_type = :et, entity_id = :ei"
    if role:
        sets += ", role = :r"
        fields["r"] = role
    result = await db.execute(text(f"""
        UPDATE attachments SET {sets}
        WHERE id = :id AND deleted_at IS NULL
        RETURNING id
    """), fields)
    if not result.first():
        raise HTTPException(404, "Attachment no encontrado")
    await db.commit()
    return {"ok": True}


@router.post("/attachments/{att_id}/sugerir-archivo")
async def sugerir_archivo(att_id: str, db: AsyncSession = Depends(get_db)):
    """Pide al LLM que sugiera dónde guardar este papel en el archivo físico.
    Junta la metadata del attachment + la entidad asociada (si es transaction).
    """
    from app.services import ollama_client

    info = (await db.execute(text("""
        SELECT att.id, att.mime_type, att.created_at,
               tx.categoria, tx.subcategoria1, tx.subcategoria2, tx.nota, tx.transaction_date
        FROM attachments att
        LEFT JOIN transactions tx
               ON att.entity_type = 'transaction' AND att.entity_id = tx.id
        WHERE att.id = :id AND att.deleted_at IS NULL
    """), {"id": att_id})).first()
    if not info:
        raise HTTPException(404, "Attachment no encontrado")

    fecha = info.transaction_date or (info.created_at.date() if info.created_at else None)
    try:
        sug = ollama_client.suggest_filing_path(
            categoria=info.categoria,
            subcategoria1=info.subcategoria1,
            subcategoria2=info.subcategoria2,
            nota=info.nota,
            mime=info.mime_type,
            fecha=fecha,
        )
    except Exception as exc:
        logger.error("sugerir_archivo falló: %s", exc)
        # Fallback heurístico
        nombres_mes = ['Enero','Febrero','Marzo','Abril','Mayo','Junio',
                       'Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']
        ruta_parts = ["Varios"]
        if info.categoria:
            ruta_parts = ([info.categoria.replace("Gastos ", "")])
        if info.subcategoria1:
            ruta_parts.append(info.subcategoria1)
        if fecha:
            ruta_parts.append(f"{fecha.year} - {nombres_mes[fecha.month-1]}")
        sug = {"ruta": " → ".join(ruta_parts), "razon": "fallback (LLM no disponible)"}

    return sug


@router.delete("/attachments/{att_id}")
async def delete_attachment(att_id: str, db: AsyncSession = Depends(get_db)):
    """Soft-delete. El archivo en disco queda hasta el próximo backup/cleanup."""
    result = await db.execute(text("""
        UPDATE attachments SET deleted_at = now()
        WHERE id = :id AND deleted_at IS NULL
        RETURNING id
    """), {"id": att_id})
    if not result.first():
        raise HTTPException(404, "Attachment no encontrado")
    await db.commit()
    return {"ok": True}
