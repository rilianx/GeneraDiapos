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
    lo = shutil.which("soffice") or shutil.which("libreoffice")
    print(f"  {OK if lo else OPC} LibreOffice                  ver el .pptx sin PowerPoint")

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
