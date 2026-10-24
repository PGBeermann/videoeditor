"""Transcripción con faster-whisper (implementación CTranslate2 de Whisper, licencia MIT).

Produce marcas de tiempo por palabra (necesarias para editar el video borrando texto)
y, opcionalmente, una traducción al inglés (tarea «translate» nativa de Whisper).
"""
from __future__ import annotations

from typing import Callable

from . import config

_model = None


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE,
                              compute_type=config.WHISPER_COMPUTE,
                              cpu_threads=config.WHISPER_THREADS,
                              download_root=str(config.MODEL_DIR))
    return _model


def transcribe(path: str, duration: float, language: str | None = "es",
               translate: bool = False,
               on_progress: Callable[[float, str], None] | None = None) -> dict:
    cb = on_progress or (lambda f, m: None)
    model = get_model()
    lang = None if language in (None, "", "auto") else language
    segments, info = model.transcribe(
        path, language=lang, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5, condition_on_previous_text=False)
    words = []
    span = 0.85 if translate else 1.0
    for seg in segments:
        for w in seg.words or []:
            txt = w.word.strip()
            if txt:
                words.append({"w": txt, "s": round(w.start, 3), "e": round(w.end, 3),
                              "p": round(w.probability, 3)})
        if duration > 0:
            cb(span * min(1.0, seg.end / duration), "Transcribiendo")
    translated = None
    if translate:
        cb(0.86, "Traduciendo al inglés")
        segs, _ = model.transcribe(path, language=info.language, task="translate",
                                   vad_filter=True, beam_size=5)
        translated = [{"s": round(s.start, 3), "e": round(s.end, 3), "text": s.text.strip()}
                      for s in segs if s.text.strip()]
    cb(1.0, "Listo")
    return {"language": info.language, "words": words, "translated": translated}
