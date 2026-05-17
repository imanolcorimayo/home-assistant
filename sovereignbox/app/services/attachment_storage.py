"""
Storage de attachments en disco local + persistencia de metadata en `attachments`.

Estructura de directorios: /app/data/files/{yyyy}/{mm}/{uuid}.{ext}

- Atómico: el archivo se escribe primero a un .tmp y luego se renombra.
- Seguro: si la escritura del DB falla, el archivo se borra para no dejar basura.
- Idempotente: cada upload genera un UUID único, no hay colisión.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Volumen Docker `media_data` montado en /app/data/files (api y worker)
BASE_DIR = Path(os.environ.get("ATTACHMENTS_DIR", "/app/data/files"))


def _ext_from(name: str, mime: str | None = None) -> str:
    if "." in name:
        ext = name.rsplit(".", 1)[1].lower()
        # Sanitizar: solo a-z, 0-9, máximo 8 chars
        ext = "".join(c for c in ext if c.isalnum())[:8]
        if ext:
            return ext
    if mime:
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return guessed.lstrip(".")
    return "bin"


def _resolve_path(year: int, month: int, file_id: uuid.UUID, ext: str) -> Path:
    return BASE_DIR / f"{year}" / f"{month:02d}" / f"{file_id}.{ext}"


async def save_file(
    db: AsyncSession,
    *,
    content: bytes,
    original_name: str,
    mime_type: Optional[str],
    uploaded_by: Optional[str],
    uploaded_via: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    role: Optional[str] = None,
    notas: Optional[str] = None,
) -> dict:
    """Guarda el archivo y persiste metadata. Retorna la fila como dict.

    Si entity_type/entity_id son None, queda como "huérfano" (asociable después).
    """
    if not content:
        raise ValueError("content vacío")
    mime = mime_type or (mimetypes.guess_type(original_name)[0] or "application/octet-stream")
    file_id = uuid.uuid4()
    now = datetime.now()
    ext = _ext_from(original_name, mime)
    path = _resolve_path(now.year, now.month, file_id, ext)
    path.parent.mkdir(parents=True, exist_ok=True)

    rel_path = str(path.relative_to(BASE_DIR))
    tmp = path.with_suffix(path.suffix + ".tmp")

    try:
        tmp.write_bytes(content)
        tmp.rename(path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

    try:
        result = await db.execute(sa_text("""
            INSERT INTO attachments
                (id, file_path, original_name, mime_type, size_bytes,
                 uploaded_by, uploaded_via, entity_type, entity_id, role, notas)
            VALUES (:id, :fp, :on, :mt, :sb, :ub, :uv, :et, :ei, :r, :n)
            RETURNING id, file_path, original_name, mime_type, size_bytes,
                      uploaded_by, uploaded_via, entity_type, entity_id, role, notas, created_at
        """), {
            "id": file_id, "fp": rel_path, "on": original_name,
            "mt": mime, "sb": len(content),
            "ub": uploaded_by, "uv": uploaded_via,
            "et": entity_type, "ei": entity_id, "r": role, "n": notas,
        })
        row = result.first()
        await db.commit()
    except Exception:
        # Si falla el insert, limpiamos el archivo
        path.unlink(missing_ok=True)
        await db.rollback()
        raise

    logger.info("Attachment guardado: %s (%d bytes, role=%s)", rel_path, len(content), role)
    return _row_to_dict(row)


def _row_to_dict(r) -> dict:
    return {
        "id":             str(r.id),
        "file_path":      r.file_path,
        "original_name":  r.original_name,
        "mime_type":      r.mime_type,
        "size_bytes":     int(r.size_bytes),
        "uploaded_by":    str(r.uploaded_by) if r.uploaded_by else None,
        "uploaded_via":   r.uploaded_via,
        "entity_type":    r.entity_type,
        "entity_id":      str(r.entity_id) if r.entity_id else None,
        "role":           r.role,
        "notas":          r.notas,
        "created_at":     r.created_at.isoformat() if r.created_at else None,
    }


def absolute_path(file_path: str) -> Path:
    """Devuelve la ruta absoluta al archivo en disco. Valida que esté DENTRO de BASE_DIR
    (defensa contra path traversal)."""
    p = (BASE_DIR / file_path).resolve()
    if not str(p).startswith(str(BASE_DIR.resolve())):
        raise ValueError("path traversal detectado")
    return p
