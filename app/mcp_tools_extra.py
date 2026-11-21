"""Herramientas MCP adicionales: el resto de funciones del editor.

Línea de tiempo (recortar, dividir, mover, eliminar tramos), edición por texto (buscar,
muletillas, corregir palabras), capas gráficas y de medios (con orden, sombra, posición),
zoom, audio y pistas, logo, subtítulos, guion, comentarios, compartir, analítica,
quitar fondo de un clip (como capa o reemplazo), importar medios por URL y ver fotogramas.
"""
from __future__ import annotations

import base64
import ipaddress
import re
import shutil
import socket
import subprocess
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import auth, config, db
from .mcp_server import (RO, TOOLS, WR, PID, ToolError, _check, _cut, _dur, _fmt, _load, _media, _media_map,
                         _s, _save, _total, _uid)

LAYER_DEFAULTS = {
    "text": ({"text": "Escriba aquí", "size": 5.5, "color": "#ffffff", "bg": "#000000", "bgOpacity": 0.55,
              "bold": True, "align": "left"}, 0.45),
    "lower": ({"title": "Dr. Nombre Apellido", "subtitle": "Escuela de Química · UNACHI"}, 0.42),
    "latex": ({"tex": "PV = nRT", "color": "#ffffff", "bg": "#000000", "bgOpacity": 0.55}, 0.32),
    "chem": ({"tex": "2H2 + O2 -> 2H2O", "color": "#ffffff", "bg": "#000000", "bgOpacity": 0.55}, 0.36),
    "molecule": ({"smiles": "CCO", "legend": "Etanol", "card": True, "explicitH": False}, 0.26),
    "arrow": ({"color": "#ffd23f", "angle": 0, "thickness": 10}, 0.14),
    "box": ({"color": "#ffd23f", "thickness": 8, "shape": "rect", "fill": 0}, 0.25),
}
FILLERS = {"eh", "ehh", "ehhh", "em", "emm", "mm", "mmm", "hmm", "ah", "ahh", "eeh"}


def _clip_at(d, t):
    t0 = 0.0
    for i, c in enumerate(d["clips"]):
        if t0 <= t < t0 + _dur(c):
            return i, c, t0
        t0 += _dur(c)
    return None, None, None


def _find(lst, key, val, what):
    for x in lst:
        if x.get(key) == val:
            return x
    raise ToolError(f"No existe {what} con id {val}")


def _project_of_item(user, pid, write=True):
    _check(pid, user, write)
    return _load(pid)


def _timeline_words(d, pid):
    """Palabras en tiempo de la línea (como computeWords del editor)."""
    tr = {r["media_id"]: db.jload(r["words"], []) for r in db.q(
        "SELECT t.media_id, t.words FROM transcripts t JOIN media m ON m.id=t.media_id WHERE m.project_id=?", pid)}
    out, t0 = [], 0.0
    for c in d["clips"]:
        dur = _dur(c)
        for idx, w in enumerate(tr.get(c["media_id"], [])):
            mid = (w["s"] + w["e"]) / 2
            if c["in"] <= mid <= c["out"]:
                s = t0 + max(0.0, w["s"] - c["in"])
                e = t0 + min(dur, w["e"] - c["in"])
                out.append({"w": w["w"], "s": s, "e": max(e, s + 0.05), "mid": c["media_id"], "idx": idx})
        t0 += dur
    out.sort(key=lambda w: w["s"])
    return out


def _norm(s):
    return re.sub(r"[.,;:¿?¡!…\"']", "", s.lower()).strip()


def _fit_rect(m, fit, W0, H0):
    mw, mh = m.get("width") or W0, m.get("height") or H0
    k = max(W0 / mw, H0 / mh) if fit == "cover" else min(W0 / mw, H0 / mh)
    w, h = mw * k / W0, mh * k / H0
    return {"x": (1 - w) / 2, "y": (1 - h) / 2, "w": w, "h": h}


# ======================================================================= Proyecto
def t_renombrar_proyecto(user, req, a):
    _check(a["proyecto_id"], user, True)
    db.ex("UPDATE projects SET name=?, updated_at=? WHERE id=?", str(a["nombre"]).strip()[:120], db.now(), a["proyecto_id"])
    return {"ok": True}


def t_eliminar_proyecto(user, req, a):
    pid = a["proyecto_id"]
    if auth.project_role(pid, user) != "owner" and user["role"] != "admin":
        raise ToolError("Solo el propietario puede eliminar el proyecto")
    if a.get("confirmar") is not True:
        raise ToolError("Para eliminar definitivamente el proyecto y todos sus medios, repita con confirmar=true")
    from .main import _purge_project_files
    _purge_project_files(pid)
    db.ex("DELETE FROM projects WHERE id=?", pid)
    return {"eliminado": pid}


def t_compartir_proyecto(user, req, a):
    pid = a["proyecto_id"]
    if auth.project_role(pid, user) != "owner":
        raise ToolError("Solo el propietario comparte el proyecto")
    u = db.q1("SELECT id FROM users WHERE username=? AND active=1", str(a["usuario"]).strip())
    if not u:
        raise ToolError("No existe ese usuario")
    rol = a.get("rol", "editor")
    if rol == "quitar":
        db.ex("DELETE FROM project_members WHERE project_id=? AND user_id=?", pid, u["id"])
    else:
        db.ex("INSERT INTO project_members(project_id, user_id, role) VALUES (?,?,?) ON CONFLICT(project_id, user_id) "
              "DO UPDATE SET role=excluded.role", pid, u["id"], "revisor" if rol == "revisor" else "editor")
    return {"miembros": [dict(r) for r in db.q("SELECT u.username, pm.role AS rol FROM project_members pm JOIN users u "
                                                "ON u.id=pm.user_id WHERE pm.project_id=?", pid)]}


# ======================================================================= Medios
def _public_ip_ok(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def t_importar_medio_url(user, req, a):
    pid = a["proyecto_id"]
    _check(pid, user, True)
    url = str(a["url"]).strip()
    p = urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname or not _public_ip_ok(p.hostname):
        raise ToolError("Use una URL pública http(s) de un archivo de video, audio o imagen")
    name = str(a.get("nombre_archivo") or Path(p.path).name or "medio")[:200]
    mid = db.new_id()
    db.ex("INSERT INTO media(id, project_id, filename, path, created_at) VALUES (?,?,?,?,?)", mid, pid, name, "", db.now())
    limit = config.MAX_UPLOAD_MB * 1024 * 1024

    def run():
        tmp = config.TMP_DIR / f"url_{mid}"
        try:
            rq = urllib.request.Request(url, headers={"User-Agent": "EstudioVideo/1.0"})
            with urllib.request.urlopen(rq, timeout=60) as r, open(tmp, "wb") as f:
                got = 0
                while chunk := r.read(1 << 20):
                    got += len(chunk)
                    if got > limit:
                        raise RuntimeError("archivo demasiado grande")
                    f.write(chunk)
            ext = Path(name).suffix.lower()
            if not re.match(r"^\.[a-z0-9]{1,5}$", ext):
                ext = ".bin"
            dst = config.MEDIA_DIR / f"{mid}{ext}"
            tmp.replace(dst)
            db.ex("UPDATE media SET path=? WHERE id=?", str(dst), mid)
            db.enqueue("ingest", pid, mid, {"auto_transcribe": bool(a.get("transcribir", True)),
                                            "language": a.get("idioma", "es")})
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            db.ex("UPDATE media SET status='error' WHERE id=?", mid)
            print(f"[mcp] importación fallida {url}: {e}", flush=True)

    threading.Thread(target=run, daemon=True).start()
    return {"medio_id": mid, "estado": "descargando", "nota": "Luego se procesa (y se transcribe si se pidió). Vea listar_medios."}


def t_eliminar_medio(user, req, a):
    m = _media(a["medio_id"], user, write=True)
    p, d = _load(m["project_id"])
    used = any(c["media_id"] == m["id"] for c in d["clips"]) or any(x["media_id"] == m["id"] for x in d["music"]) \
        or any((o.get("params") or {}).get("media_id") == m["id"] for o in d["overlays"])
    if used and a.get("confirmar") is not True:
        raise ToolError("El medio está en la línea de tiempo; se quitará de ella. Repita con confirmar=true")
    if used:
        d["clips"] = [c for c in d["clips"] if c["media_id"] != m["id"]]
        d["music"] = [x for x in d["music"] if x["media_id"] != m["id"]]
        d["overlays"] = [o for o in d["overlays"] if (o.get("params") or {}).get("media_id") != m["id"]]
        _save(m["project_id"], p["version"], d)
    for f in (m["path"], m["proxy_path"], m["waveform_path"]):
        if f:
            Path(f).unlink(missing_ok=True)
    db.ex("DELETE FROM media WHERE id=?", m["id"])
    return {"eliminado": m["filename"]}


def t_ver_fotograma(user, req, a):
    pid = a["proyecto_id"]
    _check(pid, user)
    p, d = _load(pid)
    t = float(a.get("tiempo_s", 0))
    i, c, t0 = _clip_at(d, t)
    if c is None:
        raise ToolError(f"No hay video en {t} s (duración {_fmt(_total(d))})")
    m = db.q1("SELECT * FROM media WHERE id=?", c["media_id"])
    src = m["proxy_path"] if m["proxy_path"] and Path(m["proxy_path"]).exists() else m["path"]
    st = c["in"] + (t - t0)
    args = ["ffmpeg", "-v", "error"] + (["-ss", f"{st:.3f}"] if m["kind"] == "video" else []) + \
        ["-i", src, "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "-"]
    out = subprocess.run(args, capture_output=True, timeout=60)
    if out.returncode != 0 or not out.stdout:
        raise ToolError("No se pudo extraer el fotograma")
    return {"__image__": base64.b64encode(out.stdout).decode(), "mime": "image/jpeg",
            "texto": f"Fotograma del video principal en {_fmt(t)} (clip {i + 1}, «{m['filename']}» a {_fmt(st)}). "
                     "No incluye capas, textos ni zoom."}


# ======================================================================= Línea de tiempo
def t_recortar_clip(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    c = _find(d["clips"], "id", a["clip_id"], "clip")
    m = db.q1("SELECT kind, duration FROM media WHERE id=?", c["media_id"]) or {"kind": "video", "duration": 1e9}
    maxd = 3600 if m["kind"] == "image" else m["duration"]
    if "entrada_s" in a:
        c["in"] = max(0.0, min(float(a["entrada_s"]), c["out"] - 0.05))
    if "salida_s" in a:
        c["out"] = max(c["in"] + 0.05, min(float(a["salida_s"]), maxd))
    _save(pid, p["version"], d)
    return {"clip_id": c["id"], "tramo_original": [round(c["in"], 2), round(c["out"], 2)], "duracion_total": _fmt(_total(d))}


def t_dividir(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    t = float(a["tiempo_s"])
    i, c, t0 = _clip_at(d, t)
    if c is None or t - t0 < 0.05 or t0 + _dur(c) - t < 0.05:
        raise ToolError("No hay un clip que dividir en ese punto (o está en un borde)")
    cut = c["in"] + (t - t0)
    nc = {**c, "id": _uid(), "in": cut}
    c["out"] = cut
    d["clips"].insert(i + 1, nc)
    _save(pid, p["version"], d)
    return {"clip_izquierdo": c["id"], "clip_derecho": nc["id"]}


def t_eliminar_clip(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    t0 = 0.0
    for c in d["clips"]:
        if c["id"] == a["clip_id"]:
            _cut(d, t0, t0 + _dur(c), _media_map(pid))
            _save(pid, p["version"], d)
            return {"eliminado": a["clip_id"], "duracion_total": _fmt(_total(d))}
        t0 += _dur(c)
    raise ToolError("Clip no encontrado")


def t_mover_clip(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    c = _find(d["clips"], "id", a["clip_id"], "clip")
    d["clips"].remove(c)
    pos = max(0, min(int(a["posicion"]) - 1, len(d["clips"])))
    d["clips"].insert(pos, c)
    _save(pid, p["version"], d)
    return {"orden": [x["id"] for x in d["clips"]],
            "nota": "Las capas, zooms y marcas no se mueven con el clip; revise sus tiempos con ver_proyecto."}


def t_ajustar_clip(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    c = _find(d["clips"], "id", a["clip_id"], "clip")
    if "silenciar" in a:
        c["mute"] = bool(a["silenciar"])
    if "ganancia_db" in a:
        c["gain_db"] = max(-20.0, min(12.0, float(a["ganancia_db"])))
    if "encuadre" in a:
        if a["encuadre"] not in ("contain", "cover"):
            raise ToolError("encuadre: contain (encajar) o cover (llenar)")
        c["fit"] = a["encuadre"]
    _save(pid, p["version"], d)
    return {k: c.get(k) for k in ("id", "mute", "gain_db", "fit")}


def t_eliminar_tramo(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    s, e = float(a["desde_s"]), float(a["hasta_s"])
    T = _total(d)
    s, e = max(0.0, s), min(T, e)
    if e - s < 0.02:
        raise ToolError("Tramo vacío")
    _cut(d, s, e, _media_map(pid))
    _save(pid, p["version"], d)
    return {"eliminado_s": round(e - s, 2), "duracion_antes": _fmt(T), "duracion_despues": _fmt(_total(d))}


# ======================================================================= Edición por texto
def t_buscar_en_transcripcion(user, req, a):
    pid = a["proyecto_id"]
    _check(pid, user)
    p, d = _load(pid)
    ws = _timeline_words(d, pid)
    if not ws:
        raise ToolError("Los clips de la línea de tiempo no tienen transcripción")
    q = [_norm(x) for x in str(a["texto"]).split() if _norm(x)]
    hits = []
    for i in range(len(ws) - len(q) + 1):
        if all(_norm(ws[i + k]["w"]) == q[k] for k in range(len(q))):
            j = i + len(q) - 1
            ctx = " ".join(w["w"].strip() for w in ws[max(0, i - 6): j + 7])
            prev_e = ws[i - 1]["e"] if i > 0 else 0.0
            next_s = ws[j + 1]["s"] if j + 1 < len(ws) else _total(d)
            hits.append({"desde_s": round(max(prev_e, ws[i]["s"] - 0.04), 3),
                         "hasta_s": round(min(next_s, ws[j]["e"] + 0.04), 3),
                         "inicio": _fmt(ws[i]["s"]), "contexto": ctx})
        if len(hits) >= 50:
            break
    return {"coincidencias": len(hits), "resultados": hits,
            "nota": "Para borrar una coincidencia del video, use eliminar_tramo con desde_s/hasta_s."}


def _fillers(ws):
    out = []
    for i, w in enumerate(ws):
        x = _norm(w["w"])
        if x in FILLERS:
            out.append(i)
        elif x in ("este", "esteee", "o sea"):
            pv = ws[i - 1] if i > 0 else None
            nx = ws[i + 1] if i + 1 < len(ws) else None
            if (not pv or w["s"] - pv["e"] > 0.25) and (not nx or nx["s"] - w["e"] > 0.25):
                out.append(i)
    return out


def t_muletillas(user, req, a):
    pid = a["proyecto_id"]
    eliminar = bool(a.get("eliminar"))
    _check(pid, user, eliminar)
    p, d = _load(pid)
    ws = _timeline_words(d, pid)
    if not ws:
        raise ToolError("Los clips de la línea de tiempo no tienen transcripción")
    idx = _fillers(ws)
    ranges = []
    for i in idx:
        w = ws[i]
        pv = ws[i - 1]["e"] if i > 0 else 0.0
        nx = ws[i + 1]["s"] if i + 1 < len(ws) else _total(d)
        ranges.append([max(pv, w["s"] - 0.03), min(nx, w["e"] + 0.03)])
    res = {"muletillas": len(idx), "lista": [{"palabra": ws[i]["w"], "inicio": _fmt(ws[i]["s"])} for i in idx][:100]}
    if eliminar and ranges:
        media = _media_map(pid)
        for x, y in reversed(ranges):
            _cut(d, x, y, media)
        _save(pid, p["version"], d)
        res["eliminadas"] = len(ranges)
        res["duracion_despues"] = _fmt(_total(d))
    return res


def t_corregir_transcripcion(user, req, a):
    m = _media(a["medio_id"], user, write=True)
    r = db.q1("SELECT * FROM transcripts WHERE media_id=?", m["id"])
    if not r:
        raise ToolError("Sin transcripción")
    words = db.jload(r["words"], [])
    buscar, nuevo = _norm(a["buscar"]), str(a["reemplazar"]).strip()
    n = 0
    for w in words:
        if _norm(w["w"]) == buscar:
            trail = re.search(r"[.,;:?!…]+$", w["w"].strip())
            w["w"] = nuevo + (trail.group(0) if trail and not re.search(r"[.,;:?!…]$", nuevo) else "")
            n += 1
    if n:
        db.ex("UPDATE transcripts SET words=? WHERE media_id=?", db.jdump(words), m["id"])
    return {"reemplazos": n, "nota": "Afecta los subtítulos de las próximas exportaciones."}


# ======================================================================= Capas
def _place(o, a, W0, H0):
    for k_in, k in (("x", "x"), ("y", "y")):
        if f"{k_in}_pct" in a:
            o[k] = float(a[f"{k_in}_pct"]) / 100
    if "ancho_pct" in a:
        o["w"] = max(0.02, min(1.5, float(a["ancho_pct"]) / 100))


def t_agregar_capa(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    tipo = a["tipo"]
    if tipo not in LAYER_DEFAULTS:
        raise ToolError("tipo: " + ", ".join(LAYER_DEFAULTS))
    defaults, w = LAYER_DEFAULTS[tipo]
    params = {**defaults}
    for k, v in (a.get("parametros") or {}).items():
        if k in defaults or k in ("legend",):
            params[k] = v
    s = float(a.get("inicio_s", 0))
    e = float(a.get("fin_s", s + 5))
    o = {"id": _uid(), "type": tipo, "params": params, "start": s, "end": max(e, s + 0.1),
         "x": 0.05, "y": 0.08, "w": w, "h": 0.1, "opacity": 1, "fade": 0.25, "asset_id": None}
    if tipo == "lower":
        o["y"] = 0.72
    if tipo in ("latex", "chem", "molecule"):
        o["x"] = 0.6
    if tipo in ("arrow", "box"):
        o.update(x=0.45, y=0.4, fade=0.15)
    _place(o, a, d["settings"]["width"], d["settings"]["height"])
    if a.get("sombra"):
        o["shadow"] = {"on": True, "color": "#000000", "opacity": 0.6, "dist": 0.5, "angle": 45, "blur": 0.5}
    d["overlays"].append(o)
    _save(pid, p["version"], d)
    return {"capa_id": o["id"], "nota": "Las capas gráficas se dibujan (LaTeX, química, moléculas, texto) la próxima vez "
            "que se abre el editor; ábralo una vez antes de exportar."}


def t_superponer_medio(user, req, a):
    m = _media(a["medio_id"], user, write=True)
    pid = m["project_id"]
    if m["status"] != "ready" or m["kind"] not in ("video", "image"):
        raise ToolError("Debe ser un video o una imagen ya procesados")
    p, d = _load(pid)
    W0, H0 = d["settings"]["width"], d["settings"]["height"]
    s = float(a.get("inicio_s", 0))
    T = _total(d)
    ln = float(a["fin_s"]) - s if a.get("fin_s") is not None else (min(m["duration"], max(0.5, T - s)) if m["kind"] == "video" else 5)
    ar = (m["height"] / m["width"]) if m["width"] and m["height"] else 9 / 16
    pos = a.get("posicion", "esquina_sup_der")
    w = float(a.get("ancho_pct", 32)) / 100
    hr = w * ar * W0 / H0
    presets = {"esquina_sup_der": (1 - w - 0.03, 0.04), "esquina_inf_der": (1 - w - 0.03, 1 - hr - 0.05),
               "esquina_inf_izq": (0.03, 1 - hr - 0.05), "esquina_sup_izq": (0.03, 0.04),
               "centro": ((1 - w) / 2, (1 - hr) / 2)}
    if pos == "pantalla_completa":
        w = 1.0
        x, y = 0.0, max(0.0, (1 - ar * W0 / H0) / 2)
    elif pos in ("mitad_izquierda", "mitad_derecha"):
        w = 0.5
        x, y = (0.0 if pos == "mitad_izquierda" else 0.5), max(0.0, (1 - 0.5 * ar * W0 / H0) / 2)
    else:
        x, y = presets.get(pos, presets["esquina_sup_der"])
    o = {"id": _uid(), "type": "media", "params": {"media_id": m["id"]}, "start": s, "end": s + ln,
         "in": float(a.get("desde_original_s", 0)), "x": x, "y": y, "w": w, "h": w * ar * W0 / H0,
         "opacity": 1, "fade": 0.2, "volume": 1, "mute": bool(a.get("silenciar", False))}
    d["overlays"].append(o)
    _save(pid, p["version"], d)
    return {"capa_id": o["id"], "orden": f"capa {len(d['overlays'])} (al frente)"}


def t_modificar_capa(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    o = _find(d["overlays"], "id", a["capa_id"], "capa")
    if "inicio_s" in a:
        o["start"] = max(0.0, float(a["inicio_s"]))
    if "fin_s" in a:
        o["end"] = max(o["start"] + 0.1, float(a["fin_s"]))
    _place(o, a, d["settings"]["width"], d["settings"]["height"])
    if "opacidad" in a:
        o["opacity"] = max(0.1, min(1.0, float(a["opacidad"])))
    if "fundido_s" in a:
        o["fade"] = max(0.0, min(2.0, float(a["fundido_s"])))
    if "volumen" in a:
        o["volume"] = max(0.0, min(1.0, float(a["volumen"])))
    if "silenciar" in a:
        o["mute"] = bool(a["silenciar"])
    if "parametros" in a and o.get("type") != "media":
        o["params"] = {**(o.get("params") or {}), **a["parametros"]}
        o["asset_id"] = None                      # se vuelve a dibujar al abrir el editor
    if "sombra" in a:
        sh = a["sombra"]
        if sh is False or (isinstance(sh, dict) and sh.get("activa") is False):
            o["shadow"] = {**(o.get("shadow") or {}), "on": False}
        else:
            base = {"on": True, "color": "#000000", "opacity": 0.6, "dist": 0.5, "angle": 45, "blur": 0.5}
            presets = {"suave": {"opacity": 0.5, "dist": 0.4, "angle": 45, "blur": 0.6},
                       "marcada": {"opacity": 0.85, "dist": 0.8, "angle": 45, "blur": 0.12},
                       "flotante": {"opacity": 0.45, "dist": 1.6, "angle": 70, "blur": 1.4},
                       "resplandor": {"opacity": 0.9, "dist": 0, "angle": 45, "blur": 0.9}}
            sh = sh if isinstance(sh, dict) else {}
            new = {**base, **(o.get("shadow") or {}), "on": True, **presets.get(sh.get("estilo", ""), {})}
            for k_es, k in (("color", "color"), ("opacidad", "opacity"), ("distancia_pct", "dist"),
                            ("angulo", "angle"), ("desenfoque_pct", "blur")):
                if k_es in sh:
                    new[k] = sh[k_es]
            o["shadow"] = new
    _save(pid, p["version"], d)
    return {"capa": {k: o.get(k) for k in ("id", "type", "start", "end", "x", "y", "w", "opacity", "fade", "shadow")}}


def t_ordenar_capa(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    L = d["overlays"]
    o = _find(L, "id", a["capa_id"], "capa")
    i = L.index(o)
    acc = a["accion"]
    if acc in ("subir", "bajar"):
        step = 1 if acc == "subir" else -1
        j = i + step
        while 0 <= j < len(L) and not (L[j]["start"] < o["end"] and L[j]["end"] > o["start"]):
            j += step
        if not 0 <= j < len(L):
            raise ToolError("Ya está en el extremo de las capas de ese tramo")
        L.pop(i)
        L.insert(j, o)
    elif acc == "al_frente":
        L.append(L.pop(i))
    elif acc == "al_fondo":
        L.insert(0, L.pop(i))
    else:
        raise ToolError("accion: subir, bajar, al_frente o al_fondo")
    _save(pid, p["version"], d)
    return {"orden_de_abajo_hacia_arriba": [x["id"] for x in L if x["start"] < o["end"] and x["end"] > o["start"]]}


def t_eliminar_elemento(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    coll = {"capa": "overlays", "zoom": "zooms", "audio": "music", "capitulo": "chapters", "pregunta": "quizzes"}[a["tipo"]]
    before = len(d[coll])
    d[coll] = [x for x in d[coll] if x.get("id") != a["id"]]
    if len(d[coll]) == before:
        raise ToolError("No encontrado")
    _save(pid, p["version"], d)
    return {"eliminado": a["id"]}


# ======================================================================= Zoom, audio, logo, subtítulos, guion
def t_agregar_zoom(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    s = float(a["inicio_s"])
    z = {"id": _uid(), "start": s, "end": float(a.get("fin_s", s + 4)), "x": float(a.get("x_pct", 50)) / 100,
         "y": float(a.get("y_pct", 50)) / 100, "scale": max(1.1, min(4.0, float(a.get("escala", 1.8))))}
    d["zooms"].append(z)
    _save(pid, p["version"], d)
    return {"zoom_id": z["id"]}


def t_configurar_audio(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    au = d.setdefault("audio", {})
    if "reduccion_ruido" in a:
        au["denoise"] = {"desactivada": "off", "suave": "light", "intensa": "strong"}.get(a["reduccion_ruido"], a["reduccion_ruido"])
    for k_es, k in (("paso_alto", "highpass"), ("de_esser", "deess"), ("compresor", "compress")):
        if k_es in a:
            au[k] = bool(a[k_es])
    if "ganancia_voz_db" in a:
        au["voice_gain_db"] = max(-12, min(12, int(a["ganancia_voz_db"])))
    if "sonoridad_lufs" in a:
        if int(a["sonoridad_lufs"]) not in (-14, -16, -23):
            raise ToolError("sonoridad_lufs: -14, -16 o -23")
        au["loudness"] = int(a["sonoridad_lufs"])
    _save(pid, p["version"], d)
    return {"audio": au}


def t_agregar_audio(user, req, a):
    m = _media(a["medio_id"], user, write=True)
    if m["kind"] != "audio" or m["status"] != "ready":
        raise ToolError("Debe ser un archivo de audio ya procesado")
    p, d = _load(m["project_id"])
    mu = {"id": _uid(), "media_id": m["id"], "start": float(a.get("inicio_s", 0)), "in": 0, "out": m["duration"],
          "volume": max(0.0, min(1.5, float(a.get("volumen", 1.0 if a.get("rol") == "voice" else 0.3)))),
          "duck": bool(a.get("atenuar_con_voz", True)), "role": "voice" if a.get("rol") == "voice" else "music"}
    d["music"].append(mu)
    _save(m["project_id"], p["version"], d)
    return {"pista_id": mu["id"], "rol": "narración" if mu["role"] == "voice" else "música"}


def t_modificar_audio(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    mu = _find(d["music"], "id", a["pista_id"], "pista de audio")
    if "inicio_s" in a:
        mu["start"] = max(0.0, float(a["inicio_s"]))
    if "volumen" in a:
        mu["volume"] = max(0.0, min(1.5, float(a["volumen"])))
    if "atenuar_con_voz" in a:
        mu["duck"] = bool(a["atenuar_con_voz"])
    if "desde_original_s" in a:
        mu["in"] = max(0.0, float(a["desde_original_s"]))
    if "hasta_original_s" in a:
        mu["out"] = max(mu.get("in", 0) + 0.1, float(a["hasta_original_s"]))
    _save(pid, p["version"], d)
    return {"pista": mu}


def t_configurar_logo(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    b = d.setdefault("brand", {})
    if a.get("medio_id") == "":
        b["logo_asset_id"] = None
    elif a.get("medio_id"):
        m = _media(a["medio_id"], user, write=True)
        if m["kind"] != "image" or m["project_id"] != pid:
            raise ToolError("El logo debe ser una imagen de la biblioteca de este proyecto")
        aid = db.new_id()
        dst = config.ASSET_DIR / f"{aid}.png"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", m["path"], "-frames:v", "1", str(dst)], capture_output=True)
        if r.returncode != 0:
            raise ToolError("No se pudo convertir la imagen")
        db.ex("INSERT INTO assets(id, project_id, path, created_at) VALUES (?,?,?,?)", aid, pid, str(dst), db.now())
        b["logo_asset_id"] = aid
    if "posicion" in a:
        b["position"] = {"sup_der": "tr", "sup_izq": "tl", "inf_der": "br", "inf_izq": "bl"}.get(a["posicion"], a["posicion"])
    if "tamano_pct" in a:
        b["size"] = max(0.05, min(0.3, float(a["tamano_pct"]) / 100))
    if "opacidad" in a:
        b["opacity"] = max(0.2, min(1.0, float(a["opacidad"])))
    _save(pid, p["version"], d)
    return {"logo": b}


def t_configurar_subtitulos(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid)
    c = d.setdefault("captions", {})
    if "pista_activable" in a:
        c["soft"] = bool(a["pista_activable"])
    if "incrustar" in a:
        c["burn"] = bool(a["incrustar"])
    if "color" in a:
        if not re.match(r"^#[0-9a-fA-F]{6}$", a["color"]):
            raise ToolError("color en formato #RRGGBB")
        c["color"] = a["color"]
    _save(pid, p["version"], d)
    return {"subtitulos": c}


def t_guion_teleprompter(user, req, a):
    pid = a["proyecto_id"]
    p, d = _project_of_item(user, pid, write="texto" in a)
    if "texto" in a:
        d["teleprompter"] = str(a["texto"])[:100000]
        _save(pid, p["version"], d)
    return {"guion": d.get("teleprompter", "")}


# ======================================================================= Comentarios, analítica, exportaciones
def t_comentarios(user, req, a):
    pid = a["proyecto_id"]
    _check(pid, user)
    if a.get("texto"):
        db.ex("INSERT INTO comments(project_id, user_id, t, text, created_at) VALUES (?,?,?,?,?)",
              pid, user["id"], float(a.get("tiempo_s", 0)), str(a["texto"])[:2000], db.now())
    if a.get("resolver_id"):
        db.ex("UPDATE comments SET resolved=1 WHERE id=? AND project_id=?", int(a["resolver_id"]), pid)
    rows = db.q("SELECT c.id, c.t, c.text, c.resolved, u.username FROM comments c JOIN users u ON u.id=c.user_id "
                "WHERE c.project_id=? ORDER BY c.t", pid)
    return {"comentarios": [{"id": r["id"], "tiempo": _fmt(r["t"]), "autor": r["username"], "texto": r["text"],
                             "resuelto": bool(r["resolved"])} for r in rows]}


def t_ver_analitica(user, req, a):
    from .main import export_analytics
    try:
        r = export_analytics(a["exportacion_id"], user)
    except Exception as e:  # noqa: BLE001
        raise ToolError(getattr(e, "detail", str(e)))
    ret = r["retention"]
    peak = max(ret) if ret else 0
    return {"vistas": r["views"], "completadas_90pct": r["completed"],
            "retencion_por_tramo_5s": [{"desde": _fmt(i * 5), "espectadores": v} for i, v in enumerate(ret)],
            "tramo_mas_visto": _fmt(ret.index(peak) * 5) if peak else None,
            "preguntas": [{"pregunta": q["question"], "respuestas": q["answers"], "aciertos": q["right"],
                           "por_opcion": dict(zip(q["options"], q["counts"]))} for q in r["quizzes"]]}


def t_eliminar_exportacion(user, req, a):
    e = db.q1("SELECT * FROM exports WHERE id=?", a["exportacion_id"])
    if not e:
        raise ToolError("Exportación no encontrada")
    _check(e["project_id"], user, True)
    if a.get("confirmar") is not True:
        raise ToolError("Se borrará el video y su enlace público. Repita con confirmar=true")
    shutil.rmtree(config.EXPORT_DIR / e["id"], ignore_errors=True)
    db.ex("DELETE FROM exports WHERE id=?", e["id"])
    return {"eliminada": e["id"]}


# ======================================================================= Quitar fondo de un clip
def t_quitar_fondo_clip(user, req, a):
    from .main import RemoveBgIn, media_remove_bg
    pid = a["proyecto_id"]
    _check(pid, user, True)
    p, d = _load(pid)
    c = _find(d["clips"], "id", a["clip_id"], "clip")
    res = a.get("resultado", "capa")
    body = RemoveBgIn(mode="transparent" if res == "capa" else a.get("modo", "blur"), color=a.get("color", "#ffffff"),
                      blur=int(a.get("desenfoque", 25)), quality=a.get("calidad", "normal"),
                      max_height=int(a.get("resolucion", 720)), image_media_id=a.get("imagen_medio_id"),
                      start=c["in"], end=c["out"], target="clip", item_id=c["id"],
                      placement="layer" if res == "capa" else "replace")
    try:
        r = media_remove_bg(c["media_id"], body, user)
    except Exception as e:  # noqa: BLE001
        raise ToolError(getattr(e, "detail", str(e)))
    # El procesador aplica el resultado al proyecto al terminar (aunque el editor esté cerrado)
    j = db.q1("SELECT params FROM jobs WHERE id=?", r["job_id"])
    prm = db.jload(j["params"], {})
    prm["server_apply"] = True
    db.ex("UPDATE jobs SET params=? WHERE id=?", db.jdump(prm), r["job_id"])
    return {"trabajo_id": r["job_id"], "nuevo_medio_id": r["id"],
            "al_terminar": "la persona recortada se añade como capa encima del clip" if res == "capa"
            else "el clip se reemplaza por la versión con fondo nuevo"}


EXTRA = {
    "renombrar_proyecto": (t_renombrar_proyecto, "Cambia el nombre de un proyecto.", {**PID, "nombre": _s("string", "Nombre nuevo")}, ["proyecto_id", "nombre"], WR),
    "eliminar_proyecto": (t_eliminar_proyecto, "Elimina definitivamente un proyecto con sus medios y exportaciones (requiere confirmar=true).", {**PID, "confirmar": _s("boolean", "true para confirmar")}, ["proyecto_id"], {**WR, "destructiveHint": True}),
    "compartir_proyecto": (t_compartir_proyecto, "Comparte el proyecto con otro usuario como editor o revisor (o le quita el acceso).", {**PID, "usuario": _s("string", "Nombre de usuario"), "rol": _s("string", "Rol", enum=["editor", "revisor", "quitar"], default="editor")}, ["proyecto_id", "usuario"], WR),
    "importar_medio_url": (t_importar_medio_url, "Descarga un video, audio o imagen desde una URL pública y lo añade a la biblioteca del proyecto (se procesa y, si se pide, se transcribe).", {**PID, "url": _s("string", "URL http(s) del archivo"), "nombre_archivo": _s("string", "Nombre con extensión (p. ej., clase.mp4)"), "transcribir": _s("boolean", "Transcribir al terminar", default=True), "idioma": _s("string", "Idioma del audio", default="es")}, ["proyecto_id", "url"], {**WR, "openWorldHint": True}),
    "eliminar_medio": (t_eliminar_medio, "Elimina un medio de la biblioteca (y de la línea de tiempo si se usa, con confirmar=true).", {"medio_id": _s("string", "ID del medio"), "confirmar": _s("boolean", "Confirmar si está en uso")}, ["medio_id"], {**WR, "destructiveHint": True}),
    "ver_fotograma": (t_ver_fotograma, "Devuelve una imagen del video principal en un instante de la línea de tiempo (para ver qué se muestra).", {**PID, "tiempo_s": _s("number", "Tiempo en la línea (s)")}, ["proyecto_id", "tiempo_s"], RO),
    "recortar_clip": (t_recortar_clip, "Cambia la entrada y/o salida (en segundos del archivo original) de un clip de la secuencia.", {**PID, "clip_id": _s("string", "ID del clip (ver_proyecto)"), "entrada_s": _s("number", "Nueva entrada"), "salida_s": _s("number", "Nueva salida")}, ["proyecto_id", "clip_id"], WR),
    "dividir": (t_dividir, "Divide en dos el clip que está en un tiempo de la línea.", {**PID, "tiempo_s": _s("number", "Tiempo (s)")}, ["proyecto_id", "tiempo_s"], WR),
    "eliminar_clip": (t_eliminar_clip, "Quita un clip de la secuencia (cierra el hueco y ajusta capas y marcas).", {**PID, "clip_id": _s("string", "ID del clip")}, ["proyecto_id", "clip_id"], {**WR, "destructiveHint": True}),
    "mover_clip": (t_mover_clip, "Cambia la posición (1 = primero) de un clip en la secuencia.", {**PID, "clip_id": _s("string", "ID del clip"), "posicion": _s("integer", "Posición nueva, desde 1")}, ["proyecto_id", "clip_id", "posicion"], WR),
    "ajustar_clip": (t_ajustar_clip, "Silencia, cambia la ganancia o el encuadre (encajar/llenar) de un clip.", {**PID, "clip_id": _s("string", "ID del clip"), "silenciar": _s("boolean", "Silenciar su audio"), "ganancia_db": _s("number", "-20 a +12 dB"), "encuadre": _s("string", "contain = encajar, cover = llenar", enum=["contain", "cover"])}, ["proyecto_id", "clip_id"], WR),
    "eliminar_tramo": (t_eliminar_tramo, "Elimina del video un tramo de la línea de tiempo (como borrar palabras en la edición por texto).", {**PID, "desde_s": _s("number", "Inicio (s)"), "hasta_s": _s("number", "Fin (s)")}, ["proyecto_id", "desde_s", "hasta_s"], {**WR, "destructiveHint": True}),
    "buscar_en_transcripcion": (t_buscar_en_transcripcion, "Busca una palabra o frase en lo que se dice en la línea de tiempo y devuelve dónde aparece (con el tramo exacto para eliminarla).", {**PID, "texto": _s("string", "Palabra o frase")}, ["proyecto_id", "texto"], RO),
    "muletillas": (t_muletillas, "Detecta muletillas (eh, em, este…) en la línea de tiempo y, con eliminar=true, las recorta.", {**PID, "eliminar": _s("boolean", "Recortarlas del video", default=False)}, ["proyecto_id"], WR),
    "corregir_transcripcion": (t_corregir_transcripcion, "Corrige una palabra mal transcrita en todas sus apariciones (afecta los subtítulos).", {"medio_id": _s("string", "ID del medio"), "buscar": _s("string", "Palabra tal como aparece"), "reemplazar": _s("string", "Palabra correcta")}, ["medio_id", "buscar", "reemplazar"], WR),
    "agregar_capa": (t_agregar_capa, "Agrega una capa gráfica: text (texto), lower (rótulo nombre/cargo), latex (ecuación), chem (reacción mhchem), molecule (estructura desde SMILES), arrow (flecha) o box (recuadro).", {**PID, "tipo": _s("string", "Tipo de capa", enum=list(LAYER_DEFAULTS)), "parametros": _s("object", "text: {text,size,color,bg,bgOpacity,bold,align}; lower: {title,subtitle}; latex/chem: {tex,color,bg,bgOpacity}; molecule: {smiles,legend,card,explicitH}; arrow: {color,angle,thickness}; box: {color,thickness,shape: rect|ellipse,fill}"), "inicio_s": _s("number", "Inicio (s)", default=0), "fin_s": _s("number", "Fin (s)"), "x_pct": _s("number", "Posición X (% del ancho)"), "y_pct": _s("number", "Posición Y (% del alto)"), "ancho_pct": _s("number", "Ancho (% del cuadro)"), "sombra": _s("boolean", "Con sombra")}, ["proyecto_id", "tipo"], WR),
    "superponer_medio": (t_superponer_medio, "Superpone un video o imagen de la biblioteca como capa (imagen en imagen, B-roll, pantalla dividida, diapositiva).", {"medio_id": _s("string", "ID del medio"), "inicio_s": _s("number", "Inicio en la línea (s)", default=0), "fin_s": _s("number", "Fin (s)"), "posicion": _s("string", "Ubicación", enum=["esquina_sup_der", "esquina_inf_der", "esquina_inf_izq", "esquina_sup_izq", "centro", "pantalla_completa", "mitad_izquierda", "mitad_derecha"], default="esquina_sup_der"), "ancho_pct": _s("number", "Ancho (% del cuadro)", default=32), "desde_original_s": _s("number", "Desde qué segundo del video", default=0), "silenciar": _s("boolean", "Silenciar su sonido", default=False)}, ["medio_id"], WR),
    "modificar_capa": (t_modificar_capa, "Cambia tiempo, posición, tamaño, opacidad, fundido, volumen, contenido (parametros) o sombra de una capa. sombra: false para quitarla, o {estilo: suave|marcada|flotante|resplandor, color, opacidad, distancia_pct, angulo, desenfoque_pct}.", {**PID, "capa_id": _s("string", "ID de la capa"), "inicio_s": _s("number", "Inicio"), "fin_s": _s("number", "Fin"), "x_pct": _s("number", "X %"), "y_pct": _s("number", "Y %"), "ancho_pct": _s("number", "Ancho %"), "opacidad": _s("number", "0.1–1"), "fundido_s": _s("number", "0–2 s"), "volumen": _s("number", "0–1 (videos)"), "silenciar": _s("boolean", "Silenciar (videos)"), "parametros": _s("object", "Contenido de la capa gráfica (mismos campos que agregar_capa)"), "sombra": {"description": "false o un objeto de sombra", "anyOf": [{"type": "boolean"}, {"type": "object"}]}}, ["proyecto_id", "capa_id"], WR),
    "ordenar_capa": (t_ordenar_capa, "Cambia el orden de una capa para intercalarla entre otras: subir/bajar una posición (entre las que coinciden en el tiempo), al_frente o al_fondo.", {**PID, "capa_id": _s("string", "ID de la capa"), "accion": _s("string", "Acción", enum=["subir", "bajar", "al_frente", "al_fondo"])}, ["proyecto_id", "capa_id", "accion"], WR),
    "eliminar_elemento": (t_eliminar_elemento, "Elimina una capa, zoom, pista de audio, capítulo o pregunta por su ID.", {**PID, "tipo": _s("string", "Tipo", enum=["capa", "zoom", "audio", "capitulo", "pregunta"]), "id": _s("string", "ID del elemento")}, ["proyecto_id", "tipo", "id"], {**WR, "destructiveHint": True}),
    "agregar_zoom": (t_agregar_zoom, "Agrega un acercamiento animado a un punto del cuadro (útil en pantallas de software y fórmulas pequeñas).", {**PID, "inicio_s": _s("number", "Inicio (s)"), "fin_s": _s("number", "Fin (s)"), "x_pct": _s("number", "Centro X (%)", default=50), "y_pct": _s("number", "Centro Y (%)", default=50), "escala": _s("number", "Aumento 1.1–4", default=1.8)}, ["proyecto_id", "inicio_s"], WR),
    "configurar_audio": (t_configurar_audio, "Ajusta el procesamiento de la voz: reducción de ruido, paso alto, de-esser, compresor, ganancia y sonoridad final (LUFS).", {**PID, "reduccion_ruido": _s("string", "Nivel", enum=["desactivada", "suave", "intensa"]), "paso_alto": _s("boolean", "Filtro 80 Hz"), "de_esser": _s("boolean", "Suavizar sibilantes"), "compresor": _s("boolean", "Nivelar la voz"), "ganancia_voz_db": _s("integer", "-12 a 12"), "sonoridad_lufs": _s("integer", "-14 redes, -16 educativo, -23 TV", enum=[-14, -16, -23])}, ["proyecto_id"], WR),
    "agregar_audio": (t_agregar_audio, "Añade un audio de la biblioteca como narración (voice) o música de fondo (music, se atenúa cuando hay voz).", {"medio_id": _s("string", "ID del audio"), "rol": _s("string", "Rol", enum=["voice", "music"], default="music"), "inicio_s": _s("number", "Inicio (s)", default=0), "volumen": _s("number", "0–1.5"), "atenuar_con_voz": _s("boolean", "Ducking automático", default=True)}, ["medio_id"], WR),
    "modificar_audio": (t_modificar_audio, "Cambia inicio, volumen, atenuación o tramo de una pista de audio.", {**PID, "pista_id": _s("string", "ID de la pista"), "inicio_s": _s("number", "Inicio"), "volumen": _s("number", "0–1.5"), "atenuar_con_voz": _s("boolean", "Ducking"), "desde_original_s": _s("number", "Recorte inicial"), "hasta_original_s": _s("number", "Recorte final")}, ["proyecto_id", "pista_id"], WR),
    "configurar_logo": (t_configurar_logo, "Pone (o quita, con medio_id vacío) el logo institucional permanente y ajusta su posición, tamaño y opacidad.", {**PID, "medio_id": _s("string", "Imagen de la biblioteca; vacío para quitar"), "posicion": _s("string", "Esquina", enum=["sup_der", "sup_izq", "inf_der", "inf_izq"]), "tamano_pct": _s("number", "5–30 % del ancho"), "opacidad": _s("number", "0.2–1")}, ["proyecto_id"], WR),
    "configurar_subtitulos": (t_configurar_subtitulos, "Configura los subtítulos: pista activable en el MP4, incrustados en la imagen y color.", {**PID, "pista_activable": _s("boolean", "Pista seleccionable"), "incrustar": _s("boolean", "Grabarlos en la imagen"), "color": _s("string", "#RRGGBB")}, ["proyecto_id"], WR),
    "guion_teleprompter": (t_guion_teleprompter, "Lee o escribe el guion del teleprompter que se muestra al grabar.", {**PID, "texto": _s("string", "Guion nuevo (omitir para leerlo)")}, ["proyecto_id"], WR),
    "comentarios": (t_comentarios, "Lista los comentarios de revisión anclados al tiempo; opcionalmente agrega uno o marca uno como resuelto.", {**PID, "texto": _s("string", "Comentario nuevo"), "tiempo_s": _s("number", "Tiempo del comentario"), "resolver_id": _s("integer", "ID a marcar como resuelto")}, ["proyecto_id"], WR),
    "ver_analitica": (t_ver_analitica, "Analítica de un video publicado: vistas, finalizaciones, retención por tramos de 5 s y respuestas a las preguntas.", {"exportacion_id": _s("string", "ID de la exportación")}, ["exportacion_id"], RO),
    "eliminar_exportacion": (t_eliminar_exportacion, "Borra una exportación y su enlace público (requiere confirmar=true).", {"exportacion_id": _s("string", "ID"), "confirmar": _s("boolean", "true para confirmar")}, ["exportacion_id"], {**WR, "destructiveHint": True}),
    "quitar_fondo_clip": (t_quitar_fondo_clip, "Quita el fondo de un clip de la línea de tiempo. resultado=capa (predeterminado): la persona recortada se añade encima del clip, alineada; resultado=reemplazar: el clip se reemplaza por la versión con fondo nuevo (modo blur, color o image).", {**PID, "clip_id": _s("string", "ID del clip"), "resultado": _s("string", "Qué hacer", enum=["capa", "reemplazar"], default="capa"), "modo": _s("string", "Fondo si resultado=reemplazar", enum=["blur", "color", "image"]), "color": _s("string", "#RRGGBB"), "imagen_medio_id": _s("string", "Imagen de fondo"), "desenfoque": _s("integer", "5–60"), "calidad": _s("string", "normal o rapida", enum=["normal", "rapida"]), "resolucion": _s("integer", "Altura", enum=[480, 720, 1080])}, ["proyecto_id", "clip_id"], WR),
}

TOOLS.update(EXTRA)
