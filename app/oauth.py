"""Servidor de autorización OAuth 2.1 para el conector MCP (Claude y otros clientes MCP).

Implementa lo que exige la especificación de autorización de MCP y el cliente de Claude:
  - RFC 9728  metadatos del recurso protegido   /.well-known/oauth-protected-resource[/mcp]
  - RFC 8414  metadatos del servidor             /.well-known/oauth-authorization-server
  - RFC 7591  registro dinámico de clientes      POST /oauth/register
  - RFC 6749 + RFC 7636 (PKCE S256)              GET/POST /oauth/authorize, POST /oauth/token
  - Refresh tokens rotativos (OAuth 2.1 para clientes públicos)

Los usuarios inician sesión con su cuenta del Estudio (las mismas credenciales del editor)
y aprueban el acceso en una pantalla de consentimiento. Los tokens se guardan como SHA-256.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import auth, config, db

router = APIRouter()

SCOPE = "videos"
SCOPES_SUPPORTED = [SCOPE, "offline_access"]
ACCESS_TTL = 3600                 # 1 h
REFRESH_TTL = 60 * 86400          # 60 días (se renueva con cada uso)
CODE_TTL = 300                    # 5 min
CLAUDE_CALLBACKS = {"https://claude.ai/api/mcp/auth_callback",
                    "https://claude.com/api/mcp/auth_callback"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY,
  secret_hash TEXT,
  name TEXT NOT NULL DEFAULT '',
  redirect_uris TEXT NOT NULL,
  auth_method TEXT NOT NULL DEFAULT 'none',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  redirect_uri TEXT NOT NULL,
  challenge TEXT NOT NULL,
  scope TEXT NOT NULL,
  resource TEXT,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                 -- access | refresh
  client_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  scope TEXT NOT NULL,
  resource TEXT,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_tokens_user ON oauth_tokens(user_id);
"""


def init() -> None:
    db.connect().executescript(SCHEMA)


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def base_url(request: Request) -> str:
    """URL pública (https detrás de Traefik gracias a --proxy-headers)."""
    if config.PUBLIC_URL:
        return config.PUBLIC_URL
    return str(request.base_url).rstrip("/")


def resource_url(request: Request) -> str:
    return base_url(request) + "/mcp"


def _secret() -> bytes:
    return (config.SECRET_KEY or config.ADMIN_PASSWORD or "estudio").encode()


# ----------------------------------------------------------------------- Metadatos
@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(request: Request):
    b = base_url(request)
    return {"resource": b + "/mcp", "authorization_servers": [b],
            "scopes_supported": SCOPES_SUPPORTED, "bearer_methods_supported": ["header"],
            "resource_name": "Estudio de Video Educativo · UNACHI"}


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
@router.get("/.well-known/openid-configuration")
def as_metadata(request: Request):
    b = base_url(request)
    return {"issuer": b,
            "authorization_endpoint": b + "/oauth/authorize",
            "token_endpoint": b + "/oauth/token",
            "registration_endpoint": b + "/oauth/register",
            "revocation_endpoint": b + "/oauth/revoke",
            "scopes_supported": SCOPES_SUPPORTED,
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "service_documentation": b + "/"}


# ----------------------------------------------------------------------- Registro dinámico
def _valid_redirect(u: str) -> bool:
    p = urlsplit(u)
    if p.fragment:
        return False
    if p.scheme == "https" and p.netloc:
        return True
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")


@router.post("/oauth/register")
async def register(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": "JSON inválido"}, 400)
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 10 or \
            not all(isinstance(u, str) and len(u) < 500 and _valid_redirect(u) for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "redirect_uris debe ser una lista de URL https (o loopback)"}, 400)
    method = body.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        method = "none"
    grants = body.get("grant_types") or ["authorization_code", "refresh_token"]
    cid = "mcp_" + secrets.token_urlsafe(18)
    secret = secrets.token_urlsafe(32) if method != "none" else None
    name = str(body.get("client_name") or "Cliente MCP")[:120]
    db.ex("INSERT INTO oauth_clients(client_id, secret_hash, name, redirect_uris, auth_method, created_at) "
          "VALUES (?,?,?,?,?,?)", cid, _h(secret) if secret else None, name, db.jdump(uris), method, db.now())
    out = {"client_id": cid, "client_id_issued_at": int(db.now()), "client_name": name,
           "redirect_uris": uris, "grant_types": grants, "response_types": ["code"],
           "token_endpoint_auth_method": method, "scope": " ".join(SCOPES_SUPPORTED)}
    if secret:
        out["client_secret"] = secret
        out["client_secret_expires_at"] = 0
    return JSONResponse(out, 201)


def _redirect_ok(client, uri: str) -> bool:
    regs = db.jload(client["redirect_uris"], [])
    if uri in regs:
        return True
    # Clientes nativos (p. ej., Claude Code): redirección loopback con puerto variable (RFC 8252 §7.3)
    p = urlsplit(uri)
    if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"):
        for r in regs:
            q = urlsplit(r)
            if q.scheme == "http" and q.hostname == p.hostname and q.path == p.path:
                return True
    return False


# ----------------------------------------------------------------------- Autorización
AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "state", "scope",
               "code_challenge", "code_challenge_method", "resource")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#15191d;color:#e8ecef;
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
.card{{width:min(440px,calc(100vw - 32px));background:#1e2429;border:1px solid #2e363d;border-radius:14px;
padding:26px 26px 22px;box-shadow:0 20px 60px rgba(0,0,0,.5)}}
h1{{font-size:19px;margin:0 0 4px}} .brand{{color:#7fb069;font-weight:700;font-size:13px;letter-spacing:.04em}}
p{{color:#aab4bc;margin:8px 0}} b{{color:#e8ecef}}
label{{display:block;margin:12px 0 4px;font-size:13px;color:#aab4bc}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid #3a444c;
background:#13171a;color:#e8ecef;font-size:15px}}
.row{{display:flex;gap:10px;margin-top:20px}} button{{flex:1;padding:11px;border-radius:8px;border:1px solid #3a444c;
background:#262d33;color:#e8ecef;font-size:15px;cursor:pointer}}
button.primary{{background:#4A7729;border-color:#4A7729;color:#fff;font-weight:600}}
ul{{color:#cbd3d9;padding-left:20px;margin:8px 0}} .err{{color:#ff8a80}} .host{{font-family:ui-monospace,monospace;color:#e8ecef}}
</style></head><body><div class="card"><div class="brand">ESTUDIO DE VIDEO EDUCATIVO · UNACHI</div>{body}</div></body></html>""",
                        status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                         "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'"})


def _hidden(params: dict) -> str:
    return "".join(f'<input type="hidden" name="{k}" value="{html.escape(params[k] or "")}">'
                   for k in AUTH_PARAMS if params.get(k) is not None)


def _validate(params: dict):
    """Devuelve (client, error_html). Los errores de cliente o redirect no redirigen (RFC 6749 §4.1.2.1)."""
    client = db.q1("SELECT * FROM oauth_clients WHERE client_id=?", params.get("client_id") or "")
    if not client:
        return None, "Cliente desconocido. Vuelva a agregar el conector."
    if not params.get("redirect_uri") or not _redirect_ok(client, params["redirect_uri"]):
        return None, "La dirección de retorno no está registrada para este cliente."
    return client, None


def _error_redirect(params: dict, error: str, desc: str):
    q = {"error": error, "error_description": desc}
    if params.get("state"):
        q["state"] = params["state"]
    sep = "&" if "?" in params["redirect_uri"] else "?"
    return RedirectResponse(params["redirect_uri"] + sep + urlencode(q), 302)


def _session_user(request: Request):
    tok = request.cookies.get(auth.SESSION_COOKIE)
    if not tok:
        return None, None
    row = db.q1("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=? AND s.expires_at>? AND u.active=1", tok, db.now())
    return (row, tok) if row else (None, None)


def _consent_token(session_tok: str, params: dict) -> str:
    msg = "|".join([session_tok] + [params.get(k) or "" for k in AUTH_PARAMS]).encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def _consent_page(user, client, params: dict, session_tok: str) -> HTMLResponse:
    host = urlsplit(params["redirect_uri"]).hostname or ""
    is_claude = params["redirect_uri"] in CLAUDE_CALLBACKS
    loop = host in ("localhost", "127.0.0.1", "::1")
    warn = ('<p class="err">Atención: la respuesta irá a un programa en este equipo (localhost). '
            'Apruebe solo si usted lo inició.</p>' if loop else "")
    body = f"""<h1>Autorizar acceso</h1>
<p><b>{html.escape(client['name'] or 'Cliente MCP')}</b>{' (Claude)' if is_claude else ''} solicita acceso a su cuenta
<b>{html.escape(user['username'])}</b>.</p>
<p>Podrá, en su nombre:</p>
<ul><li>Ver sus proyectos, medios, transcripciones y exportaciones</li>
<li>Editar proyectos (recortar silencios, capítulos, preguntas)</li>
<li>Iniciar transcripciones, quitar fondos y exportar videos</li></ul>
<p>La respuesta se enviará a <span class="host">{html.escape(host)}</span>.</p>{warn}
<form method="post" action="/oauth/authorize">{_hidden(params)}
<input type="hidden" name="csrf" value="{_consent_token(session_tok, params)}">
<div class="row"><button name="decision" value="deny">Cancelar</button>
<button class="primary" name="decision" value="allow">Permitir</button></div></form>
<p style="font-size:12px;margin-top:14px">Puede revocar el acceso cuando quiera desde la configuración de conectores de Claude.</p>"""
    return _page("Autorizar acceso", body)


def _login_page(params: dict, error: str = "") -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    body = f"""<h1>Iniciar sesión</h1><p>Use su cuenta del Estudio de Video Educativo para conectar Claude.</p>{err}
<form method="post" action="/oauth/authorize">{_hidden(params)}
<label>Usuario</label><input name="username" autocomplete="username" required autofocus>
<label>Contraseña</label><input name="password" type="password" autocomplete="current-password" required>
<div class="row"><button class="primary" name="decision" value="login">Entrar</button></div></form>"""
    return _page("Iniciar sesión", body, 401 if error else 200)


def _check_request(params: dict):
    if params.get("response_type") != "code":
        return "unsupported_response_type", "Solo se admite response_type=code"
    if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
        return "invalid_request", "Se requiere PKCE con code_challenge_method=S256"
    return None


@router.get("/oauth/authorize")
def authorize_get(request: Request):
    params = {k: request.query_params.get(k) for k in AUTH_PARAMS}
    client, err = _validate(params)
    if err:
        return _page("Error", f"<h1>No se puede continuar</h1><p class='err'>{html.escape(err)}</p>", 400)
    bad = _check_request(params)
    if bad:
        return _error_redirect(params, *bad)
    user, tok = _session_user(request)
    if not user:
        return _login_page(params)
    return _consent_page(user, client, params, tok)


@router.post("/oauth/authorize")
async def authorize_post(request: Request):
    form = await request.form()
    params = {k: form.get(k) for k in AUTH_PARAMS}
    client, err = _validate(params)
    if err:
        return _page("Error", f"<h1>No se puede continuar</h1><p class='err'>{html.escape(err)}</p>", 400)
    bad = _check_request(params)
    if bad:
        return _error_redirect(params, *bad)
    decision = form.get("decision")
    if decision == "login":
        ip = request.client.host if request.client else "?"
        uname = str(form.get("username") or "").strip()
        key = f"{ip}|{uname.lower()}"
        if auth.throttled(key):
            return _login_page(params, "Demasiados intentos. Espere unos minutos.")
        u = db.q1("SELECT * FROM users WHERE username=? AND active=1", uname)
        if not u or not auth.verify_password(str(form.get("password") or ""), u["pw_hash"]):
            auth.register_failure(key)
            return _login_page(params, "Usuario o contraseña incorrectos.")
        tok = auth.create_session(u["id"])
        resp = _consent_page(u, client, params, tok)
        resp.set_cookie(auth.SESSION_COOKIE, tok, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        secure=config.COOKIE_SECURE, samesite="lax", path="/")
        return resp
    user, tok = _session_user(request)
    if not user:
        return _login_page(params)
    if not hmac.compare_digest(str(form.get("csrf") or ""), _consent_token(tok, params)):
        return _page("Error", "<h1>Solicitud no válida</h1><p class='err'>Vuelva a intentarlo desde Claude.</p>", 400)
    if decision != "allow":
        return _error_redirect(params, "access_denied", "El usuario rechazó el acceso")
    code = secrets.token_urlsafe(32)
    scope = " ".join(s for s in (params.get("scope") or SCOPE).split() if s in SCOPES_SUPPORTED) or SCOPE
    db.ex("INSERT INTO oauth_codes(code_hash, client_id, user_id, redirect_uri, challenge, scope, resource, "
          "expires_at) VALUES (?,?,?,?,?,?,?,?)", _h(code), client["client_id"], user["id"],
          params["redirect_uri"], params["code_challenge"], scope, params.get("resource"), db.now() + CODE_TTL)
    q = {"code": code}
    if params.get("state"):
        q["state"] = params["state"]
    q["iss"] = base_url(request)
    sep = "&" if "?" in params["redirect_uri"] else "?"
    return RedirectResponse(params["redirect_uri"] + sep + urlencode(q), 302)


# ----------------------------------------------------------------------- Tokens
def _token_error(error: str, desc: str, status: int = 400):
    return JSONResponse({"error": error, "error_description": desc}, status,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _client_auth(request: Request, form) -> tuple[dict | None, str | None]:
    cid, secret = form.get("client_id"), form.get("client_secret")
    hdr = request.headers.get("authorization", "")
    if hdr.lower().startswith("basic "):
        try:
            from urllib.parse import unquote
            u, _, p = base64.b64decode(hdr[6:]).decode().partition(":")
            cid, secret = unquote(u), unquote(p)
        except (ValueError, UnicodeDecodeError):
            return None, "invalid_client"
    c = db.q1("SELECT * FROM oauth_clients WHERE client_id=?", cid or "")
    if not c:
        return None, "invalid_client"
    if c["secret_hash"] and not (secret and hmac.compare_digest(_h(secret), c["secret_hash"])):
        return None, "invalid_client"
    return c, None


def _issue(client_id: str, user_id: int, scope: str, resource: str | None) -> dict:
    t = db.now()
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
    db.ex("INSERT INTO oauth_tokens(token_hash, kind, client_id, user_id, scope, resource, expires_at, created_at) "
          "VALUES (?,?,?,?,?,?,?,?)", _h(access), "access", client_id, user_id, scope, resource, t + ACCESS_TTL, t)
    db.ex("INSERT INTO oauth_tokens(token_hash, kind, client_id, user_id, scope, resource, expires_at, created_at) "
          "VALUES (?,?,?,?,?,?,?,?)", _h(refresh), "refresh", client_id, user_id, scope, resource, t + REFRESH_TTL, t)
    db.ex("DELETE FROM oauth_tokens WHERE expires_at < ?", t)
    db.ex("DELETE FROM oauth_codes WHERE expires_at < ?", t)
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": refresh, "scope": scope}


@router.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    client, err = _client_auth(request, form)
    if err:
        return _token_error("invalid_client", "Autenticación del cliente fallida", 401)
    grant = form.get("grant_type")
    if grant == "authorization_code":
        code = db.q1("SELECT * FROM oauth_codes WHERE code_hash=?", _h(str(form.get("code") or "")))
        if code:
            db.ex("DELETE FROM oauth_codes WHERE code_hash=?", code["code_hash"])   # un solo uso
        if not code or code["expires_at"] < db.now() or code["client_id"] != client["client_id"]:
            return _token_error("invalid_grant", "Código inválido o vencido")
        if form.get("redirect_uri") and form.get("redirect_uri") != code["redirect_uri"]:
            return _token_error("invalid_grant", "redirect_uri no coincide")
        verifier = str(form.get("code_verifier") or "")
        calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not verifier or not hmac.compare_digest(calc, code["challenge"]):
            return _token_error("invalid_grant", "PKCE: code_verifier no válido")
        out = _issue(client["client_id"], code["user_id"], code["scope"], form.get("resource") or code["resource"])
    elif grant == "refresh_token":
        rt = db.q1("SELECT * FROM oauth_tokens WHERE token_hash=? AND kind='refresh'",
                   _h(str(form.get("refresh_token") or "")))
        if not rt or rt["expires_at"] < db.now() or rt["client_id"] != client["client_id"]:
            return _token_error("invalid_grant", "Refresh token inválido o vencido")
        u = db.q1("SELECT active FROM users WHERE id=?", rt["user_id"])
        if not u or not u["active"]:
            return _token_error("invalid_grant", "Usuario desactivado")
        db.ex("DELETE FROM oauth_tokens WHERE token_hash=?", rt["token_hash"])   # rotación
        out = _issue(client["client_id"], rt["user_id"], rt["scope"], rt["resource"])
    else:
        return _token_error("unsupported_grant_type", "Use authorization_code o refresh_token")
    return JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/oauth/revoke")
async def revoke(request: Request):
    form = await request.form()
    db.ex("DELETE FROM oauth_tokens WHERE token_hash=?", _h(str(form.get("token") or "")))
    return JSONResponse({}, 200)


def user_from_bearer(request: Request):
    """Usuario del token de acceso, o None."""
    hdr = request.headers.get("authorization", "")
    if not hdr.lower().startswith("bearer "):
        return None
    row = db.q1("SELECT u.* FROM oauth_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=? "
                "AND t.kind='access' AND t.expires_at>? AND u.active=1", _h(hdr[7:].strip()), db.now())
    if not row:
        return None
    return {k: row[k] for k in row.keys() if k != "pw_hash"}


def unauthorized(request: Request, desc: str = "Se requiere autorización") -> JSONResponse:
    meta = base_url(request) + "/.well-known/oauth-protected-resource"
    return JSONResponse({"error": "invalid_token", "error_description": desc}, 401, headers={
        "WWW-Authenticate": f'Bearer realm="estudio", resource_metadata="{meta}", scope="{SCOPE}"'})
