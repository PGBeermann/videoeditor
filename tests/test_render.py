"""Prueba de integración del motor de render con medios sintéticos.

Uso:  DATA_DIR=/tmp/ev python -m tests.test_render /ruta/a/medios_de_prueba
Requiere en esa carpeta: clip.mp4 (video+audio), music.mp3, ov.png, img.jpg
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from app import media
from app.render import Renderer


def main(src: str) -> None:
    d = Path(src)
    m = {}
    for mid, name in (("v1", "clip.mp4"), ("mu", "music.mp3"), ("im", "img.jpg")):
        info = media.probe(d / name)
        m[mid] = {**info, "path": str(d / name)}
    assert m["v1"]["kind"] == "video" and m["mu"]["kind"] == "audio" and m["im"]["kind"] == "image"
    project = {
        "settings": {"width": 1920, "height": 1080, "fps": 30},
        "clips": [{"id": "c1", "media_id": "v1", "in": 1, "out": 5},
                  {"id": "c2", "media_id": "im", "in": 0, "out": 2},
                  {"id": "c3", "media_id": "v1", "in": 10, "out": 14, "fit": "cover"}],
        "overlays": [{"id": "o1", "asset_id": "a1", "start": 0.5, "end": 4, "x": 0.1, "y": 0.1, "w": 0.3}],
        "zooms": [{"id": "z1", "start": 6.5, "end": 9.5, "x": 0.3, "y": 0.3, "scale": 2}],
        "music": [{"id": "m1", "media_id": "mu", "start": 0, "volume": 0.3, "duck": True}],
        "chapters": [{"t": 0, "title": "Introducción"}, {"t": 6, "title": "Desarrollo"}],
        "audio": {"denoise": "light", "loudness": -16},
        "captions": {"burn": True, "soft": True},
        "brand": {"logo_asset_id": "a1", "position": "br"},
    }
    transcripts = {"v1": [{"w": "Hola", "s": 1.2, "e": 1.6}, {"w": "estudiantes,", "s": 1.7, "e": 2.4},
                          {"w": "hoy", "s": 2.5, "e": 2.8}, {"w": "veremos", "s": 2.9, "e": 3.3},
                          {"w": "alcanos.", "s": 3.4, "e": 4.0}, {"w": "Fin.", "s": 11, "e": 11.5}]}
    for fmt in ("16:9", "9:16"):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            r = Renderer(project, m, {"a1": str(d / "ov.png")}, transcripts, {}, wd,
                         {"format": fmt}, lambda f, msg: None)
            res = r.run(wd / "out", "Prueba")
            out = res["files"]["mp4"]
            info = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                 "-show_streams", "-show_chapters", out], capture_output=True, text=True).stdout)
            v = next(s for s in info["streams"] if s["codec_type"] == "video")
            dur = float(info["format"]["duration"])
            print(fmt, v["width"], v["height"], round(dur, 2), "capítulos:",
                  len(info["chapters"]), "pistas:", [s["codec_type"] for s in info["streams"]],
                  sorted(res["files"]))
            assert abs(dur - 10.0) < 0.15, dur
            assert len(info["chapters"]) == 2
            loud = subprocess.run(["ffmpeg", "-hide_banner", "-i", out, "-af", "ebur128=framelog=quiet",
                                   "-f", "null", "-"], capture_output=True, text=True).stderr
            print("   ", [ln.strip() for ln in loud.splitlines() if ln.strip().startswith("I:")][-1:])
            subprocess.run(["cp", out, f"/tmp/claude-0/t/out_{fmt.replace(':', 'x')}.mp4"])
    print("OK")


if __name__ == "__main__":
    main(sys.argv[1])
