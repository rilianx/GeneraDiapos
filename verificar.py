"""Revisa qué falta instalar para correr el pipeline, y cómo instalarlo.

    python verificar.py

Usa solo la biblioteca estándar, así funciona aunque falten las dependencias.
Sale con código 1 si falta algo obligatorio.
"""
from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
import sys

OK, NO, OPC = "✔", "✘", "·"


def navegador_mermaid() -> str | None:
    """Lo mismo que _chromium de beamer_graph (salta los de snap), sin importarlo."""
    import os
    from pathlib import Path
    if os.environ.get("PUPPETEER_EXECUTABLE_PATH"):
        return os.environ["PUPPETEER_EXECUTABLE_PATH"]
    for c in ("/opt/pw-browsers/chromium", shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
              shutil.which("chromium"), shutil.which("chromium-browser")):
        if not c or not Path(c).exists():
            continue
        real = Path(c).resolve()
        try:
            snap = str(real).startswith("/snap/") or (real.stat().st_size < 20000
                                                      and "/snap/" in real.read_text(errors="ignore"))
        except OSError:
            snap = False
        if not snap:
            return c
    return None                     # mmdc probará con el Chrome propio de puppeteer, si lo hay

# (módulo de Python, paquete pip, para qué, obligatorio)
PAQUETES = [
    ("langgraph", "langgraph", "el grafo del pipeline", True),
    ("langgraph.checkpoint.sqlite", "langgraph-checkpoint-sqlite", "modo Claude Code (estado entre ejecuciones)", True),
    ("pydantic", "pydantic", "esquemas del guion", True),
    ("pymupdf4llm", "pymupdf4llm", "leer el PDF del paper", True),
    ("pymupdf", "pymupdf", "figuras y texto del PDF", True),
    ("langchain_openai", "langchain-openai", "proveedor OpenAI (API)", False),
    ("langchain_anthropic", "langchain-anthropic", "proveedor Anthropic (API)", False),
    ("pptx", "python-pptx", "--pptx / --a-pptx", False),
    ("mcp.server.fastmcp", "mcp<2", "herramientas MCP para Claude Code (mcp_servidor.py)", False),
]
# (archivo de TeX, para qué)
TEX = [("beamer.cls", "beamer"), ("beamerthememetropolis.sty", "tema metropolis"),
       ("algorithm2e.sty", "pseudocódigo"), ("booktabs.sty", "tablas"), ("tikz.sty", "tikz"),
       ("standalone.cls", "imágenes de respaldo de fórmulas en --pptx")]


def hay_modulo(nombre: str) -> bool:
    try:
        return importlib.util.find_spec(nombre) is not None
    except (ImportError, ValueError):
        return False


def pandoc() -> str | None:
    if shutil.which("pandoc"):
        return shutil.which("pandoc")
    if hay_modulo("pypandoc"):
        try:
            import pypandoc
            return pypandoc.get_pandoc_path()
        except OSError:
            return None
    return None


def probar_dot() -> tuple[bool, str]:
    """Que dot exista no basta: se dibuja un grafo mínimo."""
    if not shutil.which("dot"):
        return False, ""
    r = subprocess.run(["dot", "-Tplain"], input="digraph { a -> b }", capture_output=True, text=True, timeout=30)
    return r.returncode == 0, " ".join(r.stderr.split())[:120]


def probar_mmdc() -> tuple[bool, str]:
    """Que mmdc exista no basta (una instalación rota falla al abrir su página): se dibuja uno."""
    import json
    import os
    import tempfile
    if not shutil.which("mmdc"):
        return False, ""
    with tempfile.TemporaryDirectory() as t:
        chrome = navegador_mermaid()
        with open(f"{t}/p.json", "w") as f:
            json.dump({"args": ["--no-sandbox"], **({"executablePath": chrome} if chrome else {})}, f)
        with open(f"{t}/d.mmd", "w") as f:
            f.write("flowchart LR\n  a --> b\n")
        try:
            r = subprocess.run(["mmdc", "-q", "-p", f"{t}/p.json", "-i", f"{t}/d.mmd", "-o", f"{t}/d.png"],
                               capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return False, "no respondió en 90 s"
        if r.returncode == 0 and os.path.exists(f"{t}/d.png"):
            return True, ""
        lineas = [l.strip() for l in (r.stderr or r.stdout).splitlines() if l.strip() and not l.strip().startswith("at ")]
        return False, " ".join(lineas[:2])[:160] or "no dibujó el diagrama de prueba"


def main() -> int:
    faltan_pip, faltan_tex, obligatorio = [], [], False
    print("# Qué hay y qué falta\n")

    v = sys.version_info
    ok = v >= (3, 11)
    print(f"{OK if ok else NO} Python {v.major}.{v.minor} (se necesita 3.11 o superior)")
    obligatorio |= not ok

    print("\nPaquetes de Python:")
    for mod, pip, para, req in PAQUETES:
        hay = hay_modulo(mod)
        print(f"  {OK if hay else (NO if req else OPC)} {pip:28} {para}{'' if req else ' (opcional)'}")
        if not hay:
            faltan_pip.append(pip)
            obligatorio |= req
    pd = pandoc()
    print(f"  {OK if pd else OPC} {'pandoc (pypandoc_binary)':28} fórmulas editables en --pptx (opcional)")
    if not pd:
        faltan_pip.append("pypandoc_binary")

    print("\nLaTeX:")
    pdflatex, kpse = shutil.which("pdflatex"), shutil.which("kpsewhich")
    print(f"  {OK if pdflatex else NO} pdflatex")
    obligatorio |= not pdflatex
    for archivo, para in TEX:
        hay = bool(kpse) and subprocess.run([kpse, archivo], capture_output=True, text=True).stdout.strip() != ""
        req = archivo != "standalone.cls"
        print(f"  {OK if hay else (NO if req else OPC)} {archivo:28} {para}{'' if req else ' (opcional)'}")
        if not hay:
            faltan_tex.append(archivo)
            obligatorio |= req

    print("\nOtros (opcionales):")
    dot, dot_msg = probar_dot()
    print(f"  {OK if dot else (NO if shutil.which('dot') else OPC)} Graphviz (dot)               "
          f"diapositivas de tipo diagram{'' if dot or not dot_msg else ' — ' + dot_msg}")
    mmdc, mmdc_msg = probar_mmdc()
    print(f"  {OK if mmdc else (NO if shutil.which('mmdc') else OPC)} Mermaid (mmdc)               "
          f"diagramas en Mermaid (preferido si está){'' if mmdc or not mmdc_msg else ' — ' + mmdc_msg}")
    lo = shutil.which("soffice") or shutil.which("libreoffice")
    print(f"  {OK if lo else OPC} LibreOffice                  ver el .pptx sin PowerPoint")

    if not dot:
        print("\nPara diagramas: sudo apt-get install graphviz   (macOS: brew install graphviz)")
    if not mmdc:
        if shutil.which("mmdc") and not navegador_mermaid():
            print("Mermaid necesita un Chrome fuera de snap (el de snap no lee /usr/local/lib): instala\n"
                  "  Google Chrome (.deb) o define PUPPETEER_EXECUTABLE_PATH con la ruta del navegador")
        elif shutil.which("mmdc"):
            print("Mermaid está instalado pero no funciona; reinstálalo:\n"
                  "  npm uninstall -g @mermaid-js/mermaid-cli && npm install -g @mermaid-js/mermaid-cli\n"
                  "  (con sudo si lo instalaste con sudo; necesita Node 18+ y descarga su Chromium)")
        else:
            print("Para diagramas en Mermaid (requiere Node 18+): npm install -g @mermaid-js/mermaid-cli")
    if not faltan_pip and not faltan_tex and pdflatex:
        print("\nTodo listo.")
        return 0
    print("\n# Para instalar lo que falta\n")
    if faltan_pip:
        print("pip install -r requirements.txt")
        print(f"   (o solo lo que falta: pip install {' '.join(faltan_pip)})")
    if faltan_tex or not pdflatex:
        so = platform.system()
        if so == "Linux":
            print("sudo apt-get install texlive-latex-recommended texlive-latex-extra \\\n"
                  "  texlive-pictures texlive-science texlive-fonts-recommended")
        elif so == "Darwin":
            print("Instala MacTeX (https://tug.org/mactex/) o: brew install --cask mactex-no-gui")
        else:
            print("Instala MiKTeX (https://miktex.org) o TeX Live; MiKTeX baja los paquetes al usarlos")
    print("\nLuego: python verificar.py   y   python tests/smoke_test.py   (debe decir OK)")
    return 1 if obligatorio else 0


if __name__ == "__main__":
    sys.exit(main())
