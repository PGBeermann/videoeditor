# Estudio de Video Educativo — imagen única para los servicios «web» y «worker»

# ---------- Etapa 1: bibliotecas del navegador (MathJax 3 y RDKit.js) ----------
FROM node:22-alpine AS vendor
WORKDIR /v
RUN npm init -y >/dev/null \
 && npm install --no-audit --no-fund mathjax@3.2.2 @rdkit/rdkit@2026.9.1 \
 && mkdir -p out/mathjax out/rdkit \
 && cp node_modules/mathjax/es5/tex-svg-full.js out/mathjax/ \
 && cp node_modules/@rdkit/rdkit/dist/RDKit_minimal.js node_modules/@rdkit/rdkit/dist/RDKit_minimal.wasm out/rdkit/

# ---------- Etapa 2: aplicación ----------
FROM python:3.12-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    HF_HOME=/data/models/hf

# FFmpeg (render y audio), fuentes para subtítulos incrustados, curl para el healthcheck
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static
COPY --from=vendor /v/out ./static/vendor

# Modelo RNNoise para la reducción de ruido «intensa» (filtro arnndn de FFmpeg)
ADD https://raw.githubusercontent.com/richardpl/arnndn-models/master/sh.rnnn /app/models/rnnoise/sh.rnnn

RUN useradd --system --uid 10001 --create-home estudio \
 && mkdir -p /data \
 && chown -R estudio:estudio /data /app/models \
 && chmod 644 /app/models/rnnoise/sh.rnnn
USER estudio
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# Servidor web + procesador de trabajos en el mismo contenedor (ver app/run.py)
CMD ["python", "-m", "app.run"]
