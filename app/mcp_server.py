"""Servidor MCP remoto (transporte «Streamable HTTP», sin estado) del Estudio de Video Educativo.

Endpoint: POST /mcp  (JSON-RPC 2.0; respuestas application/json). Protegido con OAuth 2.1
(ver app/oauth.py): sin token válido responde 401 con WWW-Authenticate → resource_metadata.
Cada herramienta actúa con los permisos del usuario dueño del token (mismos roles del editor).
"""
from __future__ import annotations

import json
import secrets
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import auth, db, oauth

router = APIRouter()

SERVER_INFO = {"name": "estudio-video-unachi", "title": "Estudio de Video Educativo · UNACHI",
               "version": "1.0.0"}
PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
INSTRUCTIONS = (
    "Herramientas para el Estudio de Video Educativo (editor web de clases en video de la UNACHI). "
    "Flujo típico: listar_proyectos → ver_proyecto → (transcribir, detectar/eliminar silencios, capítulos, "
    "preguntas, quitar fondo) → exportar_video → estado_trabajos → listar_exportaciones/publicar_exportacion. "
    "Los tiempos se expresan en segundos. Las capas gráficas (texto, LaTeX, química, moléculas) creadas desde aquí "
    "se dibujan al abrir el editor; ábralo una vez antes de exportar. Las tareas pesadas (transcribir, quitar fondo, exportar) se encolan "
    "en el servidor: consulte estado_trabajos. Si el docente tiene el editor abierto, al guardar se le pedirá "
    "recargar para ver los cambios hechos desde aquí.")


# ======================================================================= Utilidades
class ToolError(Exception):
    pass


def _check(pid: str, user: dict, write: bool = False) -> str:
    try:
        return auth.check_project(pid, user, write)
    except HTTPException as e:
        raise ToolError(e.detail)


def _media(mid: str, user: dict, write: bool = False):
    m = db.q1("SELECT * FROM media WHERE id=?", mid)
    if not m:
        raise ToolError("Medio no encontrado")
    _check(m["project_id"], user, write)
    return m


def _load(pid: str):
    p = db.q1("SELECT * FROM projects WHERE id=?", pid)
    return p, db.jload(p["data"], {})


def _save(pid: str, version: int, data: dict) -> None:
    with db.tx() as c:
        cur = c.execute("SELECT version FROM projects WHERE id=?", (pid,)).fetchone()
        if cur["version"] != version:
            raise ToolError("El proyecto cambió mientras se editaba; vuelva a intentarlo.")
        c.execute("UPDATE projects SET data=?, version=version+1, updated_at=? WHERE id=?",
                  (db.jdump(data), db.now(), pid))


def _dur(c) -> float:
    return max(0.0, float(c["out"]) - float(c["in"]))


def _total(data) -> float:
    return sum(_dur(c) for c in data.get("clips", []))


def _fmt(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _public_url(request: Request, token: str) -> str:
    return oauth.base_url(request) + "/v/" + token


# ---------- Recorte de tramos de la secuencia (igual que cutRange/rippleItems del editor)
def _ripple(data: dict, a: float, b: float, media: dict) -> None:
    d = b - a

    def span(lst):
        out = []
        for o in lst:
            if o["end"] <= a:
                out.append(o)
                continue
            if o["start"] >= b:
                o["start"] -= d
                o["end"] -= d
                out.append(o)
                continue
            if o.get("type") == "media" and o["start"] > a:
                o["in"] = (o.get("in") or 0) + (b - o["start"])
            o["start"] = o["start"] if o["start"] < a else a
            o["end"] = o["end"] - d if o["end"] > b else a
            if o["end"] - o["start"] >= 0.1:
                out.append(o)
        return out

    data["overlays"] = span(data.get("overlays", []))
    data["zooms"] = span(data.get("zooms", []))
    kept = []
    for m in data.get("music", []):
        mdur = (media.get(m["media_id"]) or {}).get("duration", 0)
        out_ = m["out"] if m.get("out") is not None else mdur
        e = m["start"] + out_ - (m.get("in") or 0)
        if m.get("role") == "voice":
            if e <= a + 1e-6:
                kept.append(m)
                continue
            if m["start"] >= b - 1e-6:
                m["start"] -= d
                kept.append(m)
                continue
            if m["start"] < a:
                kept.append({**m, "out": (m.get("in") or 0) + (a - m["start"])})
            if e > b:
                kept.append({**m, "id": _uid() if m["start"] < a else m["id"],
                             "in": (m.get("in") or 0) + (b - m["start"]), "start": a})
        else:
            if m["start"] >= b:
                m["start"] -= d
            elif m["start"] > a:
                m["in"] = (m.get("in") or 0) + (b - m["start"])
                m["start"] = a
            kept.append(m)
    data["music"] = [m for m in kept if ((m["out"] if m.get("out") is not None else
                                          (media.get(m["media_id"]) or {}).get("duration", 1)) - (m.get("in") or 0)) >= 0.05]
    for k in data.get("chapters", []) + data.get("quizzes", []):
        if k["t"] >= b:
            k["t"] -= d
        elif k["t"] > a:
            k["t"] = a


def _cut(data: dict, a: float, b: float, media: dict) -> None:
    if b - a < 0.02:
        return
    out, t0 = [], 0.0
    for c in data["clips"]:
        s, e = t0, t0 + _dur(c)
        t0 = e
        if e <= a + 1e-6 or s >= b - 1e-6:
            out.append(c)
            continue
        if a > s:
            out.append({**c, "out": c["in"] + (a - s)})
        if b < e:
            out.append({**c, "id": _uid() if a > s else c["id"], "in": c["in"] + (b - s)})
    data["clips"] = [c for c in out if _dur(c) >= 1 / 30]
    _ripple(data, a, b, media)


def _silence_ranges(data: dict, media: dict, noise_db: float, min_dur: float, pad: float) -> list[list[float]]:
    """Pausas en tiempo de la línea (misma lógica que el botón «Eliminar silencios» del editor)."""
    from .config import PROXY_DIR
    from .media import detect_silences
    raw, t0 = [], 0.0
    for c in data["clips"]:
        m = media.get(c["media_id"])
        s0 = t0
        t0 += _dur(c)
        if c.get("mute") or not m or m["kind"] == "image" or not m["has_audio"] or m["status"] != "ready":
            continue
        src = m["proxy_path"] if m["proxy_path"] and Path(m["proxy_path"]).exists() else m["path"]
        for s, e in detect_silences(src, noise_db, min_dur, PROXY_DIR, m["id"]):
            a, b = max(s, c["in"]), min(e, c["out"])
            if b > a:
                raw.append([s0 + a - c["in"], s0 + b - c["in"]])
    raw.sort()
    merged = []
    for r in raw:
        if merged and r[0] - merged[-1][1] < 0.02:
            merged[-1][1] = max(merged[-1][1], r[1])
        else:
            merged.append(list(r))
    T = _total(data)
    ranges = [[0.0 if a <= 0.01 else a + pad, T if b >= T - 0.01 else b - pad]
              for a, b in merged if b - a >= min_dur]
    prot = []
    for mu in data.get("music", []):
        if mu.get("role") == "voice" and (mu.get("volume", 1) or 0) > 0:
            md = (media.get(mu["media_id"]) or {}).get("duration", 0)
            ln = (mu["out"] if mu.get("out") is not None else md) - (mu.get("in") or 0)
            if ln > 0:
                prot.append((mu["start"], mu["start"] + ln))
    for o in data.get("overlays", []):
        mm = media.get((o.get("params") or {}).get("media_id"))
        if o.get("type") == "media" and not o.get("mute") and (o.get("volume", 1) or 0) > 0 and mm \
                and mm["kind"] == "video" and mm["has_audio"] and not mm["audio_silent"]:
            prot.append((o["start"], o["end"]))
    for x, y in prot:
        nx = []
        for a, b in ranges:
            if y <= a or x >= b:
                nx.append([a, b])
                continue
            if x > a:
                nx.append([a, x])
            if y < b:
                nx.append([y, b])
        ranges = nx
    return [[round(a, 3), round(b, 3)] for a, b in ranges if b - a >= 0.1]


def _media_map(pid: str) -> dict:
    return {r["id"]: dict(r) for r in db.q("SELECT * FROM media WHERE project_id=?", pid)}


# ======================================================================= Herramientas
def t_listar_proyectos(user, req, args):
    rows = db.q("SELECT p.id, p.name, p.updated_at, p.data, u.username AS owner FROM projects p "
                "JOIN users u ON u.id=p.owner_id WHERE p.owner_id=? OR p.id IN "
                "(SELECT project_id FROM project_members WHERE user_id=?) OR ?='admin' "
                "ORDER BY p.updated_at DESC LIMIT 100", user["id"], user["id"], user["role"])
    out = []
    for r in rows:
        d = db.jload(r["data"], {})
        out.append({"proyecto_id": r["id"], "nombre": r["name"], "propietario": r["owner"],
                    "formato": d.get("settings", {}).get("format"), "duracion_s": round(_total(d), 1),
                    "clips": len(d.get("clips", [])), "actualizado": r["updated_at"]})
    return {"proyectos": out}


def t_ver_proyecto(user, req, args):
    pid = args["proyecto_id"]
    role = _check(pid, user)
    p, d = _load(pid)
    media = _media_map(pid)
    seq, t = [], 0.0
    for c in d.get("clips", []):
        m = media.get(c["media_id"], {})
        seq.append({"clip_id": c["id"], "inicio_s": round(t, 2), "fin_s": round(t + _dur(c), 2),
                    "medio": m.get("filename"), "medio_id": c["media_id"],
                    "tramo_original": [round(c["in"], 2), round(c["out"], 2)], "silenciado": bool(c.get("mute"))})
        t += _dur(c)
    capas = [{"capa_id": o["id"], "tipo": o.get("type"), "inicio_s": round(o["start"], 2), "fin_s": round(o["end"], 2),
              "medio": (media.get((o.get("params") or {}).get("media_id")) or {}).get("filename"),
              "texto": (o.get("params") or {}).get("text") or (o.get("params") or {}).get("tex"),
              "sombra": bool((o.get("shadow") or {}).get("on"))} for o in d.get("overlays", [])]
    return {"proyecto_id": pid, "nombre": p["name"], "su_rol": role, "formato": d.get("settings", {}).get("format"),
            "duracion_s": round(t, 2), "duracion": _fmt(t), "secuencia": seq, "capas_en_orden": capas,
            "capitulos": sorted(d.get("chapters", []), key=lambda k: k["t"]),
            "preguntas": [{"t": q["t"], "pregunta": q.get("question"), "opciones": q.get("options"),
                           "correcta": q.get("correct")} for q in sorted(d.get("quizzes", []), key=lambda k: k["t"])],
            "audio": d.get("audio"), "subtitulos": d.get("captions"),
            "editor_url": oauth.base_url(req) + "/editor/" + pid}


def t_crear_proyecto(user, req, args):
    from .main import default_project
    fmt = args.get("formato", "16:9")
    if fmt not in ("16:9", "9:16", "1:1"):
        raise ToolError("formato debe ser 16:9, 9:16 o 1:1")
    pid, t = db.new_id(), db.now()
    db.ex("INSERT INTO projects(id, owner_id, name, data, created_at, updated_at) VALUES (?,?,?,?,?,?)",
          pid, user["id"], str(args["nombre"]).strip()[:120], db.jdump(default_project(fmt)), t, t)
    return {"proyecto_id": pid, "editor_url": oauth.base_url(req) + "/editor/" + pid,
            "nota": "Suba o grabe los videos desde el editor; luego puede añadirlos con anadir_a_secuencia."}


def t_listar_medios(user, req, args):
    pid = args["proyecto_id"]
    _check(pid, user)
    tx = {r["media_id"]: r["language"] for r in db.q(
        "SELECT t.media_id, t.language FROM transcripts t JOIN media m ON m.id=t.media_id WHERE m.project_id=?", pid)}
    return {"medios": [{"medio_id": r["id"], "archivo": r["filename"], "tipo": r["kind"], "estado": r["status"],
                        "duracion_s": round(r["duration"], 2), "resolucion": f"{r['width']}x{r['height']}",
                        "audio": bool(r["has_audio"]), "transparente": bool(r["has_alpha"]),
                        "transcrito": r["id"] in tx, "idioma": tx.get(r["id"])}
                       for r in db.q("SELECT * FROM media WHERE project_id=? ORDER BY created_at", pid)]}


def t_ver_transcripcion(user, req, args):
    m = _media(args["medio_id"], user)
    r = db.q1("SELECT * FROM transcripts WHERE media_id=?", m["id"])
    if not r:
        raise ToolError("Este medio no tiene transcripción. Use la herramienta transcribir.")
    words = db.jload(r["words"], [])
    if args.get("con_tiempos"):
        # Frases de hasta ~15 s con su tiempo de inicio (en el archivo original)
        out, cur, t0 = [], [], None
        for w in words:
            if t0 is None:
                t0 = w["s"]
            cur.append(w["w"].strip())
            if w["w"].rstrip().endswith((".", "?", "!")) or w["e"] - t0 > 15:
                out.append(f"[{_fmt(t0)}] " + " ".join(cur))
                cur, t0 = [], None
        if cur:
            out.append(f"[{_fmt(t0)}] " + " ".join(cur))
        text = "\n".join(out)
    else:
        text = " ".join(w["w"].strip() for w in words)
    return {"medio": m["filename"], "idioma": r["language"], "palabras": len(words), "texto": text[:60000]}


def t_transcribir(user, req, args):
    m = _media(args["medio_id"], user, write=True)
    if not m["has_audio"]:
        raise ToolError("Este medio no tiene audio")
    jid = db.enqueue("transcribe", m["project_id"], m["id"],
                     {"language": args.get("idioma", "es"), "translate": bool(args.get("traducir_ingles"))})
    return {"trabajo_id": jid, "estado": "en cola", "nota": "Consulte estado_trabajos; luego ver_transcripcion."}


def t_detectar_silencios(user, req, args):
    pid = args["proyecto_id"]
    _check(pid, user)
    p, d = _load(pid)
    rng = _silence_ranges(d, _media_map(pid), float(args.get("sensibilidad_db", -35)),
                          float(args.get("pausa_minima_s", 0.7)), float(args.get("margen_s", 0.15)))
    tot = sum(b - a for a, b in rng)
    T = _total(d)
    return {"pausas": len(rng), "segundos_a_eliminar": round(tot, 1), "duracion_actual": _fmt(T),
            "duracion_resultante": _fmt(T - tot), "tramos": [{"inicio": _fmt(a), "duracion_s": round(b - a, 2)} for a, b in rng]}


def t_eliminar_silencios(user, req, args):
    pid = args["proyecto_id"]
    _check(pid, user, write=True)
    p, d = _load(pid)
    media = _media_map(pid)
    rng = _silence_ranges(d, media, float(args.get("sensibilidad_db", -35)),
                          float(args.get("pausa_minima_s", 0.7)), float(args.get("margen_s", 0.15)))
    if not rng:
        return {"pausas": 0, "mensaje": "No se encontraron pausas con esos parámetros."}
    T = _total(d)
    for a, b in reversed(rng):
        _cut(d, a, b, media)
    _save(pid, p["version"], d)
    tot = sum(b - a for a, b in rng)
    return {"pausas_eliminadas": len(rng), "segundos_eliminados": round(tot, 1),
            "duracion_antes": _fmt(T), "duracion_despues": _fmt(_total(d)),
            "nota": "Si el editor está abierto, recárguelo para ver los cortes. Conviene revisar antes con detectar_silencios."}


def t_anadir_a_secuencia(user, req, args):
    m = _media(args["medio_id"], user, write=True)
    pid = m["project_id"]
    if m["status"] != "ready" or m["kind"] not in ("video", "image"):
        raise ToolError("El medio debe ser un video o una imagen ya procesados")
    p, d = _load(pid)
    dur = float(m["duration"]) if m["kind"] == "video" else float(args.get("duracion_imagen_s", 4))
    clip = {"id": _uid(), "media_id": m["id"], "in": 0, "out": dur, "fit": "contain"}
    if args.get("posicion") == "inicio":
        d["clips"].insert(0, clip)
        from .main import default_project  # noqa: F401 (solo para coherencia de imports)
        for o in d.get("overlays", []) + d.get("zooms", []):
            o["start"] += dur
            o["end"] += dur
        for mu in d.get("music", []):
            mu["start"] += dur
        for k in d.get("chapters", []) + d.get("quizzes", []):
            k["t"] += dur
    else:
        d["clips"].append(clip)
    _save(pid, p["version"], d)
    return {"clip_id": clip["id"], "duracion_total": _fmt(_total(d))}


def t_agregar_capitulo(user, req, args):
    pid = args["proyecto_id"]
    _check(pid, user, write=True)
    p, d = _load(pid)
    t = max(0.0, min(float(args["tiempo_s"]), _total(d)))
    d.setdefault("chapters", []).append({"id": _uid(), "t": round(t, 2), "title": str(args["titulo"])[:120]})
    _save(pid, p["version"], d)
    return {"capitulos": sorted(d["chapters"], key=lambda k: k["t"])}


def t_agregar_pregunta(user, req, args):
    pid = args["proyecto_id"]
    _check(pid, user, write=True)
    opts = [str(o)[:200] for o in args["opciones"]]
    if not 2 <= len(opts) <= 6:
        raise ToolError("Indique entre 2 y 6 opciones")
    correct = int(args["correcta"])
    if not 0 <= correct < len(opts):
        raise ToolError("correcta debe ser el índice (desde 0) de la opción correcta")
    p, d = _load(pid)
    t = max(0.0, min(float(args["tiempo_s"]), _total(d)))
    q = {"id": _uid(), "t": round(t, 2), "question": str(args["pregunta"])[:300], "options": opts,
         "correct": correct, "explanation": str(args.get("explicacion", ""))[:500]}
    d.setdefault("quizzes", []).append(q)
    _save(pid, p["version"], d)
    return {"pregunta_id": q["id"], "total_preguntas": len(d["quizzes"])}


def t_quitar_fondo(user, req, args):
    from .main import RemoveBgIn, media_remove_bg
    body = RemoveBgIn(mode=args.get("modo", "transparent"), color=args.get("color", "#ffffff"),
                      blur=int(args.get("desenfoque", 25)), max_height=int(args.get("resolucion", 720)),
                      quality=args.get("calidad", "normal"), image_media_id=args.get("imagen_medio_id"),
                      start=args.get("desde_s"), end=args.get("hasta_s"))
    # media_remove_bg valida permisos con current_user; aquí ya tenemos el usuario del token
    _media(args["medio_id"], user, write=True)
    try:
        r = media_remove_bg(args["medio_id"], body, user)
    except HTTPException as e:
        raise ToolError(e.detail)
    return {"nuevo_medio_id": r["id"], "trabajo_id": r["job_id"],
            "nota": "Se crea un video nuevo en la biblioteca del proyecto; consulte estado_trabajos."}


def t_exportar_video(user, req, args):
    pid = args["proyecto_id"]
    _check(pid, user, write=True)
    p, d = _load(pid)
    if not d.get("clips"):
        raise ToolError("El proyecto no tiene clips en la línea de tiempo")
    pending = [o for o in d.get("overlays", []) if o.get("type") != "media" and not o.get("asset_id")]
    if pending and not args.get("forzar"):
        raise ToolError(f"Hay {len(pending)} capa(s) gráfica(s) aún sin dibujar (creadas o modificadas desde aquí). "
                        "Abra el proyecto en el editor una vez para que se dibujen y repita, o use forzar=true para exportar sin ellas. "
                        "Editor: " + oauth.base_url(req) + "/editor/" + pid)
    params = {"format": args.get("formato", d.get("settings", {}).get("format", "16:9")),
              "reframe": args.get("reencuadre", "blur"), "title": args.get("titulo") or p["name"],
              "crf": int(args.get("crf", 20)), "include_english": bool(args.get("subtitulos_ingles", True))}
    if args.get("resolucion") in (720, "720"):
        params["height"] = 720
    if "incrustar_subtitulos" in args:
        params["burn_captions"] = bool(args["incrustar_subtitulos"])
    jid = db.enqueue("render", pid, None, params)
    return {"trabajo_id": jid, "estado": "en cola", "nota": "Consulte estado_trabajos; al terminar, listar_exportaciones."}


def t_estado_trabajos(user, req, args):
    pid = args["proyecto_id"]
    _check(pid, user)
    names = {"ingest": "procesar medio", "transcribe": "transcribir", "render": "exportar", "bgremove": "quitar fondo"}
    rows = db.q("SELECT j.*, m.filename FROM jobs j LEFT JOIN media m ON m.id=j.media_id WHERE j.project_id=? "
                "ORDER BY j.created_at DESC LIMIT 15", pid)
    return {"trabajos": [{"trabajo_id": r["id"], "tipo": names.get(r["type"], r["type"]), "estado": r["status"],
                          "progreso": f"{round((r['progress'] or 0) * 100)}%", "mensaje": r["message"],
                          "medio": r["filename"]} for r in rows]}


def t_listar_exportaciones(user, req, args):
    pid = args["proyecto_id"]
    _check(pid, user)
    out = []
    for r in db.q("SELECT * FROM exports WHERE project_id=? ORDER BY created_at DESC", pid):
        meta = db.jload(r["meta"], {})
        out.append({"exportacion_id": r["id"], "titulo": r["title"], "formato": r["format"],
                    "duracion": _fmt(meta.get("duration", 0)), "publica": bool(r["public"]),
                    "enlace_publico": _public_url(req, r["token"]) if r["public"] else None,
                    "archivos": sorted(db.jload(r["files"], {}).keys())})
    return {"exportaciones": out}


def t_publicar_exportacion(user, req, args):
    e = db.q1("SELECT * FROM exports WHERE id=?", args["exportacion_id"])
    if not e:
        raise ToolError("Exportación no encontrada")
    _check(e["project_id"], user, write=True)
    pub = bool(args.get("publica", True))
    db.ex("UPDATE exports SET public=? WHERE id=?", int(pub), e["id"])
    return {"publica": pub, "enlace_publico": _public_url(req, e["token"]) if pub else None,
            "nota": "El enlace abre el reproductor interactivo con capítulos, subtítulos y preguntas."}


def t_obtener_subtitulos(user, req, args):
    e = db.q1("SELECT * FROM exports WHERE id=?", args["exportacion_id"])
    if not e:
        raise ToolError("Exportación no encontrada")
    _check(e["project_id"], user)
    files = db.jload(e["files"], {})
    kind = {"es": "srt", "en": "srt_en"}.get(args.get("idioma", "es"), "srt")
    if kind not in files or not Path(files[kind]).exists():
        if kind == "srt_en" and "vtt_en" in files and Path(files["vtt_en"]).exists():
            kind = "vtt_en"
        else:
            raise ToolError("Esta exportación no tiene subtítulos en ese idioma")
    return {"formato": "SRT" if kind.startswith("srt") else "WebVTT",
            "contenido": Path(files[kind]).read_text(encoding="utf-8")[:60000]}


def _s(t, d, **kw):
    return {"type": t, "description": d, **kw}


PID = {"proyecto_id": _s("string", "ID del proyecto (de listar_proyectos)")}
SIL = {"sensibilidad_db": _s("number", "Nivel bajo el cual hay silencio (dBFS): -30 aula ruidosa, -35 normal, -40/-45 grabación silenciosa", default=-35),
       "pausa_minima_s": _s("number", "Duración mínima de una pausa para eliminarla (s)", default=0.7),
       "margen_s": _s("number", "Respiro que se conserva antes y después de cada frase (s)", default=0.15)}
RO = {"readOnlyHint": True, "openWorldHint": False}
WR = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

TOOLS = {
    "listar_proyectos": (t_listar_proyectos, "Lista los proyectos de video del docente (propios y compartidos) con duración y formato.", {}, [], RO),
    "ver_proyecto": (t_ver_proyecto, "Detalle de un proyecto: secuencia de clips, capas en orden (de abajo hacia arriba), capítulos, preguntas y ajustes de audio y subtítulos.", PID, ["proyecto_id"], RO),
    "crear_proyecto": (t_crear_proyecto, "Crea un proyecto vacío.", {"nombre": _s("string", "Nombre del proyecto"), "formato": _s("string", "Formato del video", enum=["16:9", "9:16", "1:1"], default="16:9")}, ["nombre"], WR),
    "listar_medios": (t_listar_medios, "Lista los videos, audios e imágenes subidos a un proyecto (biblioteca), con estado y si están transcritos.", PID, ["proyecto_id"], RO),
    "ver_transcripcion": (t_ver_transcripcion, "Texto de la transcripción de un medio (opcionalmente en frases con marca de tiempo). Útil para resumir la clase, proponer capítulos o preguntas.", {"medio_id": _s("string", "ID del medio"), "con_tiempos": _s("boolean", "Incluir marcas de tiempo por frase", default=False)}, ["medio_id"], RO),
    "transcribir": (t_transcribir, "Encola la transcripción automática (Whisper) de un medio.", {"medio_id": _s("string", "ID del medio"), "idioma": _s("string", "Código de idioma (es, en, pt…) o auto", default="es"), "traducir_ingles": _s("boolean", "Generar además subtítulos en inglés", default=False)}, ["medio_id"], WR),
    "detectar_silencios": (t_detectar_silencios, "Analiza el audio de la línea de tiempo y lista las pausas que se eliminarían (no modifica nada).", {**PID, **SIL}, ["proyecto_id"], RO),
    "eliminar_silencios": (t_eliminar_silencios, "Recorta del proyecto las pausas detectadas en el audio (igual que el botón «Eliminar silencios»). Respeta clips silenciados, narraciones y videos superpuestos con sonido.", {**PID, **SIL}, ["proyecto_id"], {**WR, "destructiveHint": True}),
    "anadir_a_secuencia": (t_anadir_a_secuencia, "Añade un video o imagen de la biblioteca a la pista principal, al final o al inicio.", {"medio_id": _s("string", "ID del medio"), "posicion": _s("string", "Dónde añadirlo", enum=["final", "inicio"], default="final"), "duracion_imagen_s": _s("number", "Duración si es una imagen", default=4)}, ["medio_id"], WR),
    "agregar_capitulo": (t_agregar_capitulo, "Agrega un capítulo (se incrusta en el MP4 y aparece en el reproductor).", {**PID, "tiempo_s": _s("number", "Tiempo en la línea de tiempo (s)"), "titulo": _s("string", "Título del capítulo")}, ["proyecto_id", "tiempo_s", "titulo"], WR),
    "agregar_pregunta": (t_agregar_pregunta, "Agrega una pregunta de opción múltiple que pausa el video en ese tiempo (reproductor público y SCORM).", {**PID, "tiempo_s": _s("number", "Tiempo (s)"), "pregunta": _s("string", "Enunciado"), "opciones": _s("array", "Opciones (2 a 6)", items={"type": "string"}), "correcta": _s("integer", "Índice de la opción correcta, desde 0"), "explicacion": _s("string", "Retroalimentación tras responder")}, ["proyecto_id", "tiempo_s", "pregunta", "opciones", "correcta"], WR),
    "quitar_fondo": (t_quitar_fondo, "Quita el fondo de un video (recorte de la persona con IA) y crea un video nuevo en la biblioteca. Opcionalmente solo un tramo.", {"medio_id": _s("string", "ID del video"), "modo": _s("string", "Fondo nuevo", enum=["transparent", "color", "blur", "image"], default="transparent"), "color": _s("string", "Color #RRGGBB si modo=color"), "imagen_medio_id": _s("string", "Imagen de la biblioteca si modo=image"), "desenfoque": _s("integer", "Intensidad 5–60 si modo=blur", default=25), "resolucion": _s("integer", "Altura máxima", enum=[480, 720, 1080], default=720), "calidad": _s("string", "Precisión del borde", enum=["normal", "rapida"], default="normal"), "desde_s": _s("number", "Inicio del tramo en el original (s)"), "hasta_s": _s("number", "Fin del tramo en el original (s)")}, ["medio_id"], WR),
    "exportar_video": (t_exportar_video, "Encola la exportación (render) del proyecto a MP4 con subtítulos, capítulos y sonoridad normalizada.", {**PID, "formato": _s("string", "Formato de salida", enum=["16:9", "9:16", "1:1"]), "resolucion": _s("integer", "1080 (completa) o 720", enum=[1080, 720], default=1080), "titulo": _s("string", "Título de la exportación"), "incrustar_subtitulos": _s("boolean", "Grabar los subtítulos en la imagen"), "subtitulos_ingles": _s("boolean", "Incluir subtítulos en inglés si existen", default=True), "reencuadre": _s("string", "Si cambia la proporción", enum=["blur", "crop"], default="blur"), "crf": _s("integer", "Calidad x264 (18 máxima, 20 alta, 23 estándar)", default=20), "forzar": _s("boolean", "Exportar aunque haya capas gráficas sin dibujar", default=False)}, ["proyecto_id"], WR),
    "estado_trabajos": (t_estado_trabajos, "Estado y progreso de los trabajos del proyecto (procesar, transcribir, quitar fondo, exportar).", PID, ["proyecto_id"], RO),
    "listar_exportaciones": (t_listar_exportaciones, "Videos exportados del proyecto, con enlace público si está publicado.", PID, ["proyecto_id"], RO),
    "publicar_exportacion": (t_publicar_exportacion, "Publica (o despublica) una exportación y devuelve el enlace público del reproductor interactivo.", {"exportacion_id": _s("string", "ID de la exportación"), "publica": _s("boolean", "true para publicar, false para retirar", default=True)}, ["exportacion_id"], {**WR, "openWorldHint": True}),
    "obtener_subtitulos": (t_obtener_subtitulos, "Devuelve el archivo de subtítulos (SRT) de una exportación.", {"exportacion_id": _s("string", "ID de la exportación"), "idioma": _s("string", "Idioma", enum=["es", "en"], default="es")}, ["exportacion_id"], RO),
}


def _tool_list():
    out = []
    for name, (_, desc, props, req, ann) in TOOLS.items():
        out.append({"name": name, "title": name.replace("_", " ").capitalize(), "description": desc,
                    "inputSchema": {"type": "object", "properties": props, "required": req,
                                    "additionalProperties": False},
                    "annotations": ann})
    return out


# ======================================================================= JSON-RPC
def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def _handle(msg: dict, user: dict, req: Request):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Solicitud inválida")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:                       # notificación: sin respuesta
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        return _ok(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": _tool_list()})
    if method in ("resources/list", "prompts/list", "resources/templates/list"):
        key = {"resources/list": "resources", "prompts/list": "prompts", "resources/templates/list": "resourceTemplates"}[method]
        return _ok(mid, {key: []})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return _err(mid, -32602, f"Herramienta desconocida: {name}")
        fn, _, _, required, _ = TOOLS[name]
        missing = [r for r in required if r not in args]
        if missing:
            return _ok(mid, {"content": [{"type": "text", "text": "Faltan argumentos: " + ", ".join(missing)}], "isError": True})
        try:
            data = fn(user, req, args)
            if isinstance(data, dict) and "__image__" in data:
                return _ok(mid, {"content": [{"type": "image", "data": data["__image__"], "mimeType": data["mime"]},
                                             {"type": "text", "text": data.get("texto", "")}], "isError": False})
            return _ok(mid, {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}],
                             "structuredContent": data, "isError": False})
        except ToolError as e:
            return _ok(mid, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
        except (KeyError, ValueError, TypeError) as e:
            return _ok(mid, {"content": [{"type": "text", "text": f"Argumentos no válidos: {e}"}], "isError": True})
    return _err(mid, -32601, f"Método no admitido: {method}")


@router.post("/mcp")
async def mcp_post(request: Request):
    user = oauth.user_from_bearer(request)
    if not user:
        return oauth.unauthorized(request)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(_err(None, -32700, "JSON inválido"), 400)
    if isinstance(payload, list):
        res = [r for r in (_handle(m, user, request) for m in payload) if r is not None]
        return JSONResponse(res) if res else Response(status_code=202)
    res = _handle(payload, user, request)
    if res is None:
        return Response(status_code=202)
    headers = {}
    if payload.get("method") == "initialize":
        headers["Mcp-Session-Id"] = secrets.token_urlsafe(16)
    return JSONResponse(res, headers=headers)


@router.get("/mcp")
async def mcp_get(request: Request):
    if not oauth.user_from_bearer(request):
        return oauth.unauthorized(request)
    # Sin flujo SSE iniciado por el servidor (servidor sin estado)
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_delete(request: Request):
    return Response(status_code=204)
