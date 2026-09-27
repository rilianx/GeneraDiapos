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

calls = {"write": {}, "refine": 0, "review": 0}


def fake_structured(model, schema, prompt, effort=None):
    if schema is bg.Review:                            # revisor: marca la cifra 99.9, que no está en la fuente
        calls["review"] += 1
        bad = "99.9" in prompt.split("CONTEXTO:")[0]
        return bg.Review(claims=[bg.Claim(afirmacion="99.9%", veredicto="Contradicha" if bad else "respaldada",
                                          evidencia="la fuente no da ese porcentaje")])
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
assert bg.restaurar_escapes("sistem\x00e1ticamente") == "sistemáticamente"
assert bg.restaurar_escapes(r"\\texttt{a} y \\\\ b") == r"\texttt{a} y \\\\ b"   # solo antes de letras

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
assert calls["review"] > 0, "el revisor debía ejecutarse en las diapos que compilan"
# Tipos de diapo: el guion debe respetar fuentes y variedad; el frame, su formato
spec = lambda k, src: bg.SlideSpec(title="T", bullets=["x"], kind=k, sources=src)
chunks = {"sec1": "", "tab1": "", "eq1": ""}
o = bg.Outline(title="", authors="", venue="", notation="", slides=[
    bg.SlideSpec(title="P", bullets=[], kind="title"), spec("bullets", ["sec1", "tab1"]),
    spec("bullets", ["sec1"]), spec("bullets", ["sec1"]), spec("block", ["sec1"])])
o = bg.ajustar_kinds(o)
assert o.slides[1].kind == "table"                       # viñetas que citan tab1 → table
assert not bg.validate_outline(o, chunks)
assert "solo viñetas" in bg.variedad_outline(o, bg.STYLE_DEFAULTS)[0]
assert bg.kind_check(r"\begin{frame}{T}\begin{itemize}\item a\end{itemize}\end{frame}", "table", ["tab1"])
assert not bg.kind_check(r"\begin{frame}{T}\begin{tabular}{l}a\end{tabular}\end{frame}", "table", ["tab1"])
assert bg.kind_check(r"\begin{frame}{T}$x$\end{frame}", "block", ["eq1"]) == [
    "Formato (block): usa al menos un block, alertblock o exampleblock"]
assert bg.SlideSpec(title="T", bullets=[], kind="alertblock").kind == "block"

# Tablas en markdown: título pegado, numeración del paper, trozos unidos, rotas marcadas
MD = """# Resultados
Texto.

|a|1<br>2|3<br>4|
|---|---|---|
|b|5<br>6|7<br>8<br>9|

123

J Glob Optim

|c|9|10|
|---|---|---|

**Table 2** Tiempos por variante.

|Método|t|
|---|---|
|A|1.5|
"""
c = bg.chunk_document(MD, "md")
assert sorted(k for k in c if k.startswith("tab")) == ["tab1", "tab2"], list(c)
assert bg.TABLA_DANADA in c["tab1"] and "|c|" not in c["tab1"]      # trozos unidos, rota: sin contenido
assert c["tab2"].startswith("Table 2: Tiempos por variante.") and bg.TABLA_DANADA not in c["tab2"]
o = bg.ajustar_kinds(bg.Outline(title="", authors="", venue="", notation="", slides=[
    bg.SlideSpec(title="T", bullets=[], kind="bullets", sources=["tab1"])]), c)
assert o.slides[0].kind == "bullets"                     # una tabla dañada no fuerza el tipo table

# Filas copiadas y exceso de bloques
dup = r"\begin{tabular}{lc} A & 20429 & 1.0 \\ B & 20429 & 1.0 \\ C & 19181 & 0.6 \\ \end{tabular}"
assert len(bg.filas_repetidas(dup)) == 1
assert "no tiene datos" in bg.filas_repetidas(r"\begin{tabular}{@{}lc@{}} X & -- & -- \\ \end{tabular}")[0]
blk = r"\begin{frame}{T}" + r"\begin{block}{x}y\end{block}" * 3 + r"\end{frame}"
assert any("bloques" in e for e in bg.style_check(blk, bg.STYLE_DEFAULTS))

# Si un refinado rompe la compilación, se conserva la última versión que compilaba
ok_frame = r"\begin{frame}{Ok}\begin{block}{a}b\end{block}\end{frame}"
st = {"idx": 1, "frame": "roto", "best_frame": ok_frame, "errors": ["Error X"], "style_errors": [],
      "fact_errors": [], "spec": {"kind": "block", "sources": ["sec1"], "title": "Ok", "bullets": []},
      "limits": bg.STYLE_DEFAULTS, "context": "", "attempts": 3}
_, frame, status, _, warns = bg.finish_slide(st)["frames"][0]
assert status == "ok" and frame == ok_frame and "última versión que compilaba" in warns[0]

# Guion revisado por una persona: se carga desde JSON sin llamar al modelo
g = tmp / "guion.json"
g.write_text(json.dumps({"paper": "paper.tex", **res["outline"]}))
n_calls = calls["review"]
st = {"chunks": res["chunks"], "outline_path": str(g)}
assert bg.outline(st)["outline"]["slides"] == res["outline"]["slides"]
md = bg.guion_md(res["outline"], res["chunks"], "paper.tex")
assert "Resultados" in md and "`bullets`" in md
g.write_text(json.dumps({**res["outline"], "slides": [{"title": "x", "bullets": [], "kind": "bullets",
                                                       "sources": ["nope"]}]}))
try:
    bg.outline(st)
    raise AssertionError("un guion con fuentes inexistentes debía rechazarse")
except RuntimeError:
    pass

# El índice de tablas llega al revisor con título y encabezados
idx = bg.indice_tablas(c)
assert "[tab2] Table 2: Tiempos por variante." in idx and "|Método|t|" in idx and "dañada" in idx

# Consumo de tokens: se acumula por modelo y aparece en el informe
class _Msg:
    usage_metadata = {"input_tokens": 1200, "output_tokens": 300}
bg.USO.clear()
bg.registrar_uso("m", _Msg()); bg.registrar_uso("m", _Msg())
assert bg.USO["m"] == {"llamadas": 2, "entrada": 2400, "salida": 600}
assert "| **Total** | 2 | 2,400 | 600 | 3,000 |" in bg.informe_uso()
print("OK")
