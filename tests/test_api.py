"""Prueba de punta a punta contra un servidor en ejecución (web + worker).

Uso: BASE=http://localhost:8765 ADMIN_PASSWORD=... python -m tests.test_api /ruta/medios_de_prueba
Inserta una transcripción simulada (no requiere descargar el modelo Whisper).
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
import zipfile
from http.cookiejar import CookieJar
from pathlib import Path

BASE = os.environ.get("BASE", "http://localhost:8765")
jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method, path, body=None, raw=None, expect=200):
    data, headers = None, {"X-Requested-With": "estudio"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if raw is not None:
        data = raw
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with opener.open(req) as r:
            out = r.read()
            status = r.status
            ctype = r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        out, status, ctype = e.read(), e.code, e.headers.get("content-type", "")
    assert status == expect, f"{method} {path} -> {status}: {out[:300]}"
    return json.loads(out) if "json" in ctype else out


def wait_jobs(pid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = call("GET", f"/api/projects/{pid}/jobs")
        act = [x for x in j["jobs"] if x["status"] in ("queued", "running")]
        errs = [x for x in j["jobs"] if x["status"] == "error"]
        assert not errs, errs
        if not act:
            return j["jobs"]
        time.sleep(1)
    raise TimeoutError


def main(src):
    d = Path(src)
    # Seguridad básica
    call("GET", "/api/me", expect=401)
    call("POST", "/api/login", {"username": "admin", "password": "mala"}, expect=401)
    call("POST", "/api/login", {"username": "admin", "password": os.environ["ADMIN_PASSWORD"]})
    me = call("GET", "/api/me")
    assert me["role"] == "admin"
    # CSRF: sin cabecera debe fallar
    req = urllib.request.Request(BASE + "/api/projects", data=b'{"name":"x"}', method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        opener.open(req)
        raise AssertionError("CSRF no bloqueado")
    except urllib.error.HTTPError as e:
        assert e.code == 403
    call("POST", "/api/users", {"username": "colega", "name": "Colega Revisor", "password": "Revisor123", "role": "docente"})

    pid = call("POST", "/api/projects", {"name": "Prueba de alcanos", "format": "16:9"})["id"]
    ids = {}
    for name in ("clip.mp4", "music.mp3", "img.jpg"):
        r = call("PUT", f"/api/projects/{pid}/media?filename={name}", raw=(d / name).read_bytes())
        ids[name] = r["id"]
    # Subida por fragmentos (la usa la interfaz para archivos grandes)
    blob = (d / "clip.mp4").read_bytes()
    up = call("POST", f"/api/projects/{pid}/uploads", {"filename": "clip2.mp4", "size": len(blob)})["upload_id"]
    half = len(blob) // 2
    call("PUT", f"/api/uploads/{up}?offset=0", raw=blob[:half])
    call("PUT", f"/api/uploads/{up}?offset=0", raw=blob[half:], expect=409)
    call("POST", f"/api/uploads/{up}/complete", {}, expect=400)
    call("PUT", f"/api/uploads/{up}?offset={half}", raw=blob[half:])
    assert call("GET", f"/api/uploads/{up}")["received"] == len(blob)
    call("POST", f"/api/uploads/{up}/complete", {"auto_transcribe": False})
    wait_jobs(pid)
    media = {m["filename"]: m for m in call("GET", f"/api/projects/{pid}/media")}
    assert media["clip2.mp4"]["status"] == "ready" and abs(media["clip2.mp4"]["duration"] - 20) < 0.2
    assert all(m["status"] == "ready" for m in media.values()), media
    assert media["clip.mp4"]["kind"] == "video" and media["music.mp3"]["kind"] == "audio"
    # Vista previa con soporte de rangos (necesario para buscar en el <video>)
    req = urllib.request.Request(BASE + f"/api/media/{ids['clip.mp4']}/proxy", headers={"Range": "bytes=0-99"})
    with opener.open(req) as r:
        assert r.status == 206 and len(r.read()) == 100

    # Transcripción simulada (en producción la genera Whisper)
    db = sqlite3.connect(Path(os.environ["DATA_DIR"]) / "estudio.db")
    words = [{"w": "Hola", "s": 1.2, "e": 1.6, "p": .9}, {"w": "estudiantes,", "s": 1.7, "e": 2.4, "p": .9},
             {"w": "eh", "s": 2.6, "e": 2.8, "p": .5}, {"w": "hoy", "s": 3.0, "e": 3.3, "p": .9},
             {"w": "veremos", "s": 3.4, "e": 3.8, "p": .9}, {"w": "alcanos.", "s": 3.9, "e": 4.4, "p": .9}]
    db.execute("INSERT INTO transcripts(media_id, language, words, translated, created_at) VALUES (?,?,?,?,?)",
               (ids["clip.mp4"], "es", json.dumps(words),
                json.dumps([{"s": 1.2, "e": 4.4, "text": "Hello students, today we will study alkanes."}]), time.time()))
    db.commit()
    tr = call("GET", f"/api/media/{ids['clip.mp4']}/transcript")
    assert len(tr["words"]) == 6

    asset = call("POST", f"/api/projects/{pid}/assets", raw=(d / "ov.png").read_bytes())["id"]
    call("POST", f"/api/projects/{pid}/assets", raw=b"no es png", expect=400)
    p = call("GET", f"/api/projects/{pid}")
    data = p["data"]
    data["clips"] = [{"id": "c0", "media_id": ids["img.jpg"], "in": 0, "out": 2},
                     {"id": "c1", "media_id": ids["clip.mp4"], "in": 0, "out": 8}]
    data["overlays"] = [{"id": "o1", "type": "latex", "asset_id": asset, "start": 3, "end": 6,
                         "x": 0.6, "y": 0.1, "w": 0.3, "params": {}}]
    data["music"] = [{"id": "m1", "media_id": ids["music.mp3"], "start": 0, "in": 0, "out": 20, "volume": 0.3, "duck": True}]
    data["chapters"] = [{"id": "k1", "t": 0, "title": "Portada"}, {"id": "k2", "t": 2, "title": "Alcanos"}]
    data["quizzes"] = [{"id": "q1", "t": 7, "question": "¿Fórmula general de los alcanos?",
                        "options": ["CnH2n", "CnH2n+2", "CnH2n-2"], "correct": 1, "explanation": "Saturados."}]
    v = call("PUT", f"/api/projects/{pid}", {"data": data, "version": p["version"]})["version"]
    call("PUT", f"/api/projects/{pid}", {"data": data, "version": p["version"]}, expect=409)
    cap = call("POST", f"/api/projects/{pid}/captions", {"data": data})
    assert cap["cues"] and cap["words"][0]["s"] >= 2.0  # desplazado por la portada de 2 s

    # Colaboración
    call("POST", f"/api/projects/{pid}/members", {"username": "colega", "role": "revisor"})
    call("POST", f"/api/projects/{pid}/comments", {"t": 3.5, "text": "Revisar la ecuación"})
    assert len(call("GET", f"/api/projects/{pid}/comments")) == 1

    job = call("POST", f"/api/projects/{pid}/render", {"format": "16:9", "title": "Alcanos — prueba", "burn_captions": False})
    jobs = wait_jobs(pid)
    res = next(j for j in jobs if j["id"] == job["job_id"])["result"]
    exports = call("GET", f"/api/projects/{pid}/exports")
    e = exports[0]
    assert set(["mp4", "srt", "vtt", "vtt_en", "chapters", "poster", "txt"]) <= set(e["files"]), e["files"]
    assert abs(res["duration"] - 10) < 0.2
    srt = call("GET", f"/api/exports/{e['id']}/file/srt").decode()
    assert "00:00:03,200" in srt and "Hola estudiantes" in srt, srt
    # Público
    call("GET", f"/api/public/{e['token']}", expect=404)
    call("PATCH", f"/api/exports/{e['id']}", {"public": True})
    pub = call("GET", f"/api/public/{e['token']}")
    assert pub["quizzes"][0]["question"].startswith("¿Fórmula")
    view = call("POST", f"/api/public/{e['token']}/view", {"t": 1})["view_id"]
    call("POST", f"/api/public/{e['token']}/view", {"view_id": view, "t": 9.5})
    assert call("POST", f"/api/public/{e['token']}/answer", {"view_id": view, "quiz_idx": 0, "choice": 1})["correct"]
    an = call("GET", f"/api/exports/{e['id']}/analytics")
    assert an["views"] == 1 and an["completed"] == 1 and an["quizzes"][0]["right"] == 1, an
    # SCORM
    z = call("GET", f"/api/exports/{e['id']}/scorm")
    zp = Path(os.environ["DATA_DIR"]) / "scorm_test.zip"
    zp.write_bytes(z)
    with zipfile.ZipFile(zp) as zz:
        names = set(zz.namelist())
        assert {"imsmanifest.xml", "index.html", "player.js", "video.mp4", "subtitulos_es.vtt"} <= names, names
        man = zz.read("imsmanifest.xml").decode()
        assert "adlcp:scormtype=\"sco\"" in man and "<schemaversion>1.2</schemaversion>" in man
    # Revisor: puede comentar, no editar
    jar.clear()
    call("POST", "/api/login", {"username": "colega", "password": "Revisor123"})
    call("POST", f"/api/projects/{pid}/comments", {"t": 1, "text": "Bien"})
    call("PUT", f"/api/projects/{pid}", {"data": data, "version": v}, expect=403)
    call("POST", f"/api/projects/{pid}/render", {}, expect=403)
    print("API OK ·", len(names), "archivos en SCORM ·", e["files"])


if __name__ == "__main__":
    main(sys.argv[1])
