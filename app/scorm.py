"""Empaquetado SCORM 1.2 (compatible con Moodle) del reproductor interactivo.

El paquete contiene el video, subtítulos, capítulos y preguntas incrustadas; informa
al LMS el estado de la lección (incomplete/completed/passed) y la calificación
(cmi.core.score.raw) según las respuestas del estudiante.
"""
from __future__ import annotations

import html
import json
import zipfile
from pathlib import Path

from . import db

MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest identifier="EV-{id}" version="1.0"
  xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2"
  xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_rootv1p2"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.imsproject.org/xsd/imscp_rootv1p1p2 imscp_rootv1p1p2.xsd
                      http://www.imsglobal.org/xsd/imsmd_rootv1p2p1 imsmd_rootv1p2p1.xsd
                      http://www.adlnet.org/xsd/adlcp_rootv1p2 adlcp_rootv1p2.xsd">
  <metadata><schema>ADL SCORM</schema><schemaversion>1.2</schemaversion></metadata>
  <organizations default="ORG-{id}">
    <organization identifier="ORG-{id}">
      <title>{title}</title>
      <item identifier="ITEM-{id}" identifierref="RES-{id}">
        <title>{title}</title>
        <adlcp:masteryscore>{mastery}</adlcp:masteryscore>
      </item>
    </organization>
  </organizations>
  <resources>
    <resource identifier="RES-{id}" type="webcontent" adlcp:scormtype="sco" href="index.html">
{files}
    </resource>
  </resources>
</manifest>
"""

INDEX = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="player.css">
</head><body class="ev-scorm">
<div id="player"></div>
<script>window.PLAYER_CONFIG = {config};</script>
<script src="player.js"></script>
<script>EVPlayer.mount(document.getElementById('player'), window.PLAYER_CONFIG);</script>
</body></html>
"""


def build_package(export: dict, static_dir: Path) -> Path:
    files = db.jload(export["files"], {})
    meta = db.jload(export["meta"], {})
    out_dir = Path(files["mp4"]).parent
    dst = out_dir / "scorm.zip"
    if dst.exists():
        return dst
    payload = {"video.mp4": files["mp4"]}
    tracks = []
    if "vtt" in files:
        payload["subtitulos_es.vtt"] = files["vtt"]
        tracks.append({"src": "subtitulos_es.vtt", "label": "Español", "lang": "es"})
    if "vtt_en" in files:
        payload["subtitles_en.vtt"] = files["vtt_en"]
        tracks.append({"src": "subtitles_en.vtt", "label": "English", "lang": "en"})
    if "poster" in files:
        payload["poster.jpg"] = files["poster"]
    if "txt" in files:
        payload["transcripcion.txt"] = files["txt"]
    quizzes = [{k: q.get(k) for k in ("t", "question", "options", "correct", "explanation")}
               for q in meta.get("quizzes", [])]
    cfg = {"title": export["title"], "video": "video.mp4",
           "poster": "poster.jpg" if "poster" in files else None, "tracks": tracks,
           "chapters": meta.get("chapters", []), "quizzes": quizzes,
           "duration": meta.get("duration"), "mode": "scorm",
           "transcript": "transcripcion.txt" if "txt" in files else None}
    title = html.escape(export["title"])
    index = INDEX.format(title=title,
                         config=json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/"))
    file_list = ["index.html", "player.js", "player.css", *payload.keys()]
    manifest = MANIFEST.format(id=export["id"][:12], title=title,
                               mastery=70 if quizzes else 0,
                               files="\n".join(f'      <file href="{f}"/>' for f in file_list))
    tmp = dst.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr("imsmanifest.xml", manifest, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("index.html", index, compress_type=zipfile.ZIP_DEFLATED)
        for name in ("player.js", "player.css"):
            z.write(static_dir / "player" / name, name, compress_type=zipfile.ZIP_DEFLATED)
        for name, src in payload.items():
            comp = zipfile.ZIP_STORED if name.endswith((".mp4", ".jpg")) else zipfile.ZIP_DEFLATED
            z.write(src, name, compress_type=comp)
    tmp.rename(dst)
    return dst
