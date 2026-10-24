"""Aplicación web (FastAPI): API REST + páginas estáticas del editor."""
from __future__ import annotations

import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, captions, config, db, scorm
from .auth import check_project, current_user, require_admin

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    auth.bootstrap_admin()
    yield


app = FastAPI(title="Estudio de Video Educativo", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/v/"):
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


def _page(name: str) -> HTMLResponse:
    return HTMLResponse((STATIC / name).read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def page_index():
    return _page("index.html")


@app.get("/editor/{pid}", include_in_schema=False)
def page_editor(pid: str):
    return _page("editor.html")


@app.get("/grabar/{pid}", include_in_schema=False)
def page_record(pid: str):
    return _page("record.html")


@app.get("/v/{token}", include_in_schema=False)
def page_view(token: str):
    return _page("view.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    db.q1("SELECT 1")
    return {"ok": True}


# ======================================================================= Sesión
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    key = f"{request.client.host if request.client else '?'}:{body.username.lower()}"
    if auth.throttled(key):
        raise HTTPException(429, "Demasiados intentos. Espere 10 minutos.")
    u = db.q1("SELECT * FROM users WHERE username=? AND active=1", body.username.strip())
    if not u or not auth.verify_password(body.password, u["pw_hash"]):
        auth.register_failure(key)
        raise HTTPException(401, "Usuario o contraseña incorrectos")
    token = auth.create_session(u["id"])
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/")
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get(auth.SESSION_COOKIE)
    if tok:
        auth.destroy_session(tok)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=8)


@app.post("/api/me/password")
def change_password(body: PasswordIn, user=Depends(current_user)):
    row = db.q1("SELECT pw_hash FROM users WHERE id=?", user["id"])
    if not auth.verify_password(body.current, row["pw_hash"]):
        raise HTTPException(400, "La contraseña actual no es correcta")
    db.ex("UPDATE users SET pw_hash=? WHERE id=?", auth.hash_password(body.new), user["id"])
    return {"ok": True}


# ======================================================================= Usuarios (admin)
class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9._@-]+$")
    name: str = ""
    password: str = Field(min_length=8)
    role: str = Field(default="docente", pattern="^(admin|docente)$")


class UserPatch(BaseModel):
    name: str | None = None
    password: str | None = Field(default=None, min_length=8)
    role: str | None = Field(default=None, pattern="^(admin|docente)$")
    active: bool | None = None


@app.get("/api/users")
def list_users(user=Depends(require_admin)):
    return [dict(r) for r in db.q("SELECT id, username, name, role, active, created_at "
                                  "FROM users ORDER BY username")]


@app.post("/api/users")
def create_user(body: UserIn, user=Depends(require_admin)):
    if db.q1("SELECT 1 FROM users WHERE username=?", body.username):
        raise HTTPException(409, "Ese usuario ya existe")
    db.ex("INSERT INTO users(username, name, pw_hash, role, created_at) VALUES (?,?,?,?,?)",
          body.username, body.name, auth.hash_password(body.password), body.role, db.now())
    return {"ok": True}


@app.patch("/api/users/{uid}")
def patch_user(uid: int, body: UserPatch, user=Depends(require_admin)):
    if body.name is not None:
        db.ex("UPDATE users SET name=? WHERE id=?", body.name, uid)
    if body.password:
        db.ex("UPDATE users SET pw_hash=? WHERE id=?", auth.hash_password(body.password), uid)
        db.ex("DELETE FROM sessions WHERE user_id=?", uid)
    if body.role and uid != user["id"]:
        db.ex("UPDATE users SET role=? WHERE id=?", body.role, uid)
    if body.active is not None and uid != user["id"]:
        db.ex("UPDATE users SET active=? WHERE id=?", int(body.active), uid)
        if not body.active:
            db.ex("DELETE FROM sessions WHERE user_id=?", uid)
    return {"ok": True}


# ======================================================================= Proyectos
def default_project(fmt: str) -> dict:
    w, h = {"9:16": (1080, 1920), "1:1": (1080, 1080)}.get(fmt, (1920, 1080))
    return {
        "settings": {"width": w, "height": h, "fps": 30, "format": fmt},
        "clips": [], "overlays": [], "zooms": [], "music": [],
        "chapters": [], "quizzes": [],
        "audio": {"denoise": "light", "highpass": True, "deess": True, "compress": True,
                  "loudness": -16, "voice_gain_db": 0},
        "captions": {"burn": False, "soft": True, "lang": "es", "color": "#FFFFFF"},
        "brand": {"logo_asset_id": None, "position": "tr", "opacity": 0.85, "size": 0.12},
        "teleprompter": "",
    }


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    format: str = Field(default="16:9", pattern=r"^(16:9|9:16|1:1)$")


class ProjectSave(BaseModel):
    data: dict
    version: int
    name: str | None = Field(default=None, max_length=120)


@app.get("/api/projects")
def list_projects(user=Depends(current_user)):
    rows = db.q("""SELECT p.id, p.name, p.updated_at, p.created_at, u.name AS owner_name,
                   u.username AS owner, CASE WHEN p.owner_id=? THEN 'owner' ELSE pm.role END AS role,
                   (SELECT proxy_path FROM media WHERE project_id=p.id AND kind='video'
                    AND status='ready' ORDER BY created_at LIMIT 1) AS has_video
                   FROM projects p JOIN users u ON u.id=p.owner_id
                   LEFT JOIN project_members pm ON pm.project_id=p.id AND pm.user_id=?
                   WHERE p.owner_id=? OR pm.user_id IS NOT NULL ORDER BY p.updated_at DESC""",
                user["id"], user["id"], user["id"])
    return [{**dict(r), "has_video": bool(r["has_video"])} for r in rows]


@app.post("/api/projects")
def create_project(body: ProjectIn, user=Depends(current_user)):
    pid, t = db.new_id(), db.now()
    db.ex("INSERT INTO projects(id, owner_id, name, data, created_at, updated_at) "
          "VALUES (?,?,?,?,?,?)", pid, user["id"], body.name.strip(),
          db.jdump(default_project(body.format)), t, t)
    return {"id": pid}


def _media_out(r) -> dict:
    d = {k: r[k] for k in ("id", "filename", "kind", "duration", "width", "height", "fps",
                           "status", "created_at")}
    d["has_audio"] = bool(r["has_audio"])
    d["has_waveform"] = bool(r["waveform_path"])
    d["audio_silent"] = bool(r["audio_silent"]) if "audio_silent" in r.keys() else False
    d["audio_tracks"] = len(db.audio_map_of(r))
    return d


@app.get("/api/projects/{pid}")
def get_project(pid: str, user=Depends(current_user)):
    role = check_project(pid, user)
    p = db.q1("SELECT * FROM projects WHERE id=?", pid)
    media = [_media_out(r) for r in db.q("SELECT * FROM media WHERE project_id=? ORDER BY created_at", pid)]
    tr = {r["media_id"]: {"language": r["language"], "translated": r["translated"] is not None}
          for r in db.q("SELECT t.media_id, t.language, t.translated FROM transcripts t "
                        "JOIN media m ON m.id=t.media_id WHERE m.project_id=?", pid)}
    data = db.jload(p["data"], {})
    base = default_project(data.get("settings", {}).get("format", "16:9"))
    for k, v in base.items():       # compatibilidad con proyectos antiguos
        data.setdefault(k, v)
    return {"id": p["id"], "name": p["name"], "version": p["version"], "role": role,
            "data": data, "media": media, "transcripts": tr, "updated_at": p["updated_at"]}


@app.put("/api/projects/{pid}")
def save_project(pid: str, body: ProjectSave, user=Depends(current_user)):
    check_project(pid, user, write=True)
    with db.tx() as c:
        cur = c.execute("SELECT version FROM projects WHERE id=?", (pid,)).fetchone()
        if cur["version"] != body.version:
            raise HTTPException(409, "El proyecto fue modificado por otra persona. Recargue.")
        if len(db.jdump(body.data)) > 5_000_000:
            raise HTTPException(413, "Proyecto demasiado grande")
        c.execute("UPDATE projects SET data=?, version=version+1, updated_at=?, "
                  "name=COALESCE(?, name) WHERE id=?",
                  (db.jdump(body.data), db.now(), body.name, pid))
    return {"version": body.version + 1}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str, user=Depends(current_user)):
    if auth.project_role(pid, user) != "owner" and user["role"] != "admin":
        raise HTTPException(403, "Solo el propietario puede eliminar el proyecto")
    _purge_project_files(pid)
    db.ex("DELETE FROM projects WHERE id=?", pid)
    return {"ok": True}


def _purge_project_files(pid: str) -> None:
    for r in db.q("SELECT path, proxy_path, waveform_path FROM media WHERE project_id=?", pid):
        for p in (r["path"], r["proxy_path"], r["waveform_path"]):
            if p:
                Path(p).unlink(missing_ok=True)
    for r in db.q("SELECT path FROM assets WHERE project_id=?", pid):
        Path(r["path"]).unlink(missing_ok=True)
    for r in db.q("SELECT id FROM exports WHERE project_id=?", pid):
        shutil.rmtree(config.EXPORT_DIR / r["id"], ignore_errors=True)


class MemberIn(BaseModel):
    username: str
    role: str = Field(default="editor", pattern="^(editor|revisor)$")


@app.get("/api/projects/{pid}/members")
def list_members(pid: str, user=Depends(current_user)):
    check_project(pid, user)
    return [dict(r) for r in db.q("SELECT u.id, u.username, u.name, pm.role FROM project_members pm "
                                  "JOIN users u ON u.id=pm.user_id WHERE pm.project_id=?", pid)]


@app.post("/api/projects/{pid}/members")
def add_member(pid: str, body: MemberIn, user=Depends(current_user)):
    if auth.project_role(pid, user) != "owner":
        raise HTTPException(403, "Solo el propietario comparte el proyecto")
    u = db.q1("SELECT id FROM users WHERE username=? AND active=1", body.username.strip())
    if not u:
        raise HTTPException(404, "No existe ese usuario")
    db.ex("INSERT INTO project_members(project_id, user_id, role) VALUES (?,?,?) "
          "ON CONFLICT(project_id, user_id) DO UPDATE SET role=excluded.role", pid, u["id"], body.role)
    return {"ok": True}


@app.delete("/api/projects/{pid}/members/{uid}")
def remove_member(pid: str, uid: int, user=Depends(current_user)):
    if auth.project_role(pid, user) != "owner":
        raise HTTPException(403, "Solo el propietario")
    db.ex("DELETE FROM project_members WHERE project_id=? AND user_id=?", pid, uid)
    return {"ok": True}


# ======================================================================= Medios
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,5}$")


async def _stream_to(request: Request, dst: Path, limit_mb: int) -> int:
    size, limit = 0, limit_mb * 1024 * 1024
    try:
        with open(dst, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"El archivo supera el límite de {limit_mb} MB")
                f.write(chunk)
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    if size == 0:
        dst.unlink(missing_ok=True)
        raise HTTPException(400, "Archivo vacío")
    return size


def _register_media(pid: str, src: Path, filename: str, auto_transcribe: bool, language: str) -> dict:
    ext = Path(filename).suffix.lower()
    if not _EXT_RE.match(ext):
        ext = ".bin"
    mid = db.new_id()
    dst = config.MEDIA_DIR / f"{mid}{ext}"
    src.replace(dst)
    safe_name = re.sub(r"[\x00-\x1f/\\]", "_", filename)[:200]
    db.ex("INSERT INTO media(id, project_id, filename, path, created_at) VALUES (?,?,?,?,?)",
          mid, pid, safe_name, str(dst), db.now())
    jid = db.enqueue("ingest", pid, mid, {"auto_transcribe": bool(auto_transcribe),
                                          "language": language})
    return {"id": mid, "job_id": jid}


@app.put("/api/projects/{pid}/media")
async def upload_media(pid: str, request: Request, filename: str, auto_transcribe: int = 0,
                       language: str = "es", user=Depends(current_user)):
    """Subida directa en una sola petición (archivos pequeños: portadas, imágenes)."""
    check_project(pid, user, write=True)
    tmp = config.TMP_DIR / f"up_{db.new_id()}"
    await _stream_to(request, tmp, config.MAX_UPLOAD_MB)
    return _register_media(pid, tmp, filename, bool(auto_transcribe), language)


# Subida por fragmentos: cada petición es corta, así que no la cortan los tiempos máximos
# de lectura de proxies inversos (Traefik/Nginx) y se puede reanudar tras un corte de red.
UPLOAD_DIR = config.TMP_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CHUNK_MAX = 64 * 1024 * 1024


class UploadStart(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)


def _upload_meta(uid: str, user: dict) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", uid):
        raise HTTPException(404)
    mp = UPLOAD_DIR / f"{uid}.json"
    if not mp.exists():
        raise HTTPException(404, "Subida no encontrada o expirada")
    meta = db.jload(mp.read_text(), {})
    if meta.get("user_id") != user["id"]:
        raise HTTPException(403)
    return meta


@app.post("/api/projects/{pid}/uploads")
def upload_start(pid: str, body: UploadStart, user=Depends(current_user)):
    check_project(pid, user, write=True)
    if body.size > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"El archivo supera el límite de {config.MAX_UPLOAD_MB} MB")
    free = shutil.disk_usage(config.DATA_DIR).free
    if body.size * 1.6 > free:
        raise HTTPException(507, "No hay espacio suficiente en el servidor")
    uid = db.new_id()
    (UPLOAD_DIR / f"{uid}.part").touch()
    (UPLOAD_DIR / f"{uid}.json").write_text(db.jdump({"pid": pid, "user_id": user["id"],
                                                      "filename": body.filename, "size": body.size,
                                                      "created": db.now()}))
    # limpieza de subidas abandonadas (> 24 h)
    for f in UPLOAD_DIR.glob("*.json"):
        if db.now() - f.stat().st_mtime > 86400:
            f.unlink(missing_ok=True)
            f.with_suffix(".part").unlink(missing_ok=True)
    return {"upload_id": uid, "chunk": 16 * 1024 * 1024}


@app.get("/api/uploads/{uid}")
def upload_status(uid: str, user=Depends(current_user)):
    meta = _upload_meta(uid, user)
    return {"received": (UPLOAD_DIR / f"{uid}.part").stat().st_size, "size": meta["size"]}


@app.put("/api/uploads/{uid}")
async def upload_chunk(uid: str, offset: int, request: Request, user=Depends(current_user)):
    meta = _upload_meta(uid, user)
    part = UPLOAD_DIR / f"{uid}.part"
    cur = part.stat().st_size
    if offset != cur:
        raise HTTPException(409, f"Desfase de subida: el servidor tiene {cur} bytes")
    written = 0
    with open(part, "ab") as f:
        async for chunk in request.stream():
            written += len(chunk)
            if written > CHUNK_MAX or cur + written > meta["size"]:
                f.truncate(cur)
                raise HTTPException(413, "Fragmento demasiado grande")
            f.write(chunk)
    return {"received": cur + written}


class UploadDone(BaseModel):
    auto_transcribe: bool = False
    language: str = "es"


@app.post("/api/uploads/{uid}/complete")
def upload_complete(uid: str, body: UploadDone, user=Depends(current_user)):
    meta = _upload_meta(uid, user)
    check_project(meta["pid"], user, write=True)
    part = UPLOAD_DIR / f"{uid}.part"
    if part.stat().st_size != meta["size"]:
        raise HTTPException(400, "La subida está incompleta")
    (UPLOAD_DIR / f"{uid}.json").unlink(missing_ok=True)
    return _register_media(meta["pid"], part, meta["filename"], body.auto_transcribe, body.language)


def _media_for(mid: str, user: dict, write: bool = False):
    m = db.q1("SELECT * FROM media WHERE id=?", mid)
    if not m:
        raise HTTPException(404, "Medio no encontrado")
    check_project(m["project_id"], user, write)
    return m


@app.get("/api/projects/{pid}/media")
def list_media(pid: str, user=Depends(current_user)):
    check_project(pid, user)
    return [_media_out(r) for r in db.q("SELECT * FROM media WHERE project_id=? ORDER BY created_at", pid)]


@app.delete("/api/media/{mid}")
def delete_media(mid: str, user=Depends(current_user)):
    m = _media_for(mid, user, write=True)
    for p in (m["path"], m["proxy_path"], m["waveform_path"]):
        if p:
            Path(p).unlink(missing_ok=True)
    db.ex("DELETE FROM media WHERE id=?", mid)
    return {"ok": True}


@app.get("/api/media/{mid}/proxy")
def media_proxy(mid: str, user=Depends(current_user)):
    m = _media_for(mid, user)
    if not m["proxy_path"] or not Path(m["proxy_path"]).exists():
        raise HTTPException(404, "Vista previa aún no disponible")
    ext = Path(m["proxy_path"]).suffix.lower()
    mime = {".mp4": "video/mp4", ".m4a": "audio/mp4", ".png": "image/png"}.get(ext)
    return FileResponse(m["proxy_path"], media_type=mime,
                        headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/media/{mid}/waveform")
def media_wave(mid: str, user=Depends(current_user)):
    m = _media_for(mid, user)
    if not m["waveform_path"]:
        raise HTTPException(404)
    return FileResponse(m["waveform_path"], headers={"Cache-Control": "private, max-age=86400"})


class TranscribeIn(BaseModel):
    language: str = "es"
    translate: bool = False


@app.post("/api/media/{mid}/transcribe")
def media_transcribe(mid: str, body: TranscribeIn, user=Depends(current_user)):
    m = _media_for(mid, user, write=True)
    if not m["has_audio"]:
        raise HTTPException(400, "Este medio no tiene audio")
    jid = db.enqueue("transcribe", m["project_id"], mid, body.model_dump())
    return {"job_id": jid}


@app.get("/api/media/{mid}/transcript")
def get_transcript(mid: str, user=Depends(current_user)):
    _media_for(mid, user)
    r = db.q1("SELECT * FROM transcripts WHERE media_id=?", mid)
    if not r:
        raise HTTPException(404, "Sin transcripción")
    return {"language": r["language"], "words": db.jload(r["words"], []),
            "translated": db.jload(r["translated"], None)}


class WordsIn(BaseModel):
    words: list[dict]


@app.put("/api/media/{mid}/transcript")
def put_transcript(mid: str, body: WordsIn, user=Depends(current_user)):
    _media_for(mid, user, write=True)
    clean = []
    for w in body.words:
        try:
            clean.append({"w": str(w["w"])[:80], "s": float(w["s"]), "e": float(w["e"]),
                          "p": float(w.get("p", 1))})
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Formato de palabras inválido")
    db.ex("UPDATE transcripts SET words=? WHERE media_id=?", db.jdump(clean), mid)
    return {"ok": True}


class CaptionsIn(BaseModel):
    data: dict


@app.post("/api/projects/{pid}/captions")
def preview_captions(pid: str, body: CaptionsIn, user=Depends(current_user)):
    check_project(pid, user)
    tr = {r["media_id"]: db.jload(r["words"], []) for r in
          db.q("SELECT t.media_id, t.words FROM transcripts t JOIN media m ON m.id=t.media_id "
               "WHERE m.project_id=?", pid)}
    words = captions.all_words(body.data, tr)
    return {"words": words, "cues": captions.build_cues(words)}


# ======================================================================= Capas (PNG)
@app.post("/api/projects/{pid}/assets")
async def upload_asset(pid: str, request: Request, user=Depends(current_user)):
    check_project(pid, user, write=True)
    aid = db.new_id()
    dst = config.ASSET_DIR / f"{aid}.png"
    await _stream_to(request, dst, 25)
    with open(dst, "rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            dst.unlink(missing_ok=True)
            raise HTTPException(400, "Solo se aceptan imágenes PNG")
    db.ex("INSERT INTO assets(id, project_id, path, created_at) VALUES (?,?,?,?)",
          aid, pid, str(dst), db.now())
    return {"id": aid}


@app.get("/api/assets/{aid}")
def get_asset(aid: str, user=Depends(current_user)):
    a = db.q1("SELECT * FROM assets WHERE id=?", aid)
    if not a:
        raise HTTPException(404)
    check_project(a["project_id"], user)
    return FileResponse(a["path"], media_type="image/png",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


# ======================================================================= Trabajos y render
def _job_out(r) -> dict:
    d = dict(r)
    d["params"] = db.jload(d["params"], {})
    d["result"] = db.jload(d["result"], None)
    return d


@app.get("/api/projects/{pid}/jobs")
def list_jobs(pid: str, user=Depends(current_user)):
    check_project(pid, user)
    rows = db.q("SELECT j.*, m.filename FROM jobs j LEFT JOIN media m ON m.id=j.media_id "
                "WHERE j.project_id=? ORDER BY j.created_at DESC LIMIT 40", pid)
    jobs = [_job_out(r) for r in rows]
    # Posición en la cola de cada trabajo pendiente (la cola es común a todos los proyectos)
    lane = {"ingest": ("ingest",), "transcribe": ("transcribe", "render"), "render": ("transcribe", "render")}
    for j in jobs:
        if j["status"] == "queued":
            types = lane.get(j["type"], (j["type"],))
            marks = ",".join("?" * len(types))
            j["position"] = db.q1(f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running') "
                                  f"AND type IN ({marks}) AND created_at < ?", *types, j["created_at"])["n"] + 1
    busy = [{"type": r["type"], "progress": r["progress"], "mine": r["project_id"] == pid}
            for r in db.q("SELECT type, progress, project_id FROM jobs WHERE status='running'")]
    beats = {r["name"]: r["at"] for r in db.q("SELECT name, at FROM heartbeats")}
    t = db.now()
    running_types = {r["type"] for r in db.q("SELECT type FROM jobs WHERE status='running'")}
    worker = {"rapido": t - beats.get("worker:rapido", 0) < 60 or "ingest" in running_types,
              "pesado": t - beats.get("worker:pesado", 0) < 60
              or bool(running_types & {"transcribe", "render"})}
    return {"jobs": jobs, "running": busy, "worker": worker}


@app.delete("/api/jobs/{jid}")
def cancel_job(jid: str, user=Depends(current_user)):
    j = db.q1("SELECT * FROM jobs WHERE id=?", jid)
    if not j:
        raise HTTPException(404)
    check_project(j["project_id"], user, write=True)
    db.ex("UPDATE jobs SET status='error', message='Cancelado por el usuario', finished_at=? "
          "WHERE id=? AND status='queued'", db.now(), jid)
    return {"ok": True}


class RenderIn(BaseModel):
    format: str = Field(default="16:9", pattern=r"^(16:9|9:16|1:1|proyecto)$")
    reframe: str = Field(default="blur", pattern="^(blur|crop)$")
    crop_x: float = 0.5
    burn_captions: bool | None = None
    soft_captions: bool | None = None
    include_english: bool = True
    height: int | None = Field(default=None, ge=360, le=2160)
    crf: int = Field(default=config.RENDER_CRF, ge=14, le=30)
    title: str | None = Field(default=None, max_length=150)


@app.post("/api/projects/{pid}/render")
def start_render(pid: str, body: RenderIn, user=Depends(current_user)):
    check_project(pid, user, write=True)
    p = db.q1("SELECT data FROM projects WHERE id=?", pid)
    if not db.jload(p["data"], {}).get("clips"):
        raise HTTPException(400, "Agregue clips a la línea de tiempo antes de exportar")
    params = {k: v for k, v in body.model_dump().items() if v is not None}
    jid = db.enqueue("render", pid, None, params)
    return {"job_id": jid}


# ======================================================================= Exportaciones
FILE_TYPES = {"mp4": ("video/mp4", "video.mp4"), "srt": ("application/x-subrip", "subtitulos_es.srt"),
              "vtt": ("text/vtt", "subtitulos_es.vtt"), "vtt_en": ("text/vtt", "subtitles_en.vtt"),
              "srt_en": ("application/x-subrip", "subtitles_en.srt"),
              "chapters": ("text/vtt", "capitulos.vtt"), "txt": ("text/plain", "transcripcion.txt"),
              "poster": ("image/jpeg", "poster.jpg")}


def _export_out(r) -> dict:
    d = dict(r)
    d["files"] = sorted(db.jload(d["files"], {}).keys())
    d["meta"] = db.jload(d["meta"], {})
    d["public"] = bool(d["public"])
    d["views"] = db.q1("SELECT COUNT(*) AS n FROM views WHERE export_id=?", r["id"])["n"]
    return d


@app.get("/api/projects/{pid}/exports")
def list_exports(pid: str, user=Depends(current_user)):
    check_project(pid, user)
    return [_export_out(r) for r in
            db.q("SELECT * FROM exports WHERE project_id=? ORDER BY created_at DESC", pid)]


def _export_for(eid: str, user: dict, write: bool = False):
    e = db.q1("SELECT * FROM exports WHERE id=?", eid)
    if not e:
        raise HTTPException(404)
    check_project(e["project_id"], user, write)
    return e


def _send_export_file(e, kind: str, download: bool):
    files = db.jload(e["files"], {})
    if kind not in files or kind not in FILE_TYPES or not Path(files[kind]).exists():
        raise HTTPException(404, "Archivo no disponible")
    mime, fname = FILE_TYPES[kind]
    if kind == "mp4":
        safe = re.sub(r"[^\w\- ]+", "", e["title"]).strip().replace(" ", "_")[:80] or "video"
        fname = f"{safe}.mp4"
    return FileResponse(files[kind], media_type=mime,
                        filename=fname if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/exports/{eid}/file/{kind}")
def export_file(eid: str, kind: str, download: int = 0, user=Depends(current_user)):
    return _send_export_file(_export_for(eid, user), kind, bool(download))


class ExportPatch(BaseModel):
    public: bool | None = None
    title: str | None = Field(default=None, max_length=150)


@app.patch("/api/exports/{eid}")
def patch_export(eid: str, body: ExportPatch, user=Depends(current_user)):
    _export_for(eid, user, write=True)
    if body.public is not None:
        db.ex("UPDATE exports SET public=? WHERE id=?", int(body.public), eid)
    if body.title:
        db.ex("UPDATE exports SET title=? WHERE id=?", body.title, eid)
    return {"ok": True}


@app.delete("/api/exports/{eid}")
def delete_export(eid: str, user=Depends(current_user)):
    _export_for(eid, user, write=True)
    shutil.rmtree(config.EXPORT_DIR / eid, ignore_errors=True)
    db.ex("DELETE FROM exports WHERE id=?", eid)
    return {"ok": True}


@app.get("/api/exports/{eid}/scorm")
def export_scorm(eid: str, user=Depends(current_user)):
    e = _export_for(eid, user)
    path = scorm.build_package(dict(e), STATIC)
    safe = re.sub(r"[^\w\-]+", "_", e["title"])[:60] or "leccion"
    return FileResponse(path, media_type="application/zip", filename=f"SCORM_{safe}.zip")


@app.get("/api/exports/{eid}/analytics")
def export_analytics(eid: str, user=Depends(current_user)):
    e = _export_for(eid, user)
    meta = db.jload(e["meta"], {})
    dur = float(meta.get("duration", 0)) or 1
    nb = int(dur // 5) + 1
    hist = [0] * nb
    views = db.q("SELECT buckets, max_t FROM views WHERE export_id=?", eid)
    for v in views:
        for b in set(db.jload(v["buckets"], [])):
            if 0 <= b < nb:
                hist[b] += 1
    quizzes = meta.get("quizzes", [])
    qstats = []
    for i, qz in enumerate(quizzes):
        rows = db.q("SELECT choice, correct FROM quiz_answers WHERE export_id=? AND quiz_idx=?", eid, i)
        counts = [0] * len(qz.get("options", []))
        for r in rows:
            if 0 <= r["choice"] < len(counts):
                counts[r["choice"]] += 1
        qstats.append({"question": qz.get("question", ""), "options": qz.get("options", []),
                       "correct": qz.get("correct", 0), "counts": counts,
                       "answers": len(rows), "right": sum(r["correct"] for r in rows)})
    completed = sum(1 for v in views if v["max_t"] >= dur * 0.9)
    return {"views": len(views), "completed": completed, "bucket_seconds": 5,
            "retention": hist, "duration": dur, "quizzes": qstats}


# ======================================================================= Comentarios
class CommentIn(BaseModel):
    t: float
    text: str = Field(min_length=1, max_length=2000)


@app.get("/api/projects/{pid}/comments")
def list_comments(pid: str, user=Depends(current_user)):
    check_project(pid, user)
    return [dict(r) for r in db.q("SELECT c.*, u.name AS author, u.username FROM comments c "
                                  "JOIN users u ON u.id=c.user_id WHERE c.project_id=? ORDER BY c.t",
                                  pid)]


@app.post("/api/projects/{pid}/comments")
def add_comment(pid: str, body: CommentIn, user=Depends(current_user)):
    check_project(pid, user)
    db.ex("INSERT INTO comments(project_id, user_id, t, text, created_at) VALUES (?,?,?,?,?)",
          pid, user["id"], body.t, body.text, db.now())
    return {"ok": True}


class CommentPatch(BaseModel):
    resolved: bool


@app.patch("/api/comments/{cid}")
def patch_comment(cid: int, body: CommentPatch, user=Depends(current_user)):
    c = db.q1("SELECT * FROM comments WHERE id=?", cid)
    if not c:
        raise HTTPException(404)
    check_project(c["project_id"], user)
    db.ex("UPDATE comments SET resolved=? WHERE id=?", int(body.resolved), cid)
    return {"ok": True}


@app.delete("/api/comments/{cid}")
def delete_comment(cid: int, user=Depends(current_user)):
    c = db.q1("SELECT * FROM comments WHERE id=?", cid)
    if not c:
        raise HTTPException(404)
    role = check_project(c["project_id"], user)
    if c["user_id"] != user["id"] and role != "owner":
        raise HTTPException(403)
    db.ex("DELETE FROM comments WHERE id=?", cid)
    return {"ok": True}


# ======================================================================= Reproductor público
def _public_export(token: str):
    e = db.q1("SELECT * FROM exports WHERE token=? AND public=1", token)
    if not e:
        raise HTTPException(404, "Video no disponible")
    return e


@app.get("/api/public/{token}")
def public_meta(token: str):
    e = _public_export(token)
    meta = db.jload(e["meta"], {})
    files = db.jload(e["files"], {})
    base = f"/api/public/{token}/file"
    tracks = []
    if "vtt" in files:
        tracks.append({"src": f"{base}/vtt", "label": "Español", "lang": "es"})
    if "vtt_en" in files:
        tracks.append({"src": f"{base}/vtt_en", "label": "English", "lang": "en"})
    quizzes = [{k: q.get(k) for k in ("t", "question", "options", "correct", "explanation")}
               for q in meta.get("quizzes", [])]
    return {"title": e["title"], "video": f"{base}/mp4",
            "poster": f"{base}/poster" if "poster" in files else None, "tracks": tracks,
            "chapters": meta.get("chapters", []), "quizzes": quizzes,
            "duration": meta.get("duration"), "transcript": f"{base}/txt" if "txt" in files else None}


@app.get("/api/public/{token}/file/{kind}")
def public_file(token: str, kind: str, download: int = 0):
    return _send_export_file(_public_export(token), kind, bool(download))


class ViewIn(BaseModel):
    view_id: str | None = None
    t: float = 0


@app.post("/api/public/{token}/view")
def public_view(token: str, body: ViewIn):
    e = _public_export(token)
    dur = float(db.jload(e["meta"], {}).get("duration", 0)) or 1e9
    t = max(0.0, min(body.t, dur))
    b = int(t // 5)
    if body.view_id and re.fullmatch(r"[0-9a-f]{32}", body.view_id):
        v = db.q1("SELECT * FROM views WHERE id=? AND export_id=?", body.view_id, e["id"])
        if v:
            buckets = db.jload(v["buckets"], [])
            if b not in buckets and len(buckets) < 5000:
                buckets.append(b)
            db.ex("UPDATE views SET buckets=?, max_t=MAX(max_t, ?), updated_at=? WHERE id=?",
                  db.jdump(buckets), t, db.now(), v["id"])
            return {"view_id": v["id"]}
    vid = db.new_id()
    db.ex("INSERT INTO views(id, export_id, buckets, max_t, created_at, updated_at) VALUES (?,?,?,?,?,?)",
          vid, e["id"], db.jdump([b]), t, db.now(), db.now())
    return {"view_id": vid}


class AnswerIn(BaseModel):
    view_id: str | None = None
    quiz_idx: int
    choice: int


@app.post("/api/public/{token}/answer")
def public_answer(token: str, body: AnswerIn):
    e = _public_export(token)
    quizzes = db.jload(e["meta"], {}).get("quizzes", [])
    if not 0 <= body.quiz_idx < len(quizzes):
        raise HTTPException(400)
    qz = quizzes[body.quiz_idx]
    correct = int(body.choice == int(qz.get("correct", 0)))
    if body.view_id and not db.q1("SELECT 1 FROM quiz_answers WHERE view_id=? AND quiz_idx=? AND export_id=?",
                                  body.view_id, body.quiz_idx, e["id"]):
        db.ex("INSERT INTO quiz_answers(export_id, view_id, quiz_idx, choice, correct, created_at) "
              "VALUES (?,?,?,?,?,?)", e["id"], body.view_id, body.quiz_idx, body.choice, correct, db.now())
    return {"correct": bool(correct)}


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
