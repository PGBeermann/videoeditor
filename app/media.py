"""Utilidades de FFmpeg: análisis de medios, proxies, forma de onda y ejecución con progreso."""
import json
import subprocess
from pathlib import Path
from typing import Callable

from .config import PROXY_DIR

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma"}


class FFmpegError(RuntimeError):
    pass


def probe(path: str | Path) -> dict:
    """Devuelve tipo, duración, dimensiones, fps y si tiene audio."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise FFmpegError(f"No se pudo analizar el archivo: {out.stderr.strip()[:300]}")
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    ext = Path(path).suffix.lower()
    dur = float(info.get("format", {}).get("duration") or 0)
    if v is None and a is None:
        raise FFmpegError("El archivo no contiene video ni audio reconocible.")
    if ext in IMAGE_EXT or (v is not None and dur <= 0.05 and a is None):
        kind = "image"
    elif v is None:
        kind = "audio"
    else:
        kind = "video"
    fps = 0.0
    if v is not None:
        for key in ("avg_frame_rate", "r_frame_rate"):
            num, _, den = (v.get(key) or "0/1").partition("/")
            try:
                f = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                f = 0
            if 1 <= f <= 240:
                fps = f
                break
    w = int(v.get("width") or 0) if v else 0
    h = int(v.get("height") or 0) if v else 0
    rot = 0
    if v:
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = abs(int(float(sd["rotation"]))) % 180
        if rot == 90:
            w, h = h, w
    if kind == "video" and dur <= 0:
        # Grabaciones WebM de MediaRecorder a veces no traen duración en el contenedor
        dur = _decode_duration(path)
    return {"kind": kind, "duration": round(dur, 3), "width": w, "height": h,
            "fps": round(fps, 3), "has_audio": a is not None}


def _decode_duration(path) -> float:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null",
                          "-progress", "pipe:1", "-"],
                         capture_output=True, text=True, timeout=3600)
    t = 0.0
    for line in out.stdout.splitlines():
        if line.startswith("out_time_us="):
            try:
                t = int(line.split("=")[1]) / 1e6
            except ValueError:
                pass
    return t


def run_ffmpeg(args: list[str], duration: float = 0,
               on_progress: Callable[[float], None] | None = None,
               timeout: float | None = None) -> None:
    """Ejecuta ffmpeg (sin shell) e informa progreso 0..1 leyendo -progress."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    last = -1.0
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_progress and duration > 0 and line.startswith("out_time_us="):
            try:
                frac = min(1.0, int(line.split("=")[1]) / 1e6 / duration)
            except ValueError:
                continue
            if frac - last >= 0.01:
                last = frac
                on_progress(frac)
    proc.wait(timeout=timeout)
    err = proc.stderr.read() if proc.stderr else ""
    if proc.returncode != 0:
        raise FFmpegError(err.strip()[-1500:] or f"ffmpeg terminó con código {proc.returncode}")


def make_proxy(media_id: str, src: str, info: dict,
               on_progress: Callable[[float], None] | None = None) -> str:
    """Proxy liviano para el navegador: H.264 ≤720p, GOP de 1 s (búsqueda ágil)."""
    if info["kind"] == "audio":
        dst = PROXY_DIR / f"{media_id}.m4a"
        run_ffmpeg(["-i", src, "-vn", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", str(dst)], info["duration"], on_progress)
        return str(dst)
    if info["kind"] == "image":
        dst = PROXY_DIR / f"{media_id}.png"
        run_ffmpeg(["-i", src, "-frames:v", "1",
                    "-vf", "scale='min(1920,iw)':-2", str(dst)])
        return str(dst)
    dst = PROXY_DIR / f"{media_id}.mp4"
    fps = info["fps"] or 30
    gop = max(1, round(fps))
    args = ["-i", src, "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale=-2:'min(720,ih)',format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart", str(dst)]
    run_ffmpeg(args, info["duration"], on_progress)
    return str(dst)


def make_waveform(media_id: str, src: str) -> str | None:
    dst = PROXY_DIR / f"{media_id}_wave.png"
    try:
        run_ffmpeg(["-i", src, "-filter_complex",
                    "aformat=channel_layouts=mono,showwavespic=s=2000x120:colors=#7fb069",
                    "-frames:v", "1", str(dst)], timeout=1800)
        return str(dst)
    except FFmpegError:
        return None
