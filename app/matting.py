"""Quitar el fondo de un video (recorte de la persona) con Robust Video Matting.

Modelo: RVM MobileNetV3 (Lin, Yang, Saleemi y Sengupta, «Robust High-Resolution Video
Matting with Temporal Guidance», WACV 2022; https://github.com/PeterL1n/RobustVideoMatting).
Es recurrente: conserva memoria entre cuadros, de modo que el borde no parpadea como
ocurre al segmentar cada cuadro por separado. Se ejecuta con onnxruntime en CPU.

Flujo: FFmpeg decodifica a RGB → el modelo estima primer plano y alfa por cuadro →
FFmpeg recibe RGBA y, según el modo, lo guarda transparente (WebM VP9 con alfa) o lo
compone sobre un color, una imagen o el propio fondo desenfocado (MP4 H.264).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np

from .config import MODEL_DIR
from .media import FFmpegError, audio_filter

MODEL_URL = ("https://github.com/PeterL1n/RobustVideoMatting/releases/download/"
             "v1.0.0/rvm_mobilenetv3_fp32.onnx")
MODEL_SHA256 = "88d4531297118f595bf2fd60f6f566aec2e559393802d1f436c380f0cbbd2828"
MODEL_PATH = MODEL_DIR / "rvm" / "rvm_mobilenetv3_fp32.onnx"

# Resolución interna del modelo (lado mayor, px). Los autores recomiendan que la imagen
# reducida quede alrededor de 512 px (p. ej., 0,25 para 1920×1080 en retratos).
QUALITY_PX = {"normal": 512, "rapida": 320}

_lock = threading.Lock()


def ensure_model(on_msg: Callable[[str], None] | None = None) -> Path:
    """Descarga el modelo la primera vez (15 MB) y verifica su SHA-256."""
    with _lock:
        if MODEL_PATH.exists():
            return MODEL_PATH
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        if on_msg:
            on_msg("Descargando el modelo de recorte (solo la primera vez, 15 MB)")
        tmp = MODEL_PATH.with_suffix(".part")
        with urllib.request.urlopen(MODEL_URL, timeout=120) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if digest != MODEL_SHA256:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("El modelo descargado no coincide con la huella esperada")
        tmp.replace(MODEL_PATH)
        return MODEL_PATH


def _session():
    import onnxruntime as ort
    so = ort.SessionOptions()
    n = int(os.environ.get("MATTING_THREADS", "0"))
    if n > 0:
        so.intra_op_num_threads = n
    return ort.InferenceSession(str(ensure_model()), so, providers=["CPUExecutionProvider"])


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def remove_background(src: str, info: dict, dst_base: Path, mode: str = "transparent",
                      color: str = "#ffffff", image: str | None = None, blur: int = 25,
                      max_height: int = 720, quality: str = "normal",
                      on_progress: Callable[[float, str], None] | None = None) -> Path:
    """Genera el video sin fondo. Devuelve la ruta creada (.webm si es transparente, .mp4 si no).

    mode: transparent | color | blur | image
    """
    if info.get("kind") != "video":
        raise ValueError("Solo se puede quitar el fondo de un video")
    prog = on_progress or (lambda f, m="": None)
    prog(0.01, "Preparando el modelo de recorte")
    sess = _session()

    sw, sh = int(info["width"]), int(info["height"])
    H = _even(min(sh, max_height))
    W = _even(sw * H / sh)
    fps = info.get("fps") or 30
    fps = min(fps, 60)
    dur = float(info.get("duration") or 0)
    total = max(1, int(dur * fps))
    dr = min(1.0, QUALITY_PX.get(quality, 512) / max(W, H))

    dec = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src, "-map", "0:v:0",
         "-vf", f"fps={fps:g},scale={W}:{H}:flags=bicubic,format=rgb24",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # --- Codificación (y composición del fondo nuevo) ---
    enc = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-r", f"{fps:g}", "-i", "-"]
    n_in = 1
    fl, vout = [], "0:v"
    if mode == "color":
        c = color.lstrip("#") if color else "ffffff"
        fl.append(f"color=c=0x{c}:s={W}x{H}:r={fps:g}[bg]")
        fl.append("[bg][0:v]overlay=shortest=1:format=auto[v]")
        vout = "v"
    elif mode == "blur":
        enc += ["-i", src]
        b = max(5, min(60, int(blur)))
        fl.append(f"[1:v]fps={fps:g},scale={W}:{H},boxblur={b}:2[bg]")
        fl.append("[bg][0:v]overlay=shortest=1:format=auto[v]")
        vout, n_in = "v", 2
    elif mode == "image" and image:
        enc += ["-loop", "1", "-framerate", f"{fps:g}", "-i", image]
        fl.append(f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[bg]")
        fl.append("[bg][0:v]overlay=shortest=1:format=auto[v]")
        vout, n_in = "v", 2
    elif mode != "transparent":
        raise ValueError("Modo de fondo no válido")

    # Audio original (las pistas con sonido elegidas en la ingesta)
    amap = info.get("audio_map") or ([0] if info.get("has_audio") else [])
    a_args: list[str] = []
    if amap:
        enc += ["-i", src]
        ai = n_in
        mix = audio_filter(ai, amap, "a")
        if mix:
            fl.append(mix)
            a_args = ["-map", "[a]"]
        else:
            a_args = ["-map", f"{ai}:a:{amap[0]}"]
    if fl:
        enc += ["-filter_complex", ";".join(fl)]
    enc += ["-map", f"[{vout}]" if vout != "0:v" else "0:v", *a_args]

    if mode == "transparent":
        dst = dst_base.with_suffix(".webm")
        enc += ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "30",
                "-deadline", "good", "-cpu-used", "4", "-row-mt", "1", "-auto-alt-ref", "0",
                "-g", str(max(1, round(fps)))]
        if a_args:
            enc += ["-c:a", "libopus", "-b:a", "128k"]
    else:
        dst = dst_base.with_suffix(".mp4")
        enc += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-g", str(max(1, round(fps))), "-movflags", "+faststart"]
        if a_args:
            enc += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    enc += ["-shortest", str(dst)]
    encp = subprocess.Popen(enc, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    # El error estándar se drena en hilos para que FFmpeg no se bloquee
    errs = {"dec": b"", "enc": b""}

    def drain(name, pipe):
        errs[name] = pipe.read()

    ts = [threading.Thread(target=drain, args=("dec", dec.stderr), daemon=True),
          threading.Thread(target=drain, args=("enc", encp.stderr), daemon=True)]
    for t in ts:
        t.start()

    frame_bytes = W * H * 3
    rec = [np.zeros((1, 1, 1, 1), np.float32)] * 4
    ratio = np.array([dr], np.float32)
    rgba = np.empty((H, W, 4), np.uint8)
    k = 0
    try:
        assert dec.stdout is not None and encp.stdin is not None
        while True:
            buf = dec.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            img = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
            x = (img.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
            fgr, pha, *rec = sess.run(None, {"src": x, "r1i": rec[0], "r2i": rec[1],
                                             "r3i": rec[2], "r4i": rec[3],
                                             "downsample_ratio": ratio})
            rgba[..., :3] = np.clip(fgr[0].transpose(1, 2, 0) * 255.0 + 0.5, 0, 255)
            rgba[..., 3] = np.clip(pha[0, 0] * 255.0 + 0.5, 0, 255)
            encp.stdin.write(rgba.tobytes())
            k += 1
            if k % 10 == 0:
                prog(0.03 + 0.95 * min(1.0, k / total), f"Quitando el fondo: cuadro {k} de ~{total}")
        encp.stdin.close()
        encp.wait(timeout=3600)
        dec.wait(timeout=60)
    except BaseException:
        dec.kill()
        encp.kill()
        dst.unlink(missing_ok=True)
        raise
    for t in ts:
        t.join(timeout=5)
    if k == 0:
        dst.unlink(missing_ok=True)
        raise FFmpegError("No se pudo leer el video: " + errs["dec"].decode(errors="replace")[-500:])
    if encp.returncode != 0:
        dst.unlink(missing_ok=True)
        raise FFmpegError(errs["enc"].decode(errors="replace").strip()[-800:] or "Falló la codificación")
    prog(0.99, "Guardando")
    return dst
