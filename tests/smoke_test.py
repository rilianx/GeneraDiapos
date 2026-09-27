"""Prueba sin API: simula el LLM para verificar el grafo, la compilación
por diapositiva, los ciclos de refinado y la pausa de revisión."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.types import Command

import beamer_graph as bg

PAPER = r"""\documentclass{article}\begin{document}
\section{Introducción}
Los solvers Branch and Bound por intervalos eligen qué variable bisecar en cada nodo.
\section{Método}
El Lagrangiano es
\begin{equation} L(x,\lambda)=f(x)+\sum_j \lambda_j g_j(x) \end{equation}
y se elige la variable con mayor impacto.
\section{Resultados}
La ganancia frente a smearsum es 1.43 en promedio sobre 76 instancias.
\end{document}"""

calls = {"write": {}, "refine": 0}


def fake_structured(model, schema, prompt):
    return bg.Outline(
        title="Demo", authors="A. Autor", venue="Revista", notation="$x$: variables",
        slides=[
            bg.SlideSpec(title="Portada", bullets=[], kind="title"),
            bg.SlideSpec(title="Método", bullets=["Lagrangiano"], kind="equation",
                         sources=["sec2", "eq1"]),
            bg.SlideSpec(title="Resultados", bullets=["Ganancia"], kind="bullets",
                         sources=["sec3"]),
        ])


def fake_text(model, prompt):
    if prompt.startswith("Esta diapositiva"):          # refine_slide
        calls["refine"] += 1
        if "Método" in prompt:
            return r"\begin{frame}{Método}$x\in\R^n$ y \[L(x,\lambda)\]\end{frame}"
        return r"\begin{frame}{Resultados}Ganancia 1.43 en 76 instancias.\end{frame}"
    title = "Método" if "Título: Método" in prompt else "Resultados"
    if title == "Método":                              # error: macro inexistente
        return r"\begin{frame}{Método}$\noexiste{x}$\end{frame}"
    wide = r"\[" + "+".join(["x_{%d}" % i for i in range(80)]) + r"\]"
    return r"\begin{frame}{Resultados}Ganancia 1.43, 99.9\%" + wide + r"\end{frame}"


# Escapes JSON que el modelo no dobló (\texttt → tab + "exttt") se restauran
assert bg.restaurar_escapes({"t": ["\texttt{x} \frac{a}{b} \beta \rho \neq 0"]}) == \
    {"t": [r"\texttt{x} \frac{a}{b} \beta \rho \neq 0"]}
assert bg.restaurar_escapes("a\n  \\item b") == "a\n  \\item b"   # saltos reales intactos

bg.call_structured, bg.call_text = fake_structured, fake_text

tmp = Path(tempfile.mkdtemp())
(tmp / "paper.tex").write_text(PAPER)
out = tmp / "out"
graph = bg.build_graph()
cfg = {"configurable": {"thread_id": "t"}, "max_concurrency": 2}
res = graph.invoke({"source_path": str(tmp / "paper.tex"), "out_dir": str(out),
                    "human_review": True}, cfg)
assert "__interrupt__" in res, "debía pausar para revisión"
edited = res["__interrupt__"][0].value["outline"]
edited["slides"][2]["title"] = "Resultados"
res = graph.invoke(Command(resume=edited), cfg)

print("chunks:", list(res["chunks"]))
print("refinados:", calls["refine"])
print((out / "informe.md").read_text())
assert (out / "presentacion.pdf").exists()
assert not res["log_errors"]
# Tipos de diapo: el guion debe respetar fuentes y variedad; el frame, su formato
spec = lambda k, src: bg.SlideSpec(title="T", bullets=["x"], kind=k, sources=src)
chunks = {"sec1": "", "tab1": "", "eq1": ""}
o = bg.Outline(title="", authors="", venue="", notation="", slides=[
    bg.SlideSpec(title="P", bullets=[], kind="title"), spec("bullets", ["sec1", "tab1"]),
    spec("bullets", ["sec1"]), spec("bullets", ["sec1"]), spec("block", ["sec1"])])
errs = bg.validate_outline(o, chunks)
assert any("cita tab*" in e for e in errs) and any("solo viñetas" in e for e in errs), errs
assert bg.kind_check(r"\begin{frame}{T}\begin{itemize}\item a\end{itemize}\end{frame}", "table", ["tab1"])
assert not bg.kind_check(r"\begin{frame}{T}\begin{tabular}{l}a\end{tabular}\end{frame}", "table", ["tab1"])
assert bg.kind_check(r"\begin{frame}{T}$x$\end{frame}", "block", ["eq1"]) == [
    "Formato (block): usa al menos un block, alertblock o exampleblock",
    "Formato (block): incluye al menos una ecuación en display (\\[ \\] o align)"]

assert bg.SlideSpec(title="T", bullets=[], kind="alertblock").kind == "block"

# Consumo de tokens: se acumula por modelo y aparece en el informe
class _Msg:
    usage_metadata = {"input_tokens": 1200, "output_tokens": 300}
bg.USO.clear()
bg.registrar_uso("m", _Msg()); bg.registrar_uso("m", _Msg())
assert bg.USO["m"] == {"llamadas": 2, "entrada": 2400, "salida": 600}
assert "| **Total** | 2 | 2,400 | 600 | 3,000 |" in bg.informe_uso()
print("OK")
