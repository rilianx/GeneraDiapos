"""Servidor MCP del pipeline: herramientas de SOLO LECTURA para que Claude revise su trabajo
antes de responder una tarea (modo Claude Code).

    python mcp_servidor.py        # stdio; Claude Code lo lanza solo vía .mcp.json

- validar_frame: lo mismo que diría compile_slide (compilación, lint, formato, estilo,
  diagramas, cifras) y una imagen de la diapositiva compilada.
- dibujar_diagrama: el PNG de un DOT con el estilo del pipeline, o el error de Graphviz.
- ver_pagina: una página del paper como imagen.

No escriben el estado del grafo ni el registro de errores: el pipeline sigue decidiendo los
pasos y valida todo al recibir la respuesta. Solo evitan rondas de refinado.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

import beamer_graph as bg

mcp = FastMCP("paper2beamer")


def _contexto(trabajo: str) -> dict:
    """Cabecera, estilo, figuras y paper de una corrida (--claude DIR); o los de por defecto."""
    ctx = {"head": None, "limits": bg.load_style(None)[1], "figuras_dir": None, "texto": "", "paper": None}
    d = Path(trabajo) if trabajo else None
    if d and (d / "estado.sqlite").exists():
        from langgraph.checkpoint.sqlite import SqliteSaver
        graph = bg.build_graph(SqliteSaver(sqlite3.connect(d / "estado.sqlite", check_same_thread=False)))
        v = graph.get_state({"configurable": {"thread_id": "beamer"}}).values
        ctx.update(head=v.get("head"), figuras_dir=v.get("figuras_dir"), texto=v.get("texto", ""),
                   limits=bg.load_style(v.get("style_path"))[1], paper=v.get("source_path"))
    if d and (d / "meta.json").exists() and not ctx["paper"]:
        ctx["paper"] = json.loads((d / "meta.json").read_text()).get("paper")
    if ctx["paper"] and d and not Path(ctx["paper"]).is_absolute():
        # la ruta se guardó relativa a donde se lanzó el pipeline (normalmente la raíz del repo)
        for base in (Path.cwd(), d.parent.parent, d.parent):
            if (base / ctx["paper"]).exists():
                ctx["paper"] = str(base / ctx["paper"])
                break
    if not ctx["head"]:
        ctx["head"], _ = bg.split_base(Path(bg.DEFAULT_BASE).read_text(),
                                       {"title": "Título", "authors": "Autores", "venue": ""})
    return ctx


@mcp.tool()
def validar_frame(frame: str, tipo: str, fuentes: list[str] | None = None, trabajo: str = "") -> list:
    """Valida un frame Beamer como lo hará el pipeline, sin gastar una ronda.

    frame: el \\begin{frame}…\\end{frame} completo. tipo: bullets, columns, block, equation,
    table, figure, algorithm o diagram. fuentes: ids que cita la diapo (tab1, fig2…).
    trabajo: carpeta --claude de la corrida (usa su cabecera, estilo, figuras y paper).
    Devuelve el diagnóstico y, si compila, una imagen de la diapositiva."""
    ctx = _contexto(trabajo)
    fuentes = fuentes or []
    figdir = ctx["figuras_dir"] or str(Path(tempfile.mkdtemp()) / "figuras")
    frame_tex, diag_err, diag_avisos = bg.expandir_diagramas(frame, figdir)
    errores = bg.lint_frame(bg.sin_diagramas(frame)) + diag_err
    carpeta = None
    if not errores:
        tex, off = bg.standalone(ctx["head"], frame_tex)
        errores, carpeta = bg.compile_tex(tex, offset=off, figuras=figdir)
    estilo = (bg.kind_check(frame, tipo, fuentes) + bg.style_check(bg.sin_diagramas(frame), ctx["limits"])
              + diag_avisos) if tipo in bg.KINDS else [f"tipo desconocido: {tipo}"]
    cifras = bg.soft_checks(frame, ctx["texto"]) if ctx["texto"] else []
    informe = {"compila": not errores, "errores": errores, "estilo": estilo,
               "avisos_cifras": cifras,
               "veredicto": ("listo para responder" if not errores and not estilo
                             else "corrígelo antes de responder")}
    salida: list = [json.dumps(informe, ensure_ascii=False, indent=1)]
    pdf = Path(carpeta) / "doc.pdf" if carpeta else None
    if not errores and pdf and pdf.exists():
        import pymupdf
        with pymupdf.open(str(pdf)) as doc:
            salida.append(Image(data=doc[0].get_pixmap(dpi=80).tobytes("png"), format="png"))
    return salida


@mcp.tool()
def dibujar_diagrama(dot: str) -> list:
    """Dibuja un grafo DOT con el estilo del pipeline. Devuelve el PNG, o el error de Graphviz y
    los avisos (demasiados nodos, letra ilegible)."""
    d = Path(tempfile.mkdtemp())
    ruta, errores, avisos = bg.render_diagrama(dot, d / "figuras")
    salida: list = [json.dumps({"errores": errores, "avisos": avisos}, ensure_ascii=False)]
    if ruta:
        salida.append(Image(path=str((d / ruta).with_suffix(".png"))))
    return salida


@mcp.tool()
def ver_pagina(n: int, trabajo: str = "", paper: str = "") -> list:
    """Muestra la página n (desde 1) del paper de la corrida (trabajo) o del PDF indicado."""
    ruta = paper or _contexto(trabajo)["paper"]
    if not ruta or not str(ruta).endswith(".pdf") or not Path(ruta).exists():
        return ["No hay un PDF del paper: pasa trabajo (carpeta --claude) o paper (ruta al PDF)."]
    import pymupdf
    with pymupdf.open(str(ruta)) as doc:
        if not 1 <= n <= len(doc):
            return [f"El paper tiene {len(doc)} páginas."]
        return [f"Página {n} de {len(doc)}", Image(data=doc[n - 1].get_pixmap(dpi=110).tobytes("png"),
                                                   format="png")]


if __name__ == "__main__":
    mcp.run()
