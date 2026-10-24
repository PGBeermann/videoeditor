"""Prueba de interfaz con Playwright (Chromium sin cabeza) contra un servidor local.

Uso: BASE=http://localhost:8765 ADMIN_PASSWORD=... python -m tests.test_ui /carpeta/capturas
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://localhost:8765")


def main(outdir):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required",
                                     "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        pg = b.new_page(viewport={"width": 1600, "height": 960})
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(BASE + "/")
        pg.fill("input[name=username]", "admin")
        pg.fill("input[name=password]", os.environ["ADMIN_PASSWORD"])
        pg.click("button[type=submit]")
        pg.wait_for_selector(".pcard")
        pg.screenshot(path=str(out / "1_proyectos.png"))
        pg.click(".pcard")
        pg.wait_for_selector(".tl-item.clip")
        pg.wait_for_timeout(1500)
        pg.screenshot(path=str(out / "2_editor.png"))

        # Capas: LaTeX, química y molécula
        pg.click("text=Capas")
        for label in ("Ecuación LaTeX", "Química (mhchem)", "Molécula (SMILES)"):
            pg.click("#tlRuler", position={"x": 140, "y": 10})
            pg.click(f"#layerButtons >> text={label}")
            pg.wait_for_timeout(2500)
        pg.wait_for_function("() => document.querySelectorAll('.layer:not(.pending) img').length >= 4", timeout=30000)
        pg.click("#tlRuler", position={"x": 150, "y": 10})
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(out / "3_capas.png"))

        # Edición por texto: seleccionar «eh» y eliminar
        pg.click("text=Texto")
        pg.wait_for_selector(".transcript .w")
        before = pg.evaluate("() => document.querySelectorAll('.tl-item.clip').length")
        pg.click(".transcript .w >> text=eh")
        pg.click("#btnCutSel")
        pg.wait_for_timeout(500)
        after = pg.evaluate("() => document.querySelectorAll('.tl-item.clip').length")
        assert after == before + 1, (before, after)
        assert "eh" not in pg.inner_text("#transcript").split()
        pg.screenshot(path=str(out / "4_texto.png"))
        # Deshacer
        pg.keyboard.press("Escape")
        pg.click("#btnUndo")
        pg.wait_for_timeout(300)
        assert pg.evaluate("() => document.querySelectorAll('.tl-item.clip').length") == before

        # Reproducir 2 s
        pg.click("#btnStart")
        pg.click("#btnPlay")
        pg.wait_for_timeout(2500)
        t = pg.inner_text("#tcNow")
        pg.click("#btnPlay")
        print("tiempo tras reproducir:", t)
        assert t not in ("0:00.0",), t

        # Lección y exportados
        pg.click("text=Lección")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(out / "5_leccion.png"))
        pg.click("text=Exportados")
        pg.wait_for_selector(".export-card")
        pg.screenshot(path=str(out / "6_exportados.png"))
        pg.wait_for_timeout(2500)  # autoguardado

        # Grabador (dispositivos simulados)
        pid = pg.url.rstrip("/").split("/")[-1]
        pg.goto(BASE + f"/grabar/{pid}")
        pg.click("text=Solo cámara")
        pg.wait_for_timeout(2500)
        pg.screenshot(path=str(out / "7_grabar.png"))

        # Reproductor público
        tok = pg.evaluate("""async () => { const r = await fetch('/api/projects/%s/exports'); const e = await r.json(); return e[0].token; }""" % pid)
        pg.goto(BASE + f"/v/{tok}")
        pg.wait_for_selector(".evp video")
        pg.evaluate("() => { const v = document.querySelector('video'); v.muted = true; v.currentTime = 6.9; v.play(); }")
        pg.wait_for_selector(".evp-quiz:not(.hidden)", timeout=15000)
        pg.screenshot(path=str(out / "8_reproductor.png"))
        b.close()
    errs = [e for e in errors if "favicon" not in e and "401" not in e]  # 401 = comprobación de sesión en el login
    print("errores de consola:", errs)
    assert not errs, errs
    print("UI OK")


if __name__ == "__main__":
    main(sys.argv[1])
