"""Local-disk media store — the source of truth is the `media` table; files live
on a writable volume under MEDIA_ROOT. Designed to be tidy and exportable:

  layout:  {MEDIA_ROOT}/{family_id}/{YYYY}/{MM}/{media_id}.{ext}
  table:   stores the RELATIVE path (under MEDIA_ROOT) + sha256 + metadata.

Because paths are relative and derivable from the row, exporting a family later
is just "copy that family's subtree + dump its rows", and swapping to S3/R2 only
touches this module — the table doesn't change.
"""

import hashlib
import os
import uuid
from datetime import datetime

from app import config, db

_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/ogg": "ogg", "audio/webm": "webm", "audio/aac": "aac", "audio/mp4": "m4a",
    "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/flac": "flac",
}


def base_mime(mime: str) -> str:
    """Strip parameters like `;codecs=opus` (MediaRecorder adds these) and
    lowercase — so 'audio/mp4;codecs=opus' compares as 'audio/mp4'."""
    return (mime or "").split(";")[0].strip().lower()


def classify(mime: str) -> str | None:
    """Map a MIME type to our coarse kind, or None if not an accepted type.
    Tolerant of codec parameters on the type."""
    m = base_mime(mime)
    if m in config.ALLOWED_IMAGE_MIME:
        return "image"
    if m in config.ALLOWED_AUDIO_MIME:
        return "audio"
    return None


def abs_path(storage_path: str) -> str:
    """Absolute path for a stored relative path. Guards against traversal."""
    root = os.path.realpath(config.MEDIA_ROOT)
    full = os.path.realpath(os.path.join(root, storage_path))
    if not full.startswith(root + os.sep):
        raise ValueError("invalid storage path")
    return full


async def save_image(family_id, member_id, chat_session_id, mime: str,
                     original_filename: str, data: bytes) -> dict:
    """Persist an image to disk + a `media` row. Returns the row as a dict.
    (Images are kept; audio is sent to the model but not persisted — see main.)"""
    media_id = uuid.uuid4()
    now = datetime.now()
    rel_dir = os.path.join(str(family_id), f"{now.year:04d}", f"{now.month:02d}")
    rel_path = os.path.join(rel_dir, f"{media_id}.{_EXT.get(mime, 'bin')}")
    full = os.path.join(config.MEDIA_ROOT, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)

    row = await db.fetchrow(
        """
        INSERT INTO media
            (media_id, family_id, member_id, chat_session_id, kind, mime,
             size_bytes, original_filename, storage_path, sha256)
        VALUES ($1, $2, $3, $4, 'image', $5, $6, $7, $8, $9)
        RETURNING media_id, kind, mime, size_bytes, storage_path
        """,
        media_id, family_id, member_id, chat_session_id, mime,
        len(data), (original_filename or "")[:255], rel_path,
        hashlib.sha256(data).hexdigest(),
    )
    return dict(row)
