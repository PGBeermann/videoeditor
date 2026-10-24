"""Proceso de trabajos pesados: ingesta (análisis + proxy), transcripción y render.

Se ejecuta como servicio separado en docker-compose (`python -m app.worker`) y
comparte con la aplicación web el volumen /data y la base SQLite.
"""
from __future__ import annotations

import os
import secrets
import signal
import threading
import time
import traceback

from . import config, db, media, transcribe
from .render import Renderer, cleanup

_stop = False


def _sig(*_):
    global _stop
    _stop = True


class Progress:
    def __init__(self, job_id: str, job_type: str = ""):
        self.job_id = job_id
        self.lane = "worker:rapido" if job_type == "ingest" else "worker:pesado"
        self.last = 0.0
        self.beat = 0.0

    def __call__(self, frac: float, msg: str = "") -> None:
        t = time.time()
        if t - self.last < 1.0 and frac < 1.0:
            return
        self.last = t
        if t - self.beat > 10:
            db.heartbeat(self.lane)
            self.beat = t
        db.ex("UPDATE jobs SET progress=?, message=? WHERE id=?",
              round(min(max(frac, 0), 1), 4), msg, self.job_id)


def claim(types: tuple[str, ...]):
    marks = ",".join("?" * len(types))
    return db.q1(
        "UPDATE jobs SET status='running', started_at=?, message='Iniciando' "
        f"WHERE id=(SELECT id FROM jobs WHERE status='queued' AND type IN ({marks}) "
        "ORDER BY created_at LIMIT 1) RETURNING *", db.now(), *types)


def do_ingest(job, prog: Progress) -> dict:
    m = db.q1("SELECT * FROM media WHERE id=?", job["media_id"])
    if not m:
        raise RuntimeError("El medio ya no existe")
    prog(0.02, "Analizando archivo")
    info = media.probe(m["path"])
    db.ex("UPDATE media SET kind=?, duration=?, width=?, height=?, fps=?, has_audio=? WHERE id=?",
          info["kind"], info["duration"], info["width"], info["height"], info["fps"],
          int(info["has_audio"]), m["id"])
    proxy = media.make_proxy(m["id"], m["path"], info,
                             lambda f: prog(0.05 + 0.85 * f, "Creando versión de vista previa"))
    wave = None
    if info["has_audio"]:
        prog(0.92, "Dibujando forma de onda")
        wave = media.make_waveform(m["id"], m["path"])
    db.ex("UPDATE media SET proxy_path=?, waveform_path=?, status='ready' WHERE id=?",
          proxy, wave, m["id"])
    params = db.jload(job["params"], {})
    if params.get("auto_transcribe") and info["has_audio"] and info["kind"] in ("video", "audio"):
        db.enqueue("transcribe", job["project_id"], m["id"],
                   {"language": params.get("language", "es")})
    return {"media": m["id"], **info}


def do_transcribe(job, prog: Progress) -> dict:
    m = db.q1("SELECT * FROM media WHERE id=?", job["media_id"])
    if not m:
        raise RuntimeError("El medio ya no existe")
    params = db.jload(job["params"], {})
    prog(0.01, f"Cargando modelo Whisper «{config.WHISPER_MODEL}» (la primera vez se descarga)")
    res = transcribe.transcribe(m["path"], m["duration"], params.get("language", "es"),
                                bool(params.get("translate")), prog)
    db.ex("INSERT INTO transcripts(media_id, language, words, translated, created_at) "
          "VALUES (?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET language=excluded.language, "
          "words=excluded.words, translated=COALESCE(excluded.translated, transcripts.translated), "
          "created_at=excluded.created_at",
          m["id"], res["language"], db.jdump(res["words"]),
          db.jdump(res["translated"]) if res["translated"] is not None else None, db.now())
    return {"words": len(res["words"]), "language": res["language"]}


def do_render(job, prog: Progress) -> dict:
    params = db.jload(job["params"], {})
    p = db.q1("SELECT * FROM projects WHERE id=?", job["project_id"])
    if not p:
        raise RuntimeError("El proyecto ya no existe")
    project = db.jload(p["data"], {})
    rows = db.q("SELECT * FROM media WHERE project_id=? AND status='ready'", p["id"])
    media_map = {r["id"]: dict(r) for r in rows}
    for r in media_map.values():
        r["has_audio"] = bool(r["has_audio"])
    assets = {r["id"]: r["path"] for r in
              db.q("SELECT id, path FROM assets WHERE project_id=?", p["id"])}
    transcripts, translations = {}, {}
    for r in db.q("SELECT t.* FROM transcripts t JOIN media m ON m.id=t.media_id "
                  "WHERE m.project_id=?", p["id"]):
        transcripts[r["media_id"]] = db.jload(r["words"], [])
        if params.get("include_english", True) and r["translated"]:
            translations[r["media_id"]] = db.jload(r["translated"], [])
    export_id = db.new_id()
    workdir = config.TMP_DIR / f"render_{job['id']}"
    workdir.mkdir(parents=True, exist_ok=True)
    title = params.get("title") or p["name"]
    try:
        r = Renderer(project, media_map, assets, transcripts, translations, workdir, params, prog)
        out_dir = config.EXPORT_DIR / export_id
        res = r.run(out_dir, title)
    finally:
        cleanup(workdir)
    meta = {"duration": res["duration"], "width": res["width"], "height": res["height"],
            "chapters": sorted(project.get("chapters", []), key=lambda c: c["t"]),
            "quizzes": sorted(project.get("quizzes", []), key=lambda q: q["t"]),
            "project_name": p["name"]}
    db.ex("INSERT INTO exports(id, project_id, job_id, title, format, token, files, meta, created_at) "
          "VALUES (?,?,?,?,?,?,?,?,?)", export_id, p["id"], job["id"], title,
          params.get("format", "16:9"), secrets.token_urlsafe(16), db.jdump(res["files"]),
          db.jdump(meta), db.now())
    return {"export_id": export_id, **{k: res[k] for k in ("duration", "width", "height")}}


HANDLERS = {"ingest": do_ingest, "transcribe": do_transcribe, "render": do_render}

# Dos carriles independientes: la ingesta (rápida) nunca espera detrás de una
# transcripción o un render largos.
LANES = {"rapido": ("ingest",), "pesado": ("transcribe", "render")}


def run_job(job) -> None:
    prog = Progress(job["id"], job["type"])
    print(f"[worker] {job['type']} {job['id']}", flush=True)
    try:
        result = HANDLERS[job["type"]](job, prog)
        db.ex("UPDATE jobs SET status='done', progress=1, message='Completado', result=?, "
              "finished_at=? WHERE id=?", db.jdump(result), db.now(), job["id"])
    except Exception as e:  # noqa: BLE001 - se informa al usuario
        traceback.print_exc()
        msg = str(e).strip().splitlines()[-1][:500] if str(e).strip() else type(e).__name__
        db.ex("UPDATE jobs SET status='error', message=?, finished_at=? WHERE id=?",
              msg, db.now(), job["id"])
        if job["type"] == "ingest" and job["media_id"]:
            db.ex("UPDATE media SET status='error' WHERE id=?", job["media_id"])


def lane_loop(name: str, types: tuple[str, ...]) -> None:
    last_beat = 0.0
    while not _stop:
        if time.time() - last_beat > 10:
            db.heartbeat(f"worker:{name}")
            last_beat = time.time()
        job = claim(types)
        if not job:
            time.sleep(config.WORKER_POLL_SECONDS)
            continue
        run_job(job)
        db.heartbeat(f"worker:{name}")
        last_beat = time.time()


def main() -> None:
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    db.init_db()
    # Trabajos que quedaron «en curso» por un reinicio se marcan como interrumpidos
    db.ex("UPDATE jobs SET status='error', message='Interrumpido por reinicio del servidor', "
          "finished_at=? WHERE status='running'", db.now())
    db.ex("UPDATE media SET status='error' WHERE status='processing' AND id NOT IN "
          "(SELECT media_id FROM jobs WHERE type='ingest' AND status='queued' AND media_id IS NOT NULL)")
    print(f"[worker] listo (pid {os.getpid()}), carriles: {', '.join(LANES)}; "
          f"modelo Whisper: {config.WHISPER_MODEL}", flush=True)
    threads = [threading.Thread(target=lane_loop, args=(n, t), name=n, daemon=True)
               for n, t in LANES.items()]
    for th in threads:
        th.start()
    while not _stop and any(th.is_alive() for th in threads):
        time.sleep(1)
    print("[worker] detenido", flush=True)


if __name__ == "__main__":
    main()
