"""Detección de silencios con FFmpeg (silencedetect).

Uso: python -m tests.test_silences   (requiere FFmpeg en el sistema)
"""
import subprocess
import tempfile
from pathlib import Path

from app.media import detect_silences


def main():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "voz.m4a"
        # tono 2 s · silencio 1,5 s · tono 1 s · ruido muy bajo 0,4 s · tono 2 s · silencio 3 s
        subprocess.run(["ffmpeg", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "sine=f=440:d=2",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1.5",
                        "-f", "lavfi", "-i", "sine=f=300:d=1",
                        "-f", "lavfi", "-i", "anoisesrc=a=0.002:d=0.4",
                        "-f", "lavfi", "-i", "sine=f=500:d=2",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
                        "-filter_complex", "[0][1][2][3][4][5]concat=n=6:v=0:a=1,aresample=44100[a]",
                        "-map", "[a]", "-c:a", "aac", str(src)], check=True)
        short = detect_silences(src, -35, 0.3)
        assert len(short) == 3, short
        long_ = detect_silences(src, -35, 0.7, Path(tmp), "m1")
        assert len(long_) == 2, long_
        (a, b), (c, d) = long_
        assert abs(a - 2.0) < 0.05 and abs(b - 3.5) < 0.05, long_
        assert abs(c - 6.9) < 0.05 and d > 9.8, long_          # silencio final hasta el término
        assert (Path(tmp) / "m1_sil_-35_0.7.json").exists()
        assert detect_silences(src, -35, 0.7, Path(tmp), "m1") == long_   # desde la caché
    print("Silencios OK ·", long_)


if __name__ == "__main__":
    main()
