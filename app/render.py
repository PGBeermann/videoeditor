"""Motor de render en tres etapas, todo con FFmpeg.

Etapa A  Ensambla los clips de la secuencia principal (cortes) en tramos de hasta
         40 clips, normalizados a la resolución/fps del proyecto, y los concatena
         en una «base» intermedia de alta calidad.
Etapa B  Composición: zoom animado, capas (texto, LaTeX, química, moléculas,
         imágenes, flechas), logo institucional, reencuadre a 9:16 o 1:1,
         subtítulos incrustados; cadena de audio (filtro paso alto, reducción de
         ruido, de-esser, compresor) y música con atenuación automática (ducking).
Etapa C  Normalización de sonoridad EBU R128 en dos pasadas (loudnorm, ITU-R
         BS.1770) y multiplexado final MP4 con capítulos y subtítulos opcionales.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import captions, config
from .media import FFmpegError, audio_filter, run_ffmpeg

CHUNK = 40
FORMATS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
RNNOISE_MODEL = Path(__file__).resolve().parent.parent / "models" / "rnnoise" / "sh.rnnn"


def _f(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".") if "." in f"{x:.4f}" else str(x)


class Renderer:
    def __init__(self, project: dict, media: dict[str, dict], assets: dict[str, str],
                 transcripts: dict[str, list], translations: dict[str, list],
                 workdir: Path, options: dict,
                 on_progress: Callable[[float, str], None] | None = None):
        self.p = project
        self.media = media              # media_id -> fila (path, kind, has_audio...)
        self.assets = assets            # asset_id -> ruta PNG
        self.transcripts = transcripts
        self.translations = translations
        self.wd = workdir
        self.opt = options
        self.cb = on_progress or (lambda f, m: None)
        st = project.get("settings", {})
        self.W0 = int(st.get("width", 1920)) // 2 * 2
        self.H0 = int(st.get("height", 1080)) // 2 * 2
        self.fps = float(st.get("fps", 30)) or 30
        fmt = options.get("format", "16:9")
        if fmt == "proyecto" or fmt not in FORMATS:
            self.W, self.H = self.W0, self.H0
        else:
            self.W, self.H = FORMATS[fmt]
        if options.get("height") and fmt in ("16:9", "proyecto"):
            # p. ej. exportación 720p para conexiones lentas
            h = int(options["height"]) // 2 * 2
            self.W, self.H = int(round(self.W * h / self.H)) // 2 * 2, h
        self.clips = [c for c in project.get("clips", [])
                      if captions.clip_duration(c) >= 1 / self.fps and c.get("media_id") in media]
        self.T = sum(captions.clip_duration(c) for c in self.clips)
        if not self.clips or self.T <= 0:
            raise ValueError("La secuencia principal está vacía: agregue al menos un clip.")

    # ------------------------------------------------------------------ Etapa A
    def _chunk_cmd(self, clips: list[dict], out: Path) -> list[str]:
        args, fl, labels = [], [], []
        idx = 0
        W0, H0, fps = self.W0, self.H0, self.fps
        for c in clips:
            m = self.media[c["media_id"]]
            dur = captions.clip_duration(c)
            if m["kind"] == "image":
                args += ["-loop", "1", "-framerate", _f(fps), "-t", _f(dur), "-i", m["path"]]
            else:
                args += ["-ss", _f(float(c["in"])), "-t", _f(dur), "-i", m["path"]]
            vi = idx
            idx += 1
            fit = c.get("fit", "contain")
            if fit == "cover":
                scale = (f"scale={W0}:{H0}:force_original_aspect_ratio=increase,"
                         f"crop={W0}:{H0}")
            else:
                scale = (f"scale={W0}:{H0}:force_original_aspect_ratio=decrease,"
                         f"pad={W0}:{H0}:(ow-iw)/2:(oh-ih)/2:color=black")
            fl.append(f"[{vi}:v]{scale},setsar=1,fps={_f(fps)},format=yuv420p,"
                      f"tpad=stop_mode=clone:stop_duration=1,trim=duration={_f(dur)},"
                      f"setpts=PTS-STARTPTS[v{vi}]")
            if m["kind"] == "video" and m["has_audio"] and not c.get("mute"):
                gain = float(c.get("gain_db", 0) or 0)
                fade = min(0.012, dur / 4)
                amap = m.get("audio_map") or [0]
                mix = audio_filter(vi, amap, f"am{vi}")
                if mix:
                    fl.append(mix)
                    a_in = f"[am{vi}]"
                else:
                    a_in = f"[{vi}:a:{amap[0]}]"
                fl.append(f"{a_in}aformat=sample_rates=48000:channel_layouts=stereo,"
                          f"volume={_f(gain)}dB,apad,atrim=0:{_f(dur)},asetpts=PTS-STARTPTS,"
                          f"afade=t=in:d={_f(fade)},afade=t=out:st={_f(dur - fade)}:d={_f(fade)}"
                          f"[a{vi}]")
                labels.append(f"[v{vi}][a{vi}]")
            else:
                args += ["-f", "lavfi", "-t", _f(dur), "-i",
                         "anullsrc=channel_layout=stereo:sample_rate=48000"]
                ai = idx
                idx += 1
                fl.append(f"[{ai}:a]atrim=0:{_f(dur)},asetpts=PTS-STARTPTS[a{vi}]")
                labels.append(f"[v{vi}][a{vi}]")
        fl.append(f"{''.join(labels)}concat=n={len(clips)}:v=1:a=1[vout][aout]")
        return args + ["-filter_complex", ";".join(fl), "-map", "[vout]", "-map", "[aout]",
                       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10",
                       "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-ar", "48000",
                       str(out)]

    def stage_a(self) -> Path:
        chunks = [self.clips[i:i + CHUNK] for i in range(0, len(self.clips), CHUNK)]
        parts, done = [], 0.0
        for k, ch in enumerate(chunks):
            out = self.wd / f"chunk_{k:03d}.mkv"
            dur = sum(captions.clip_duration(c) for c in ch)
            base = done
            run_ffmpeg(self._chunk_cmd(ch, out), dur,
                       lambda f, b=base, d=dur: self.cb(0.35 * (b + f * d) / self.T,
                                                        "Ensamblando cortes"))
            done += dur
            parts.append(out)
        if len(parts) == 1:
            return parts[0]
        lst = self.wd / "concat.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        base = self.wd / "base.mkv"
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(base)])
        return base

    # ------------------------------------------------------------------ Etapa B
    def _zoom_filter(self) -> str | None:
        zooms = sorted([z for z in self.p.get("zooms", [])
                        if float(z["end"]) > float(z["start"]) and float(z.get("scale", 1)) > 1.01],
                       key=lambda z: z["start"])
        if not zooms:
            return None
        ramp = 0.45
        zs, cxs, cys = [], [], []
        for z in zooms:
            s, e = float(z["start"]), float(z["end"])
            r = min(ramp, (e - s) / 2)
            # rampa suave (smoothstep) de entrada y salida, en segundos de la línea de tiempo
            u_in = f"clip((T-{_f(s)})/{_f(r)},0,1)"
            u_out = f"clip(({_f(e)}-T)/{_f(r)},0,1)"
            k = f"(min({u_in},{u_out}))"
            k = f"({k}*{k}*(3-2*{k}))"
            zs.append(f"{_f(float(z['scale']) - 1)}*{k}")
            cxs.append((float(z.get("x", 0.5)), k))
            cys.append((float(z.get("y", 0.5)), k))
        T = f"(in/{_f(self.fps)})"
        zexpr = ("1+" + "+".join(zs)).replace("T", T)
        ksum = "+".join(k for _, k in cxs)
        cx = "(" + "+".join(f"{_f(x)}*{k}" for x, k in cxs) + f"+0.5*(1-min(1,{ksum})))"
        cy = "(" + "+".join(f"{_f(y)}*{k}" for y, k in cys) + f"+0.5*(1-min(1,{ksum})))"
        cx, cy = cx.replace("T", T), cy.replace("T", T)
        x = f"max(0,min(iw-iw/zoom,{cx}*iw-iw/zoom/2))"
        y = f"max(0,min(ih-ih/zoom,{cy}*ih-ih/zoom/2))"
        # zoompan sobre una versión al doble de resolución reduce el «temblor» de redondeo
        return (f"scale={self.W0 * 2}:{self.H0 * 2}:flags=bicubic,"
                f"zoompan=z='{zexpr}':x='{x}':y='{y}':d=1:s={self.W0}x{self.H0}:fps={_f(self.fps)}")

    def _audio_chain(self) -> str:
        a = self.p.get("audio", {})
        f = []
        if a.get("highpass", True):
            f.append("highpass=f=80")
        dn = a.get("denoise", "light")
        if dn == "strong" and RNNOISE_MODEL.exists():
            f.append(f"arnndn=m={RNNOISE_MODEL}:mix=0.85")
        elif dn == "strong":
            f.append("afftdn=nr=24:nf=-40:tn=1")
        elif dn == "light":
            f.append("afftdn=nr=10:nf=-50:tn=1")
        if a.get("deess", True):
            f.append("deesser=i=0.35:m=0.5:f=0.5")
        if a.get("compress", True):
            f.append("acompressor=threshold=-20dB:ratio=3:attack=10:release=180:makeup=2")
        g = float(a.get("voice_gain_db", 0) or 0)
        if g:
            f.append(f"volume={_f(g)}dB")
        return ",".join(f) if f else "anull"

    def build_cues(self) -> list[dict]:
        words = captions.timeline_words(self.p, self.transcripts)
        return captions.build_cues(words)

    def stage_b(self, base: Path, cues: list[dict]) -> Path:
        W0, H0, W, H, T, fps = self.W0, self.H0, self.W, self.H, self.T, self.fps
        args = ["-i", str(base)]
        n = 1
        fl = []
        cur = "0:v"

        z = self._zoom_filter()
        if z:
            fl.append(f"[{cur}]{z}[vz]")
            cur = "vz"

        # Capas gráficas (renderizadas como PNG en el navegador: lo que se ve es lo que se exporta)
        for i, ov in enumerate(sorted(self.p.get("overlays", []), key=lambda o: o.get("track", 0))):
            path = self.assets.get(ov.get("asset_id"))
            s, e = max(0.0, float(ov["start"])), min(T, float(ov["end"]))
            if not path or e - s <= 0.04:
                continue
            args += ["-framerate", _f(fps), "-loop", "1", "-t", _f(T), "-i", path]
            pw = max(2, int(round(float(ov.get("w", 0.3)) * W0)) // 2 * 2)
            x = int(round(float(ov.get("x", 0)) * W0))
            y = int(round(float(ov.get("y", 0)) * H0))
            fade = min(float(ov.get("fade", 0.25)), (e - s) / 3)
            chain = f"[{n}:v]scale={pw}:-2:flags=lanczos,format=rgba"
            op = float(ov.get("opacity", 1))
            if op < 0.999:
                chain += f",colorchannelmixer=aa={_f(op)}"
            if fade > 0.01:
                chain += (f",fade=t=in:st={_f(s)}:d={_f(fade)}:alpha=1,"
                          f"fade=t=out:st={_f(e - fade)}:d={_f(fade)}:alpha=1")
            fl.append(f"{chain}[ov{i}]")
            fl.append(f"[{cur}][ov{i}]overlay=x={x}:y={y}:enable='between(t,{_f(s)},{_f(e)})'"
                      f":eof_action=pass[vo{i}]")
            cur = f"vo{i}"
            n += 1

        # Logo institucional permanente
        brand = self.p.get("brand", {}) or {}
        logo = self.assets.get(brand.get("logo_asset_id"))
        if logo:
            args += ["-framerate", _f(fps), "-loop", "1", "-t", _f(T), "-i", logo]
            lw = max(2, int(float(brand.get("size", 0.12)) * W0) // 2 * 2)
            m = int(0.025 * W0)
            pos = {"tl": (f"{m}", f"{m}"), "tr": (f"W-w-{m}", f"{m}"),
                   "bl": (f"{m}", f"H-h-{m}"), "br": (f"W-w-{m}", f"H-h-{m}")}
            px, py = pos.get(brand.get("position", "tr"), pos["tr"])
            fl.append(f"[{n}:v]scale={lw}:-2,format=rgba,"
                      f"colorchannelmixer=aa={_f(float(brand.get('opacity', 0.85)))}[logo]")
            fl.append(f"[{cur}][logo]overlay=x={px}:y={py}:eof_action=pass[vl]")
            cur = "vl"
            n += 1

        # Reencuadre para redes sociales (9:16 o 1:1)
        if (W, H) != (W0, H0):
            mode = self.opt.get("reframe", "blur")
            if abs(W / H - W0 / H0) < 0.01:
                fl.append(f"[{cur}]scale={W}:{H}:flags=lanczos[vr]")
            elif mode == "crop":
                cx = float(self.opt.get("crop_x", 0.5))
                fl.append(f"[{cur}]scale=-2:{H}:flags=lanczos,"
                          f"crop={W}:{H}:x='max(0,min(iw-{W},{_f(cx)}*iw-{W}/2))':y=0[vr]")
            else:
                fl.append(f"[{cur}]split=2[rb][rf]")
                fl.append(f"[rb]scale={W}:{H}:force_original_aspect_ratio=increase,"
                          f"crop={W}:{H},gblur=sigma=30,eq=brightness=-0.08[bg]")
                fl.append(f"[rf]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg]")
                fl.append("[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2[vr]")
            cur = "vr"

        # Subtítulos incrustados
        cap = self.p.get("captions", {}) or {}
        if self.opt.get("burn_captions", cap.get("burn")) and cues:
            ass = self.wd / "subs.ass"
            ass.write_text(captions.to_ass(cues, W, H, cap), encoding="utf-8")
            fl.append(f"[{cur}]ass=filename={ass}[vs]")
            cur = "vs"
        fl.append(f"[{cur}]format=yuv420p[vfinal]")

        # Audio: voz procesada + música con ducking
        fl.append(f"[0:a]{self._audio_chain()}[voice]")
        music = [mu for mu in self.p.get("music", [])
                 if mu.get("media_id") in self.media and self.media[mu["media_id"]]["has_audio"]]
        if music:
            mlabels = []
            for j, mu in enumerate(music):
                mm = self.media[mu["media_id"]]
                start = max(0.0, float(mu.get("start", 0)))
                if start >= T:
                    continue
                mi = float(mu.get("in", 0))
                mo = float(mu.get("out", mm["duration"]) or mm["duration"])
                length = max(0.1, min(mo - mi, T - start))
                args += ["-ss", _f(mi), "-t", _f(length), "-i", mm["path"]]
                vol = float(mu.get("volume", 0.25))
                d = int(start * 1000)
                fade = min(1.5, length / 4)
                fl.append(f"[{n}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                          f"volume={_f(vol)},afade=t=in:d={_f(fade)},"
                          f"afade=t=out:st={_f(length - fade)}:d={_f(fade)},"
                          f"adelay={d}|{d}[m{j}]")
                mlabels.append(f"[m{j}]")
                n += 1
            if mlabels:
                if len(mlabels) > 1:
                    fl.append(f"{''.join(mlabels)}amix=inputs={len(mlabels)}:normalize=0[mus]")
                else:
                    fl.append(f"{mlabels[0]}anull[mus]")
                if any(mu.get("duck", True) for mu in music):
                    fl.append("[voice]asplit=2[vmain][vsc]")
                    fl.append("[mus][vsc]sidechaincompress=threshold=0.02:ratio=10:"
                              "attack=15:release=450:makeup=1[mduck]")
                    fl.append("[vmain][mduck]amix=inputs=2:duration=first:normalize=0[afinal]")
                else:
                    fl.append("[voice][mus]amix=inputs=2:duration=first:normalize=0[afinal]")
            else:
                fl.append("[voice]anull[afinal]")
        else:
            fl.append("[voice]anull[afinal]")

        out = self.wd / "mezz.mkv"
        crf = int(self.opt.get("crf", config.RENDER_CRF))
        args += ["-filter_complex", ";".join(fl), "-map", "[vfinal]", "-map", "[afinal]",
                 "-t", _f(T), "-r", _f(fps),
                 "-c:v", "libx264", "-preset", self.opt.get("preset", config.RENDER_PRESET),
                 "-crf", str(crf), "-profile:v", "high", "-pix_fmt", "yuv420p",
                 "-g", str(int(round(fps * 2))),
                 "-c:a", "pcm_s16le", "-ar", "48000", str(out)]
        run_ffmpeg(args, T, lambda f: self.cb(0.35 + 0.55 * f, "Componiendo y codificando"))
        return out

    # ------------------------------------------------------------------ Etapa C
    def measure_loudness(self, src: Path, target: float) -> dict | None:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(src), "-map", "0:a",
               "-af", f"loudnorm=I={_f(target)}:TP=-1.5:LRA=11:print_format=json",
               "-f", "null", "-"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out.stderr, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        try:
            if float(data["input_i"]) < -70:   # silencio total: no normalizar
                return None
        except (ValueError, KeyError):
            return None
        return data

    def stage_c(self, mezz: Path, cues: list[dict], dst: Path, title: str) -> None:
        target = float(self.p.get("audio", {}).get("loudness", -16))
        self.cb(0.9, "Midiendo sonoridad (EBU R128)")
        meas = self.measure_loudness(mezz, target)
        args = ["-i", str(mezz)]
        meta = self.wd / "chapters.txt"
        meta.write_text(captions.chapters_ffmetadata(self.p.get("chapters", []), self.T, title),
                        encoding="utf-8")
        args += ["-f", "ffmetadata", "-i", str(meta)]
        maps = ["-map", "0:v", "-map", "0:a"]
        cap = self.p.get("captions", {}) or {}
        soft = cues and self.opt.get("soft_captions", cap.get("soft", True))
        if soft:
            srt = self.wd / "soft.srt"
            srt.write_text(captions.to_srt(cues), encoding="utf-8")
            args += ["-i", str(srt)]
            maps += ["-map", "2:s"]
        if meas:
            af = (f"loudnorm=I={_f(target)}:TP=-1.5:LRA=11:"
                  f"measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
                  f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
                  f"offset={meas['target_offset']}:linear=true,aresample=48000")
        else:
            af = "anull"
        args += maps + ["-map_metadata", "1", "-map_chapters", "1",
                        "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        if soft:
            args += ["-c:s", "mov_text", "-metadata:s:s:0",
                     f"language={'spa' if cap.get('lang', 'es') == 'es' else 'eng'}"]
        args += ["-metadata", f"title={title}", "-movflags", "+faststart", str(dst)]
        self.cb(0.93, "Normalizando audio y empaquetando MP4")
        run_ffmpeg(args, self.T, lambda f: self.cb(0.93 + 0.06 * f, "Empaquetando MP4"))

    # ------------------------------------------------------------------ Orquestación
    def run(self, out_dir: Path, title: str) -> dict:
        out_dir.mkdir(parents=True, exist_ok=True)
        cues = self.build_cues()
        base = self.stage_a()
        mezz = self.stage_b(base, cues)
        mp4 = out_dir / "video.mp4"
        self.stage_c(mezz, cues, mp4, title)
        files = {"mp4": str(mp4)}
        if cues:
            (out_dir / "subtitulos_es.srt").write_text(captions.to_srt(cues), encoding="utf-8")
            (out_dir / "subtitulos_es.vtt").write_text(captions.to_vtt(cues), encoding="utf-8")
            files["srt"] = str(out_dir / "subtitulos_es.srt")
            files["vtt"] = str(out_dir / "subtitulos_es.vtt")
            words = captions.timeline_words(self.p, self.transcripts)
            (out_dir / "transcripcion.txt").write_text(_paragraphs(words), encoding="utf-8")
            files["txt"] = str(out_dir / "transcripcion.txt")
        segs = captions.timeline_segments(self.p, self.translations)
        if segs:
            en = captions.segments_to_cues(segs)
            (out_dir / "subtitles_en.vtt").write_text(captions.to_vtt(en), encoding="utf-8")
            (out_dir / "subtitles_en.srt").write_text(captions.to_srt(en), encoding="utf-8")
            files["vtt_en"] = str(out_dir / "subtitles_en.vtt")
            files["srt_en"] = str(out_dir / "subtitles_en.srt")
        if self.p.get("chapters"):
            (out_dir / "capitulos.vtt").write_text(
                captions.chapters_vtt(self.p["chapters"], self.T), encoding="utf-8")
            files["chapters"] = str(out_dir / "capitulos.vtt")
        poster = out_dir / "poster.jpg"
        try:
            run_ffmpeg(["-ss", _f(min(self.T * 0.1, 5)), "-i", str(mp4), "-frames:v", "1",
                        "-q:v", "3", str(poster)])
            files["poster"] = str(poster)
        except FFmpegError:
            pass
        self.cb(1.0, "Listo")
        return {"files": files, "duration": round(self.T, 3), "width": self.W, "height": self.H}


def _paragraphs(words: list[dict]) -> str:
    out, line = [], []
    for i, w in enumerate(words):
        line.append(w["w"].strip())
        nxt = words[i + 1]["s"] if i + 1 < len(words) else None
        if nxt is None or nxt - w["e"] > 1.5:
            out.append(" ".join(line))
            line = []
    return "\n\n".join(out) + "\n"


def cleanup(workdir: Path) -> None:
    shutil.rmtree(workdir, ignore_errors=True)
