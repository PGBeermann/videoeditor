"""Subtítulos: proyección de la transcripción a la línea de tiempo y exportación SRT/VTT/ASS.

Criterios de segmentación (buenas prácticas de subtitulado, p. ej. guías BBC y Netflix):
  * máximo ~42 caracteres por línea, una línea por cue
  * duración entre 1 y 6 segundos
  * corte en puntuación final o pausas > 0,7 s
"""
from __future__ import annotations

MAX_CHARS = 42
MAX_DUR = 6.0
MIN_DUR = 1.0
PAUSE = 0.7


def clip_duration(clip: dict) -> float:
    return max(0.0, float(clip["out"]) - float(clip["in"]))


def timeline_duration(project: dict) -> float:
    return sum(clip_duration(c) for c in project.get("clips", []))


def timeline_words(project: dict, transcripts: dict[str, list]) -> list[dict]:
    """Palabras de cada clip, con tiempos en la línea de tiempo final."""
    out, t0 = [], 0.0
    for clip in project.get("clips", []):
        dur = clip_duration(clip)
        cin, cout = float(clip["in"]), float(clip["out"])
        for w in transcripts.get(clip.get("media_id"), []) or []:
            mid = (w["s"] + w["e"]) / 2
            if cin <= mid <= cout:
                s = t0 + max(0.0, w["s"] - cin)
                e = t0 + min(dur, w["e"] - cin)
                out.append({"w": w["w"], "s": round(s, 3), "e": round(max(e, s + 0.05), 3)})
        t0 += dur
    return out


def timeline_segments(project: dict, segments_by_media: dict[str, list]) -> list[dict]:
    """Igual que timeline_words, pero para segmentos (p. ej. traducción al inglés)."""
    out, t0 = [], 0.0
    for clip in project.get("clips", []):
        dur = clip_duration(clip)
        cin, cout = float(clip["in"]), float(clip["out"])
        for sg in segments_by_media.get(clip.get("media_id"), []) or []:
            s, e = max(sg["s"], cin), min(sg["e"], cout)
            if e - s > 0.3:
                out.append({"text": sg["text"].strip(), "s": round(t0 + s - cin, 3),
                            "e": round(t0 + e - cin, 3)})
        t0 += dur
    return out


def build_cues(words: list[dict]) -> list[dict]:
    cues, cur = [], []

    def flush():
        if cur:
            cues.append({"s": cur[0]["s"], "e": cur[-1]["e"],
                         "text": " ".join(w["w"].strip() for w in cur).strip()})
            cur.clear()

    for i, w in enumerate(words):
        if cur:
            text_len = len(" ".join(x["w"].strip() for x in cur)) + 1 + len(w["w"].strip())
            gap = w["s"] - cur[-1]["e"]
            if text_len > MAX_CHARS or gap > PAUSE or w["e"] - cur[0]["s"] > MAX_DUR:
                flush()
        cur.append(w)
        if w["w"].strip()[-1:] in ".?!…":
            flush()
    flush()
    # duración mínima sin solaparse con el siguiente
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["s"] if i + 1 < len(cues) else c["e"] + MIN_DUR
        if c["e"] - c["s"] < MIN_DUR:
            c["e"] = min(c["s"] + MIN_DUR, nxt - 0.04)
        c["e"] = max(c["e"], c["s"] + 0.2)
    return [c for c in cues if c["text"]]


def segments_to_cues(segs: list[dict]) -> list[dict]:
    """Divide segmentos largos en cues de ≤42 caracteres repartiendo el tiempo."""
    cues = []
    for sg in segs:
        words = sg["text"].split()
        if not words:
            continue
        lines, cur = [], ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > MAX_CHARS:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        lines.append(cur)
        total = sum(len(x) for x in lines) or 1
        t = sg["s"]
        for ln in lines:
            d = (sg["e"] - sg["s"]) * len(ln) / total
            cues.append({"s": round(t, 3), "e": round(t + d, 3), "text": ln})
            t += d
    return cues


def _ts(t: float, sep: str) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(round(t * 1000)), 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues: list[dict]) -> str:
    return "\n".join(f"{i}\n{_ts(c['s'], ',')} --> {_ts(c['e'], ',')}\n{c['text']}\n"
                     for i, c in enumerate(cues, 1))


def to_vtt(cues: list[dict]) -> str:
    body = "\n".join(f"{_ts(c['s'], '.')} --> {_ts(c['e'], '.')}\n{c['text']}\n" for c in cues)
    return "WEBVTT\n\n" + body


def chapters_vtt(chapters: list[dict], total: float) -> str:
    chs = sorted(chapters, key=lambda c: c["t"])
    parts = ["WEBVTT\n"]
    for i, c in enumerate(chs):
        end = chs[i + 1]["t"] if i + 1 < len(chs) else total
        parts.append(f"{_ts(c['t'], '.')} --> {_ts(end, '.')}\n{c['title']}\n")
    return "\n".join(parts)


def chapters_ffmetadata(chapters: list[dict], total: float, title: str) -> str:
    def esc(s: str) -> str:
        return "".join("\\" + ch if ch in "=;#\\\n" else ch for ch in s)
    chs = sorted([c for c in chapters if 0 <= c["t"] < total], key=lambda c: c["t"])
    out = [";FFMETADATA1", f"title={esc(title)}"]
    for i, c in enumerate(chs):
        end = chs[i + 1]["t"] if i + 1 < len(chs) else total
        out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(c['t'] * 1000)}",
                f"END={int(end * 1000)}", f"title={esc(c['title'])}"]
    return "\n".join(out) + "\n"


def to_ass(cues: list[dict], width: int, height: int, style: dict | None = None) -> str:
    style = style or {}
    vertical = height > width
    size = int(round((width * 0.058) if vertical else (height * 0.045)))
    margin_v = int(height * (0.16 if vertical else 0.06))
    margin_h = int(width * 0.06)
    color = style.get("color", "#FFFFFF").lstrip("#")
    rr, gg, bb = color[0:2], color[2:4], color[4:6]
    primary = f"&H00{bb}{gg}{rr}"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{size},{primary},&H000000FF,&H64000000,&H64000000,1,0,0,0,100,100,0,0,3,{max(2, size // 6)},0,2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def at(t: float) -> str:
        cs = int(round(max(0, t) * 100))
        h, rem = divmod(cs, 360000)
        m, rem = divmod(rem, 6000)
        s, c = divmod(rem, 100)
        return f"{h}:{m:02d}:{s:02d}.{c:02d}"

    lines = []
    for c in cues:
        txt = c["text"].replace("\\", "\\\\").replace("{", "(").replace("}", ")")
        txt = txt.replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{at(c['s'])},{at(c['e'])},Default,,0,0,0,,{txt}")
    return header + "\n".join(lines) + "\n"


# Muletillas frecuentes del español académico oral (solo se sugieren; el docente decide)
FILLERS = {"eh", "ehh", "ehhh", "em", "emm", "mm", "mmm", "hmm", "ah", "ahh", "este", "esteee", "eeh"}


def is_filler(word: str) -> bool:
    w = word.strip().lower().strip(".,;:¿?¡!…\"'")
    return w in FILLERS
