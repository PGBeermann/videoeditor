"""Transcripción con faster-whisper (implementación CTranslate2 de Whisper, licencia MIT).

Produce marcas de tiempo por palabra (necesarias para editar el video borrando texto)
y, opcionalmente, una traducción al inglés (tarea «translate» nativa de Whisper).
"""
from __future__ import annotations

import subprocess
from typing import Callable

import numpy as np

from . import config

SAMPLE_RATE = 16000   # Whisper trabaja con audio mono a 16 kHz


def load_audio(path: str) -> np.ndarray:
    """Audio mono 16 kHz float32 decodificado con FFmpeg.

    Se evita el lector interno de faster-whisper (PyAV): las versiones ≥ 15 de PyAV
    eliminaron el argumento «metadata_errors» que faster-whisper 1.2 todavía usa
    (error «open() got an unexpected keyword argument 'metadata_errors'»).
    """
    out = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
                          "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                          "-f", "f32le", "-"], capture_output=True, timeout=3600)
    if out.returncode != 0:
        raise RuntimeError("No se pudo leer el audio: " + out.stderr.decode(errors="replace")[-400:])
    return np.frombuffer(out.stdout, np.float32).copy()

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
    audio = load_audio(path)
    if audio.size < SAMPLE_RATE // 4:
        raise RuntimeError("El medio no tiene audio suficiente para transcribir")
    segments, info = model.transcribe(
        audio, language=lang, word_timestamps=True, vad_filter=True,
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
        segs, _ = model.transcribe(audio, language=info.language, task="translate",
                                   vad_filter=True, beam_size=5)
        translated = [{"s": round(s.start, 3), "e": round(s.end, 3), "text": s.text.strip()}
                      for s in segs if s.text.strip()]
    cb(1.0, "Listo")
    return {"language": info.language, "words": words, "translated": translated}
