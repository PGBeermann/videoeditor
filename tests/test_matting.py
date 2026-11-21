"""Quitar el fondo (Robust Video Matting): los cuatro modos producen un video válido.

Uso: DATA_DIR=/tmp/prueba python -m tests.test_matting
(la primera vez descarga el modelo, 15 MB, a DATA_DIR/models/rvm)
"""
import json
import subprocess
import tempfile
from pathlib import Path

from app import matting, media


def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,pix_fmt:stream_tags=alpha_mode",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src, img = tmp / "cam.mp4", tmp / "fondo.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2",
                        "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(src)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x200",
                        "-frames:v", "1", str(img)], check=True)
        info = media.probe(src)
        info["audio_map"] = [0]
        for mode in ("transparent", "color", "blur", "image"):
            out = matting.remove_background(str(src), info, tmp / f"o_{mode}", mode=mode,
                                            color="#4a7729", image=str(img), quality="rapida")
            st = streams(out)
            v = next(s for s in st if s["codec_type"] == "video")
            assert any(s["codec_type"] == "audio" for s in st), (mode, st)
            if mode == "transparent":
                assert out.suffix == ".webm" and v["codec_name"] == "vp9"
                assert v.get("tags", {}).get("alpha_mode") == "1", v
            else:
                assert out.suffix == ".mp4" and v["codec_name"] == "h264", v
            assert abs(media.probe(out)["duration"] - 2) < 0.2, mode
    print("Quitar fondo OK · 4 modos")


if __name__ == "__main__":
    main()
