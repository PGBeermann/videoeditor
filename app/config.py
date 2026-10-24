"""Configuración central, leída de variables de entorno."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
MEDIA_DIR = DATA_DIR / "media"          # originales subidos/grabados
PROXY_DIR = DATA_DIR / "proxies"        # versiones H.264 720p para la vista previa
ASSET_DIR = DATA_DIR / "assets"         # PNG de capas (texto, LaTeX, moléculas)
EXPORT_DIR = DATA_DIR / "exports"       # renders finales y paquetes SCORM
MODEL_DIR = DATA_DIR / "models"         # caché de modelos Whisper
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "estudio.db"

for d in (MEDIA_DIR, PROXY_DIR, ASSET_DIR, EXPORT_DIR, MODEL_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "8192"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "0"))  # 0 = automático

# Calidad de codificación final (x264). CRF 18-20 ≈ visualmente sin pérdidas.
RENDER_CRF = int(os.environ.get("RENDER_CRF", "20"))
RENDER_PRESET = os.environ.get("RENDER_PRESET", "veryfast")
WORKER_POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "2"))

# Colores institucionales UNACHI (Pantone 364C / 187C, aproximación sRGB)
BRAND_GREEN = "#4A7729"
BRAND_RED = "#A6192E"
