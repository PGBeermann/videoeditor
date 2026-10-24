"""Supervisor del contenedor: ejecuta el servidor web y el procesador de trabajos.

Permite desplegar el editor como una sola «Aplicación» de Dokploy (un contenedor).
Si el worker termina por un error, se reinicia automáticamente.
En docker-compose, el servicio «web» define EMBEDDED_WORKER=0 porque el worker
corre en su propio servicio.
"""
import os
import signal
import subprocess
import sys
import time

PORT = os.environ.get("PORT", "8000")
WEB = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", PORT,
       "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
WORKER = [sys.executable, "-m", "app.worker"]

_stop = False


def _sig(signum, _frame):
    global _stop
    _stop = True


def main() -> None:
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    embedded = os.environ.get("EMBEDDED_WORKER", "1") == "1"
    web = subprocess.Popen(WEB)
    worker = subprocess.Popen(WORKER) if embedded else None
    print(f"[run] web pid {web.pid}" + (f", worker pid {worker.pid}" if worker else ""), flush=True)
    while not _stop:
        if web.poll() is not None:
            print(f"[run] el servidor web terminó (código {web.returncode})", flush=True)
            break
        if worker is not None and worker.poll() is not None:
            print(f"[run] el worker terminó (código {worker.returncode}); reiniciando en 3 s", flush=True)
            time.sleep(3)
            worker = subprocess.Popen(WORKER)
        time.sleep(1)
    for p in (worker, web):
        if p is not None and p.poll() is None:
            p.terminate()
    for p in (worker, web):
        if p is not None:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
    sys.exit(web.returncode or 0)


if __name__ == "__main__":
    main()
