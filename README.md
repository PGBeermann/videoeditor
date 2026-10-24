# Estudio de Video Educativo · UNACHI

Editor web de video para docentes: graba pantalla y cámara, edita borrando texto de la
transcripción, agrega ecuaciones, química y moléculas, procesa el audio a nivel profesional
y exporta MP4, subtítulos y paquetes SCORM para Moodle. Se despliega con Docker y Dokploy.

## Funciones

| Área | Qué incluye |
|---|---|
| Grabación | Pantalla + cámara (PiP circular o rectangular), solo pantalla, solo cámara, pizarra blanca + cámara; anotaciones a mano alzada que quedan grabadas; teleprompter con velocidad, tamaño y modo espejo; cuenta regresiva; pausa; medidor de nivel; mezcla de micrófono y audio del sistema |
| Edición | Línea de tiempo con pistas de video, capas, zoom, música y marcas; recortar, dividir, reordenar; deshacer/rehacer; atajos de teclado; autoguardado con control de versiones |
| Edición por texto | Transcripción automática con marcas de tiempo por palabra (faster-whisper); borrar palabras recorta el video; detección de muletillas; recorte automático de silencios; corrección de palabras para los subtítulos |
| Contenido científico | Ecuaciones LaTeX (MathJax 3), fórmulas y reacciones químicas con mhchem (`\ce{...}`), estructuras moleculares desde SMILES (RDKit.js), flechas, recuadros, rótulos, imágenes, portada institucional |
| Zoom | Acercamiento animado a un área con transición suave |
| Audio | Filtro paso alto, reducción de ruido (FFT o red neuronal RNNoise), de-esser, compresor, música con *ducking* automático y normalización de sonoridad EBU R128 en dos pasadas (−14, −16 o −23 LUFS) |
| Accesibilidad | Subtítulos en pista activable y archivos SRT/VTT; incrustación opcional; traducción automática al inglés (tarea *translate* de Whisper) |
| Lección | Capítulos incrustados en el MP4; preguntas de opción múltiple que pausan el video |
| Publicación | Formatos 16:9, 9:16 y 1:1 desde el mismo proyecto (fondo desenfocado o recorte), 1080p o 720p; enlace público con reproductor interactivo; paquete SCORM 1.2 con calificación |
| Revisión | Compartir proyectos con colegas como editor o revisor; comentarios anclados al tiempo |
| Analítica | Vistas, retención por tramos de 5 s y estadísticas de respuestas por pregunta |
| Administración | Usuarios con rol administrador o docente; contraseñas con scrypt; protección CSRF y límite de intentos de acceso |

## Arquitectura

```
Navegador ──HTTPS──> Traefik (Dokploy) ──> web (FastAPI + uvicorn, puerto 8000)
                                              │  SQLite (WAL) + archivos en el volumen /data
                                           worker (FFmpeg + faster-whisper)
```

* **web** sirve la interfaz y la API, recibe subidas por fragmentos de 16 MB (reanudables) y encola trabajos.
* **worker** procesa la cola: análisis y proxy de vista previa, transcripción y render.
* El render tiene tres etapas: (A) ensamblado de cortes, (B) composición de capas, zoom, reencuadre, subtítulos y cadena de audio, y (C) normalización de sonoridad en dos pasadas y empaquetado MP4 con capítulos.
* Las capas gráficas se rasterizan a PNG en el navegador y FFmpeg compone esos mismos PNG, de modo que lo que se ve en la vista previa es lo que se exporta.

## Despliegue con Dokploy

Hay dos formas válidas. En ambas, **los datos deben ir en un volumen persistente montado en `/data`**; sin él, cada redepliegue borra proyectos, medios y usuarios.

### Opción A — Aplicación (un contenedor)

1. **Create Service → Application**, proveedor GitHub, repositorio `PGBeermann/videoeditor`, rama `main`, *Build Type* **Dockerfile**.
2. **Environment:** pegue el contenido de `.env.example` y cambie `ADMIN_PASSWORD`.
3. **Advanced → Volumes → Add Volume:** tipo *Volume Mount*, nombre `videoeditor-data`, *Mount Path* `/data`.
4. **Domains:** dominio (p. ej. `videoeditor.vipcursos.cloud`), puerto **8000**, HTTPS.
5. **Deploy.** El contenedor ejecuta el servidor web y el procesador de trabajos (`app/run.py`), y reinicia el procesador si falla.

### Opción B — Compose (web y worker separados)

1. **Create Service → Compose**, el mismo repositorio, *Compose Path* `./docker-compose.yml`.
2. **Environment:** como en la opción A. El volumen `estudio-data` ya está declarado en el compose.
3. **Domains:** servicio **web**, puerto **8000**, HTTPS.
4. **Deploy.**

La primera transcripción descarga el modelo Whisper elegido (unos 480 MB para `small`) y lo guarda en el volumen.

### Recursos recomendados del VPS

* **CPU:** el render es intensivo en CPU (x264). En un entorno de prueba con 2 vCPU, 59 s de video 1080p con una capa, un zoom y reducción de ruido intensa se renderizaron en unos 91 s, es decir, aproximadamente 1,5 veces la duración. Con 4 vCPU espere un tiempo cercano a la duración del video (`RENDER_PRESET=veryfast`).
* **Transcripción:** como referencia, el benchmark del propio faster-whisper reporta 1 min 42 s para 13 min de audio con el modelo `small` en int8, usando 8 hilos de un Intel i7-12700K. En un VPS de 4 vCPU espere varias veces ese tiempo.
* **RAM:** 4 GB como mínimo; 8 GB si usa `WHISPER_MODEL=medium`.
* **Disco:** cada proyecto guarda los originales, el proxy y las exportaciones. Prevea unos 3 GB por hora de clase grabada.

### Respaldo

Todo lo persistente está en el volumen `estudio-data`: la base `estudio.db` y las carpetas `media`, `assets` y `exports`. Respáldelo con la función de *Volume Backups* de Dokploy o copiando el volumen con el servicio detenido.

## Desarrollo local (sin Docker)

```bash
pip install -r requirements.txt            # requiere FFmpeg en el sistema
cd static/vendor && npm init -y && npm i mathjax@3.2.2 @rdkit/rdkit@2026.9.1 \
  && mkdir -p mathjax rdkit && cp node_modules/mathjax/es5/tex-svg-full.js mathjax/ \
  && cp node_modules/@rdkit/rdkit/dist/RDKit_minimal.* rdkit/ && cd ../..
export DATA_DIR=./data ADMIN_PASSWORD=ClaveLocal123 COOKIE_SECURE=0
uvicorn app.main:app --reload &      # interfaz en http://localhost:8000
python -m app.worker                 # en otra terminal
```

Pruebas automatizadas (con el servidor y el worker en ejecución):

```bash
python -m tests.test_render /ruta/medios     # motor de render
python -m tests.test_api /ruta/medios        # API de punta a punta (incluye SCORM y analítica)
python -m tests.test_ui /ruta/capturas       # interfaz con Playwright
```

## Limitaciones conocidas

* La vista previa reproduce los cortes, capas, zoom y subtítulos, pero no los filtros de audio ni el *ducking*: estos se aplican solo en el render.
* La grabación se guarda en la memoria del navegador hasta que se sube; para sesiones de más de 45 minutos conviene dividir la clase en varias tomas.
* No incluye segmentación de personas sin pantalla verde ni reencuadre que siga el rostro. Ambas funciones requieren GPU.
* Los resúmenes o glosarios automáticos requieren un modelo de lenguaje externo y no están incluidos.

## Componentes de terceros

| Componente | Uso | Licencia |
|---|---|---|
| FFmpeg | Decodificación, filtros, codificación | LGPL/GPL (paquete de Debian) |
| faster-whisper / CTranslate2 | Transcripción | MIT |
| Modelos Whisper (OpenAI) | Reconocimiento de voz | MIT |
| MathJax 3 + mhchem | Ecuaciones y química | Apache 2.0 |
| RDKit.js (MinimalLib) | Estructuras moleculares | BSD-3 |
| Modelo RNNoise `sh.rnnn` (arnndn-models) | Reducción de ruido | Ver el repositorio richardpl/arnndn-models |
| FastAPI, Starlette, Uvicorn | Servidor web | MIT / BSD |

## Normas y referencias técnicas

* EBU R128 / ITU-R BS.1770: medición y normalización de sonoridad (filtro `loudnorm` de FFmpeg).
* WCAG 2.1, criterio 1.2.2: subtítulos para contenido pregrabado.
* ADL SCORM 1.2: empaquetado (`imsmanifest.xml`) y API de comunicación con el LMS (`cmi.core.*`).
