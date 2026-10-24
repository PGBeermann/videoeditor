"""Autenticación: contraseñas con scrypt (stdlib), sesiones en BD, control de acceso."""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request

from . import config, db

SESSION_COOKIE = "ev_session"
SESSION_DAYS = 14
_failed: dict[str, list[float]] = defaultdict(list)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex),
                            n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


def bootstrap_admin() -> None:
    """Crea el administrador inicial a partir de ADMIN_USER / ADMIN_PASSWORD."""
    if db.q1("SELECT 1 FROM users WHERE role='admin' LIMIT 1"):
        return
    if not config.ADMIN_PASSWORD:
        print("[estudio] AVISO: defina ADMIN_PASSWORD para crear el administrador.")
        return
    db.ex("INSERT INTO users(username, name, pw_hash, role, created_at) "
          "VALUES (?,?,?,?,?)", config.ADMIN_USER, "Administrador",
          hash_password(config.ADMIN_PASSWORD), "admin", db.now())
    print(f"[estudio] Administrador '{config.ADMIN_USER}' creado.")


def throttled(key: str) -> bool:
    """Máximo 8 intentos fallidos por IP y usuario cada 10 minutos."""
    window = time.time() - 600
    _failed[key] = [t for t in _failed[key] if t > window]
    return len(_failed[key]) >= 8


def register_failure(key: str) -> None:
    _failed[key].append(time.time())


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    t = db.now()
    db.ex("INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
          token, user_id, t, t + SESSION_DAYS * 86400)
    db.ex("DELETE FROM sessions WHERE expires_at < ?", t)
    return token


def destroy_session(token: str) -> None:
    db.ex("DELETE FROM sessions WHERE token=?", token)


def current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "No autenticado")
    row = db.q1("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=? AND s.expires_at>? AND u.active=1", token, db.now())
    if not row:
        raise HTTPException(401, "Sesión expirada")
    # Protección CSRF: toda petición que modifica datos debe traer la cabecera
    # X-Requested-With (un formulario de otro sitio no puede añadirla).
    if request.method not in ("GET", "HEAD", "OPTIONS") and \
            request.headers.get("x-requested-with") != "estudio":
        raise HTTPException(403, "Falta cabecera de seguridad")
    return {k: row[k] for k in row.keys() if k != "pw_hash"}


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Solo administradores")
    return user


def project_role(project_id: str, user: dict) -> str | None:
    """Devuelve 'owner', 'editor', 'revisor' o None."""
    p = db.q1("SELECT owner_id FROM projects WHERE id=?", project_id)
    if not p:
        return None
    if p["owner_id"] == user["id"]:
        return "owner"
    m = db.q1("SELECT role FROM project_members WHERE project_id=? AND user_id=?",
              project_id, user["id"])
    if m:
        return m["role"]
    if user["role"] == "admin":
        return "editor"
    return None


def check_project(project_id: str, user: dict, write: bool = False) -> str:
    role = project_role(project_id, user)
    if role is None:
        raise HTTPException(404, "Proyecto no encontrado")
    if write and role == "revisor":
        raise HTTPException(403, "Los revisores solo pueden comentar")
    return role
