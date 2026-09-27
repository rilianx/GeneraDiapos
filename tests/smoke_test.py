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
    if schema is bg.Lectura:                           # modo claude: inventario de lo leído
        calls["lectura"] = calls.get("lectura", 0) + 1
        return bg.Lectura(tablas=[], algoritmos=[], secciones=[], ecuaciones=[
            bg.ElementoLeido(id="eq1", pagina=1, titulo="Lagrangiano",
                             latex=r"L(x,\lambda)=f(x)+\sum_j \lambda_j g_j(x)")])
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
                         sources=["sec2", "eq1"], section="Propuesta"),
            bg.SlideSpec(title="Resultados", bullets=["Ganancia"], kind="bullets",
                         sources=["sec3"], section="Experimentos"),
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

real_structured, real_text = bg.call_structured, bg.call_text
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
assert [s["kind"] for s in edited["slides"]][:2] == ["title", "agenda"]   # portada y agenda
edited["slides"][3]["title"] = "Resultados"
res = graph.invoke(Command(resume=edited), cfg)

print("chunks:", list(res["chunks"]))
print("refinados:", calls["refine"])
print((out / "informe.md").read_text())
assert (out / "presentacion.pdf").exists()
assert not res["log_errors"]
tex = (out / "presentacion.tex").read_text()
assert "\\tableofcontents" in tex and tex.index("\\section{Propuesta}") < tex.index("\\section{Experimentos}")
assert calls["review"] > 0, "el revisor debía ejecutarse en las diapos que compilan"
# Tipos de diapo: el guion debe respetar fuentes y variedad; el frame, su formato
spec = lambda k, src: bg.SlideSpec(title="T", bullets=["x"], kind=k, sources=src, section="Propuesta")
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

# Portada y agenda se garantizan; toda diapo de contenido necesita una sección válida
o2 = bg.asegurar_portada(bg.Outline(title="Demo", authors="", venue="", notation="", slides=[
    spec("block", ["sec1"]), bg.SlideSpec(title="X", bullets=[], kind="block", sources=["sec1"])]),
    bg.STYLE_DEFAULTS)
assert [s.kind for s in o2.slides] == ["title", "agenda", "block", "block"]
assert any("sin sección válida" in e for e in bg.validate_outline(o2, chunks))

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

# Avisos del guion: duda declarada, tabla dañada y métodos que no están en la tabla citada
ol = {"slides": [
    {"title": "Clásicas", "bullets": [r"\texttt{rr} y \texttt{A}"], "kind": "table", "sources": ["tab2"],
     "aviso": ""},
    {"title": "Rotas", "bullets": ["x"], "kind": "table", "sources": ["tab1"], "aviso": "¿tab1 o tab2?"},
    {"title": "Bien", "bullets": [r"\texttt{A}"], "kind": "table", "sources": ["tab2"], "aviso": ""}]}
av = bg.avisos_guion(ol, c)
assert av[0][0].startswith("Menciona rr, que no aparecen en tab2")     # A sí está en tab2
assert any("¿tab1 o tab2?" in w for w in av[1]) and any("dañada" in w for w in av[1])
assert 2 not in av
assert "⚠ Revisar primero" in bg.guion_md({"title": "T", **ol}, c, "p.pdf")

# Modo Claude Code (LLM_PROVIDER=claude): el mismo grafo se pausa en cada llamada al
# modelo, deja tareas en archivos y se reanuda al volver a ejecutar el driver
import argparse
import driver_claude
bg.call_structured, bg.call_text = real_structured, real_text
work = tmp / "claude"
cli = argparse.Namespace(claude=str(work), source=str(tmp / "paper.tex"), out=str(tmp / "out_claude"),
                         base=str(bg.DEFAULT_BASE), estilo=str(bg.DEFAULT_STYLE), extractor="pymupdf",
                         review=True, guion=None, concurrency=2)
rounds = 0
while (code := driver_claude.run(cli)) == 3:
    rounds += 1
    assert rounds < 20, "el modo claude no terminó"
    for t in json.loads((work / "pendientes.json").read_text()):
        prompt = Path(t["tarea"]).read_text().split("---\n\n", 1)[1].split("\n\n---\n")[0]
        if t["tipo"] == "guion":
            ans = (work / "guion.json").read_text()
        elif t["tipo"] == "json":
            ans = fake_structured("claude", getattr(bg, t["esquema"]), prompt).model_dump_json()
        else:
            ans = fake_text("claude", prompt)
        Path(t["respuesta"]).write_text(ans)
assert code == 0 and (tmp / "out_claude" / "presentacion.pdf").exists()
assert (work / "guion.md").exists() and rounds >= 4      # lectura, guion, revisión, diapos...
assert calls["lectura"] == 1
tareas_diapo = "".join(p.read_text() for p in (work / "tareas").glob("*.md"))
# validar_lectura: lo que Claude dice haber visto tiene que existir en el texto del paper
txt = "Table 2 Average CPU time. lsmear lsmear-MG time #box gain"
ok = bg.Lectura(tablas=[bg.TablaLeida(numero=2, pagina=3, titulo="t", encabezados=["time", "#box"],
                                      metodos=["lsmear-MG"], que_mide="s")],
                algoritmos=[], ecuaciones=[], secciones=[])
assert not bg.validar_lectura(ok, txt, 5)
mal = ok.model_copy(deep=True)
mal.tablas[0].metodos = ["lsmear-XYZ"]
mal.tablas[0].pagina = 9
errs = bg.validar_lectura(mal, txt, 5)
assert any("fuera de" in e for e in errs) and any("xyz" in e for e in errs), errs
# una respuesta que no cumple el esquema no se acepta
bad = driver_claude.leer_respuestas([{"id": "x", "tarea": "t", "respuesta": str(tmp / "mal.json"),
                                      "tipo": "json", "esquema": "Review"}], {})
(tmp / "mal.json").write_text('{"otra": 1}')
_, _, errs = driver_claude.leer_respuestas([{"id": "x", "tarea": "t", "respuesta": str(tmp / "mal.json"),
                                             "tipo": "json", "esquema": "Review"}], {})
assert errs and bad[1] == ["t"]
bg.PROVIDER = "openai"

# Consumo de tokens: se acumula por modelo y aparece en el informe
class _Msg:
    usage_metadata = {"input_tokens": 1200, "output_tokens": 300}
bg.USO.clear()
bg.registrar_uso("m", _Msg()); bg.registrar_uso("m", _Msg())
assert bg.USO["m"] == {"llamadas": 2, "entrada": 2400, "salida": 600}
assert "| **Total** | 2 | 2,400 | 600 | 3,000 |" in bg.informe_uso()
print("OK")
