"""
Wrapper síncrono para faster-whisper — solo se llama desde Celery workers.
El modelo se carga una única vez (singleton) para evitar re-cargar 1.5GB por tarea.
"""
import os
import tempfile
from typing import Optional

from faster_whisper import WhisperModel

from app.core.config import settings

_model: Optional[WhisperModel] = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        # device="cpu", compute_type="int8" — óptimo para Mini PC sin GPU dedicada
        _model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
    return _model


def transcribe(audio_bytes: bytes) -> str:
    """
    Transcribe audio bytes a texto en español.
    Escribe a un archivo temporal porque faster-whisper requiere un path en disco.
    """
    model = _get_model()

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        segments, _ = model.transcribe(
            tmp_path,
            language="es",
            beam_size=5,
            no_speech_threshold=0.3,
            initial_prompt="Registro de gastos familiares en euros.",
        )
        return " ".join(seg.text for seg in segments).strip()
    finally:
        os.unlink(tmp_path)
