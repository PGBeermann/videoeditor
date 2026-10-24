"""Acceso a SQLite (modo WAL: web y worker comparten el mismo archivo en el volumen)."""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from .config import DB_PATH

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'docente',      -- admin | docente
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  data TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_members (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'editor',        -- editor | revisor
  PRIMARY KEY (project_id, user_id)
);
CREATE TABLE IF NOT EXISTS media (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'video',         -- video | audio | image
  duration REAL NOT NULL DEFAULT 0,
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  fps REAL NOT NULL DEFAULT 0,
  has_audio INTEGER NOT NULL DEFAULT 0,
  proxy_path TEXT,
  waveform_path TEXT,
  status TEXT NOT NULL DEFAULT 'processing',  -- processing | ready | error
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transcripts (
  media_id TEXT PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
  language TEXT,
  words TEXT NOT NULL,
  translated TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
  media_id TEXT,
  type TEXT NOT NULL,                         -- ingest | transcribe | render
  params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',      -- queued | running | done | error
  progress REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  result TEXT,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS exports (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  job_id TEXT,
  title TEXT NOT NULL,
  format TEXT NOT NULL,                       -- 16:9 | 9:16 | 1:1
  token TEXT UNIQUE NOT NULL,                 -- enlace público del reproductor
  public INTEGER NOT NULL DEFAULT 0,
  files TEXT NOT NULL DEFAULT '{}',           -- {"mp4": ruta, "srt": ruta, ...}
  meta TEXT NOT NULL DEFAULT '{}',            -- capítulos, preguntas, duración
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  t REAL NOT NULL,
  text TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS views (
  id TEXT PRIMARY KEY,
  export_id TEXT NOT NULL REFERENCES exports(id) ON DELETE CASCADE,
  buckets TEXT NOT NULL DEFAULT '[]',         -- segundos vistos, en tramos de 5 s
  max_t REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quiz_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  export_id TEXT NOT NULL REFERENCES exports(id) ON DELETE CASCADE,
  view_id TEXT,
  quiz_idx INTEGER NOT NULL,
  choice INTEGER NOT NULL,
  correct INTEGER NOT NULL,
  created_at REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


def init_db() -> None:
    connect().executescript(SCHEMA)


@contextmanager
def tx():
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def q(sql: str, *args):
    return connect().execute(sql, args).fetchall()


def q1(sql: str, *args):
    return connect().execute(sql, args).fetchone()


def ex(sql: str, *args):
    return connect().execute(sql, args)


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


def jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def jload(s, default=None):
    if s is None:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def enqueue(job_type: str, project_id: str | None, media_id: str | None = None,
            params: dict | None = None) -> str:
    jid = new_id()
    ex("INSERT INTO jobs(id, project_id, media_id, type, params, created_at) "
       "VALUES (?,?,?,?,?,?)", jid, project_id, media_id, job_type,
       jdump(params or {}), now())
    return jid
