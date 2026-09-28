"""Prueba sin API: simula el LLM para verificar el grafo, la compilación
por diapositiva, los ciclos de refinado y la pausa de revisión."""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
REGISTRO = Path(tempfile.mkdtemp()) / "errores.jsonl"      # no ensuciar el registro real
os.environ["BEAMER_ERRORES"] = str(REGISTRO)

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
    if schema is bg.Outline:
        calls["outline_prompt"] = prompt
    if schema is bg.Lectura:                           # modo claude: inventario de lo leído
        calls["lectura"] = calls.get("lectura", 0) + 1
        if calls["lectura"] == 1:                      # 1er intento: una tabla que no existe
            return bg.Lectura(tablas=[bg.TablaLeida(numero=7, pagina=1, titulo="t", encabezados=["x"],
                                                    metodos=["y"], que_mide="z")],
                              algoritmos=[], ecuaciones=[], secciones=[])
        return bg.Lectura(tablas=[], algoritmos=[], secciones=[], ecuaciones=[
            bg.ElementoLeido(id="eq1", pagina=1, titulo="Lagrangiano",
                             latex=r"L(x,\lambda)=f(x)+\sum_j \lambda_j g_j(x)")])
    if schema is bg.Notas:                             # 1er intento: falta una y otra trae LaTeX
        calls["notas_prompt"] = prompt
        calls["notas"] = calls.get("notas", 0) + 1
        if calls["notas"] == 1:
            return bg.Notas(notas=[bg.NotaDiapo(diapo=0, texto="Hoy presentamos $x$."),
                                   bg.NotaDiapo(diapo=2, texto="El método.")])
        return bg.Notas(notas=[
            bg.NotaDiapo(diapo=0, texto="Hoy presentamos un método de optimización."),
            bg.NotaDiapo(diapo=2, texto="El lagrangiano junta objetivo y restricciones: 100% de_ellas {x}."),
            bg.NotaDiapo(diapo=3, texto="Mejora 1.43 veces.\n\nEn 777 casos, lo que no dice el paper.")])
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
    calls["write"][title] = prompt
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
(tmp / "notas.txt").write_text("El autor quiere destacar que lsmear no requiere modificar el contractor.")
res = graph.invoke({"source_path": str(tmp / "paper.tex"), "out_dir": str(out),
                    "human_review": True, "pptx": str(Path(bg.__file__).with_name("plantilla.pptx")),
                    "extras": [str(tmp / "notas.txt")], "notas_expositor": True,
                    "instrucciones": "Público: estudiantes de pregrado; 10 minutos."}, cfg)
assert "__interrupt__" in res, "debía pausar para revisión"
edited = res["__interrupt__"][0].value["outline"]
assert [s["kind"] for s in edited["slides"]][:2] == ["title", "agenda"]   # portada y agenda
edited["slides"][3]["title"] = "Resultados"
edited["slides"][3]["aviso"] = "la ganancia sale del texto, no de una tabla"
res = graph.invoke(Command(resume=edited), cfg)
# Cada diapo recibe su sección, su aviso y el guion completo con ella marcada
p_met, p_res = calls["write"]["Método"], calls["write"]["Resultados"]
assert "esta es la diapo 2 (sección: Propuesta)" in p_met and "  2. Método" in p_met and "  3. Resultados" in p_met
plan = lambda p: p.split("guion completo:\n\n")[1].split("\n\nTítulo:")[0]
assert plan(p_met) == plan(p_res) and "[Experimentos]" in plan(p_met) and "Portada" not in plan(p_met)
# lo que es solo del guion (reparto entre diapos, % de viñetas) no se repite en cada diapo
assert "Un recorrido típico" not in p_met and "solo con viñetas" not in p_met
assert "Un recorrido típico" in bg.guia_guion(None)
assert "solo con viñetas" in bg.describe_limits(bg.STYLE_DEFAULTS, guion=True)
assert "Aviso del guion" in p_res and "sale del texto" in p_res and "Aviso del guion" not in p_met

print("chunks:", list(res["chunks"]))
print("refinados:", calls["refine"])
print((out / "informe.md").read_text())
assert (out / "presentacion.pdf").exists()
assert not res["log_errors"]
tex = (out / "presentacion.tex").read_text()
assert "\\tableofcontents" in tex and tex.index("\\section{Propuesta}") < tex.index("\\section{Experimentos}")
assert calls["review"] > 0, "el revisor debía ejecutarse en las diapos que compilan"
# Fuentes adicionales e instrucciones: llegan al guion y a cada diapo
assert "x1sec1" in res["chunks"] and "[Fuente adicional: notas.txt]" in res["chunks"]["x1sec1"]
assert "no requiere modificar el contractor" in calls["outline_prompt"]
assert "estudiantes de pregrado" in calls["outline_prompt"] and "estudiantes de pregrado" in calls["write"]["Método"]
assert "Elige tú las secciones" in calls["outline_prompt"]       # secciones libres por defecto
# PowerPoint: misma cantidad de diapos, la ecuación como OMML editable y un fallback con imagen
import zipfile
pptx_f = out / "presentacion.pptx"
assert pptx_f.exists(), (out / "informe.md").read_text()
with zipfile.ZipFile(pptx_f) as z:
    diapos = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    xml = "".join(z.read(n).decode() for n in diapos)
n_frames = len(res["outline"]["slides"])
assert len(diapos) == n_frames, (len(diapos), n_frames)
assert "<a14:m>" in xml and "oMath" in xml and "mc:Fallback" in xml and "<p:pic>" in xml
assert "Método" in xml and "PowerPoint: `presentacion.pptx`" in (out / "informe.md").read_text()
# --a-pptx: la presentación ya generada (o editada a mano) pasa a PowerPoint sin el pipeline
apx = bg.tex_a_pptx(out / "presentacion.tex")
with zipfile.ZipFile(apx) as z:
    assert len([n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]) == n_frames
    notas_apx = "".join(z.read(n).decode() for n in z.namelist() if n.startswith("ppt/notesSlides/notesSlide"))
# Notas del expositor: se reintenta si falta una o trae LaTeX; salen en notas.md, \note{} y el .pptx
assert calls["notas"] == 2 and "falta la nota de la diapositiva [3]" in calls["notas_prompt"]
assert "trae LaTeX ($x$)" in calls["notas_prompt"] and "[1]" not in calls["notas_prompt"]   # sin agenda
assert "Portada: Demo (A. Autor; Revista)" in calls["notas_prompt"] and "Unas 80 a 130" in calls["notas_prompt"]
assert "estudiantes de pregrado" in calls["notas_prompt"]
nmd = (out / "notas.md").read_text()
assert "## 0. Portada" in nmd and "## 3. Resultados" in nmd and "1.43 veces" in nmd
assert r"\note{El lagrangiano junta objetivo y restricciones: 100\% de\_ellas \{x\}.}" in tex
errs_n, _ = bg.compile_tex(tex, passes=2, ignore_vbox=True, figuras=res.get("figuras_dir"))     # con las notas sigue compilando
assert not errs_n, errs_n
inf = (out / "informe.md").read_text()
assert "Notas del expositor: `notas.md` (3 diapositivas" in inf and "Nota de la diapo 3: Cifra 777" in inf
with zipfile.ZipFile(pptx_f) as z:
    notas_px = "".join(z.read(n).decode() for n in z.namelist() if n.startswith("ppt/notesSlides/notesSlide"))
for t in ("Hoy presentamos un método", "100% de_ellas {x}", "En 777 casos"):
    assert t in notas_px and t in notas_apx, t
assert bg.separar_nota(r"\begin{frame}{T}a\note{b \{c\} 5\%}\end{frame}") == \
    ("\\begin{frame}{T}a\n\\end{frame}", "b {c} 5%")
assert bg.tex_con_notas("x\\end{frame}", [0, 1], {"0": "n"}) == "x\\end{frame}"   # no calza: sin notas
import driver_claude
assert driver_claude.etiqueta({"esquema": "Notas", "prompt": ""}) == "notas-expositor"
# Registro de errores de los validadores: \noexiste (compilación) queda anotado y en el informe
cats = [r["categoria"] for r in bg.leer_errores()]
assert "Error: Undefined control sequence (\\noexiste)" in cats, cats
assert "Errores que detectaron los validadores" in (out / "informe.md").read_text()
assert bg.errores_previos() == ""                       # visto una sola vez: aún no se avisa
bg.registrar_errores("compilación", "equation", ["Error: Undefined control sequence. contexto: \\noexiste"])
assert "Undefined control sequence (\\noexiste) (2 veces)" in bg.errores_previos()
res_err = bg.resumen_errores()                          # --resumen-errores
assert "| Veces | Categoría |" in res_err and "Undefined control sequence (\\noexiste)" in res_err
assert "Sin errores registrados" in bg.resumen_errores(str(tmp / "no_existe.jsonl"))
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

# Diagramas: el DOT se dibuja con Graphviz (PDF para Beamer, PNG para PowerPoint) y se valida
if bg.hay_graphviz():
    figd = tmp / "diag" / "figuras"
    fr_d = (r"\begin{frame}{Flujo}" "\n" r"\begin{diagrama}" "\n"
            'digraph { rankdir=LR; a [label="Leer"]; b [label="Validar"]; a -> b; }' "\n"
            r"\end{diagrama}" "\n" r"\end{frame}")
    tex_d, e_d, a_d = bg.expandir_diagramas(fr_d, figd)
    assert not e_d and not a_d and "\\includegraphics" in tex_d and "diagrama" not in tex_d
    assert list(figd.glob("diag_*.pdf")) and list(figd.glob("diag_*.png"))
    assert not bg.kind_check(fr_d, "diagram", ["sec1"]) and bg.kind_check(r"\begin{frame}{X}x\end{frame}", "diagram", ["sec1"])
    assert not bg.lint_frame(bg.sin_diagramas(fr_d))
    _, e_mal, _ = bg.expandir_diagramas(fr_d.replace("a -> b;", "a -> ;"), figd)
    assert e_mal and "DOT no es válido" in e_mal[0]
    grande = "digraph{" + ";".join(f"n{i}->n{i+1}" for i in range(20)) + "}"
    _, _, a_g = bg.expandir_diagramas(r"\begin{diagrama}" + grande + r"\end{diagrama}", figd)
    assert any("nodos" in x for x in a_g)
    assert "diagram" in bg.kinds_disponibles()
# Navegadores de snap no sirven para mermaid-cli (no leen /usr/local/lib): se descartan
(tmp / "snapnav").write_text('#!/bin/sh\nexec /snap/bin/chromium "$@"\n')
(tmp / "nav").write_text('#!/bin/sh\nexec /opt/google/chrome/chrome "$@"\n')
assert bg.es_snap(str(tmp / "snapnav")) and not bg.es_snap(str(tmp / "nav"))
if bg.hay_mermaid():                                   # Mermaid: se detecta por el código
    fr_m = (r"\begin{frame}{Mensajes}" "\n" r"\begin{diagrama}" "\n"
            "sequenceDiagram\n  A->>B: pide\n  B-->>A: responde\n" r"\end{diagrama}" "\n" r"\end{frame}")
    tex_m, e_m, _ = bg.expandir_diagramas(fr_m, tmp / "diagm" / "figuras")
    assert not e_m and "diag_" in tex_m and ".png" in tex_m, e_m
    assert bg.motor_diagrama("sequenceDiagram\n A->>B: x") == "mermaid"
    assert bg.motor_diagrama("digraph { a -> b }") == "graphviz"
    _, e_m2, _ = bg.expandir_diagramas(r"\begin{diagrama}flowchart LR" "\n a --> \n" r"\end{diagrama}", tmp / "diagm")
    assert e_m2 and "Mermaid no es válido" in e_m2[0]
    assert "Mermaid" in bg.regla_diagramas() and "flowchart" in bg.ejemplo_kind("diagram")

# Herramientas MCP (si está el paquete): validar_frame dice lo mismo que el pipeline
try:
    import mcp_servidor
except ImportError:
    mcp_servidor = None
if mcp_servidor:
    n_log = len(bg.leer_errores())
    ok_ = mcp_servidor.validar_frame(r"\begin{frame}{Corto}\begin{itemize}\item a\item b\end{itemize}\end{frame}", "bullets")
    assert json.loads(ok_[0])["compila"] and len(ok_) == 2            # diagnóstico + imagen
    mal_ = json.loads(mcp_servidor.validar_frame(r"\begin{frame}{X}$\noexiste$\end{frame}", "bullets")[0])
    assert not mal_["compila"] and "noexiste" in mal_["errores"][0]
    assert len(bg.leer_errores()) == n_log                               # solo lectura: no anota en el registro
    assert json.loads(mcp_servidor.dibujar_diagrama("digraph{a->}")[0])["errores"]

# Motores que no funcionan (caso real: mermaid-cli mal instalado y sin Graphviz): no se ofrecen,
# y una diapo diagram del guion pasa a block con aviso, en vez de improvisar con TikZ
falso = tmp / "bin_falso"
falso.mkdir()
(falso / "mmdc").write_text("#!/bin/sh\necho 'Error: net::ERR_FILE_NOT_FOUND at file:///x/dist/index.html' >&2\nexit 1\n")
(falso / "mmdc").chmod(0o755)
path0, env0 = os.environ["PATH"], os.environ.get("BEAMER_DIAGRAMAS")
os.environ["PATH"] = f"{falso}:{path0}"
os.environ["BEAMER_DIAGRAMAS"] = "mermaid"          # y sin Graphviz
bg._MOTORES.clear()
assert not bg.hay_mermaid() and "ERR_FILE_NOT_FOUND" in bg.probar_motor("mermaid")[1]
assert "diagram" not in bg.kinds_disponibles()
d_ = bg.sin_motor_a_bloque({"kind": "diagram", "title": "T", "aviso": ""})
assert d_["kind"] == "block" and "no hay motor" in d_["aviso"]
assert "no escribas" in bg.regla_diagramas()             # el prompt no pide DOT ni Mermaid
_, e_sm, _ = bg.expandir_diagramas(r"\begin{diagrama}digraph { a -> b }\end{diagrama}", tmp / "sin_motor")
assert e_sm and "no hay motor" in e_sm[0]                 # aunque exista dot, si está desactivado
os.environ["PATH"] = path0
if env0 is None:
    del os.environ["BEAMER_DIAGRAMAS"]
else:
    os.environ["BEAMER_DIAGRAMAS"] = env0
bg._MOTORES.clear()

# Secciones libres: toda diapo con sección y cada sección con diapos seguidas; fijas: las de estilo.toml
libre = dict(bg.STYLE_DEFAULTS)
sec = lambda n: bg.SlideSpec(title="T", bullets=["x"], kind="block", sources=["sec1"], section=n)
o3 = bg.asegurar_portada(bg.Outline(title="", authors="", venue="", notation="",
                                    slides=[sec("Motivación"), sec("Método"), sec("Motivación")]), libre)
assert any("partidas" in e for e in bg.validate_outline(o3, chunks, libre))
o3.slides[-1].section = "Método"
assert not bg.validate_outline(o3, chunks, libre)
fijas = {**libre, "secciones": ["Introducción", "Propuesta"]}
assert any("sin sección válida" in e for e in bg.validate_outline(o3, chunks, fijas))
assert "Una de: Introducción, Propuesta" in bg.regla_secciones(fijas)

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
rounds, comunes, editado_mal = 0, 0, None
import contextlib, io
while True:
    salida = io.StringIO()
    with contextlib.redirect_stdout(salida):
        code = driver_claude.run(cli)
    print(salida.getvalue(), end="")
    if "no valida" in salida.getvalue():          # guion.md inválido: el mensaje es para la persona
        assert "Es su archivo" in salida.getvalue() and "RESPUESTA INVÁLIDA" not in salida.getvalue()
        guion_invalido_visto = True
    if code != 3:
        break
    rounds += 1
    assert rounds < 20, "el modo claude no terminó"
    for t in json.loads((work / "pendientes.json").read_text()):
        tarea = Path(t["tarea"]).read_text()
        comunes += tarea.count("[bloque común")
        prompt = driver_claude.expandir(tarea, work / driver_claude.COMUN)
        if t["esquema"] == "Review":                  # por defecto se revisa Claude mismo
            assert "PARA QUIEN COORDINA" not in prompt and "como si la hubiera escrito otra persona" in prompt
        prompt = prompt.split("---\n\n", 1)[1].split("\n\n---\n")[0]
        if t["tipo"] == "guion":                      # la persona edita guion.md y aprueba
            gmd = work / "guion.md"
            assert bg.MARCA_CRUDO in gmd.read_text() and "$x$" in gmd.read_text()   # fórmulas $…$
            if not editado_mal:                       # 1º: la persona se equivoca de fuente
                editado_mal = gmd.read_text()
                gmd.write_text(re.sub(r"Fuentes: sec3", "Fuentes: sec99", editado_mal, count=1))
            else:                                     # 2º: lo corrige
                gmd.write_text(editado_mal.replace("- Ganancia", "- Ganancia editada a mano", 1))
            ans = "aprobado"
        elif t["tipo"] == "json":
            ans = fake_structured("claude", getattr(bg, t["esquema"]), prompt).model_dump_json()
        else:
            ans = fake_text("claude", prompt)
        Path(t["respuesta"]).write_text(ans)
assert code == 0 and (tmp / "out_claude" / "presentacion.pdf").exists()
assert (work / "guion.md").exists() and rounds >= 4      # lectura, guion, revisión, diapos...
# validar_lectura rechazó el 1er inventario: el nodo repite la pregunta (mismo id de
# interrupt) y el driver debe reanudar bien, no declarar «Listo» sin hacer nada
assert calls["lectura"] == 2
assert comunes > 0, "las reglas repetidas entre tareas debían ir a comun.md"
assert "Errores que los validadores detectaron" in calls["write"]["Método"], \
    "los errores frecuentes de ejecuciones anteriores debían avisarse en el prompt"
assert guion_invalido_visto, "un guion.md mal editado debía rechazarse con un mensaje para la persona"
inf = (tmp / "out_claude" / "informe.md").read_text()      # tiempos por ronda en el informe
assert "## Tiempos (modo Claude)" in inf and "revisión humana del guion" in inf and "1 lectura" in inf
assert "Ganancia editada a mano" in json.dumps(graph_c := driver_claude.bg.build_graph(
    __import__("langgraph.checkpoint.sqlite", fromlist=["SqliteSaver"]).SqliteSaver(
        __import__("sqlite3").connect(work / "estado.sqlite", check_same_thread=False))).get_state(
    {"configurable": {"thread_id": "beamer"}}).values["outline"], ensure_ascii=False), \
    "la edición de guion.md debía llegar al guion aprobado"
# LLM_REVISOR=subagente: la revisión pide un solo subagente independiente para la ronda
import os
os.environ["LLM_REVISOR"] = "subagente"
assert "UN solo subagente" in driver_claude.revision_independiente() and driver_claude.revisor() == "subagente"
os.environ["LLM_REVISOR"] = "otro"
try:
    driver_claude.revisor()
    raise AssertionError("LLM_REVISOR inválido debía fallar")
except SystemExit:
    pass
del os.environ["LLM_REVISOR"]
# valor_resume: (a) diapos en paralelo: una sola tarea en la ronda pero el grafo lista varios
# interrupts de un mismo paso → por id; (b) un nodo que repite su pregunta → con el valor
import operator
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send, interrupt
from langgraph.checkpoint.memory import MemorySaver
class _S(TypedDict, total=False):
    out: Annotated[list, operator.add]
class _Sub(_S, total=False):
    k: int
def _a(s): return {"out": [interrupt("escribir")]}
def _b(s): return {"out": [interrupt("revisar")]} if s["k"] == 0 else {}
_sg = StateGraph(_Sub); _sg.add_node("a", _a); _sg.add_node("b", _b)
_sg.add_edge(START, "a"); _sg.add_edge("a", "b"); _sg.add_edge("b", END)
_g = StateGraph(_S); _g.add_node("slide", _sg.compile())
_g.add_conditional_edges(START, lambda s: [Send("slide", {"k": i}) for i in range(3)], ["slide"])
_g.add_edge("slide", END)
def _ronda(G, c, r):
    ans = {i.id: "ok" for i in r["__interrupt__"]}
    return G.invoke(Command(resume=driver_claude.valor_resume(ans, G.get_state(c))), c)
G, c = _g.compile(checkpointer=MemorySaver()), {"configurable": {"thread_id": "p"}}
r = _ronda(G, c, G.invoke({}, c))
assert len(r["__interrupt__"]) == 1 and len(G.get_state(c).interrupts) > 1   # el caso real
r = _ronda(G, c, r)
assert not r.get("__interrupt__") and len(r["out"]) == 4 and not G.get_state(c).next
def _reintenta(s):                                   # como lectura: pregunta hasta que valida
    while (v := interrupt("inventario")) != "bueno":
        pass
    return {"out": [v]}
_h = StateGraph(_S); _h.add_node("n", _reintenta); _h.add_edge(START, "n"); _h.add_edge("n", END)
H, c = _h.compile(checkpointer=MemorySaver()), {"configurable": {"thread_id": "r"}}
r = H.invoke({}, c)
for v in ("malo", "bueno"):
    r = H.invoke(Command(resume=driver_claude.valor_resume({r["__interrupt__"][0].id: v}, H.get_state(c))), c)
assert r["out"] == ["bueno"] and not H.get_state(c).next
# compactar: lo repetido va una vez al archivo común y expandir lo devuelve intacto
largo = "Reglas:\n" + "- regla fija\n" * 20
cs, com = driver_claude.compactar([f"A\n\n{largo}\n\nTítulo: 1", f"B\n\n{largo}\n\nTítulo: 2", "C corta"])
assert len(com) == 1 and largo not in cs[0] and cs[2] == "C corta"
(tmp / "c.md").write_text("# x\n\n" + "\n\n".join(f"## {k}\n\n{v}" for k, v in com.items()) + "\n")
assert driver_claude.expandir(cs[1], tmp / "c.md") == f"B\n\n{largo}\n\nTítulo: 2"
# celdas con «--» en una tabla: no se aceptan
assert bg.filas_repetidas(r"\begin{tabular}{lrr} A & 1 & 2 \\ B & 3 & -- \\ \end{tabular}")
# el informe usa el título final de la diapo (un refinado pudo acortarlo), con llaves anidadas
assert bg.titulo_frame(r"\begin{frame}[fragile]{\texttt{lsmear} gana}\n x \end{frame}") == r"\texttt{lsmear} gana"
assert bg.titulo_frame(bg.TITLE_FRAME) == ""
# índice de tablas con el inventario de la lectura: sin «encabezados: Encabezados:»
idx = bg.indice_tablas({"tab1": "Table 1 (pág. 3): t\nEncabezados: a | b\nCompara: x, y\nMide: z"})
assert "encabezados: Encabezados" not in idx and "Compara: x, y" in idx
# pies de figura: sin el rótulo repetido y cortados en una frase
pie = bg.pie_corto("Fig. 2 Uno dos. " + "palabra " * 60)
assert pie.startswith("Uno dos.") and pie.endswith("palabra…") and len(pie) <= 301
larga = "Fig. 3 " + "Una frase bastante larga que explica la figura. " * 8
assert bg.pie_corto(larga).endswith("figura.") and len(bg.pie_corto(larga)) <= 300
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
# Empezar de nuevo (con paper, sin --reanudar): se borra solo lo generado, no archivos propios
(tmp / "out_claude" / "mio.txt").write_text("no tocar")
cli.reanudar = True
bg.empezar_de_nuevo(cli)
assert (work / "estado.sqlite").exists() and (tmp / "out_claude" / "presentacion.pdf").exists()
cli.reanudar = False
bg.empezar_de_nuevo(cli)
assert not (work / "estado.sqlite").exists() and not (work / "tareas").exists()
assert not (tmp / "out_claude" / "presentacion.pdf").exists() and not (tmp / "out_claude" / "figuras").exists()
assert (tmp / "out_claude" / "mio.txt").read_text() == "no tocar"
bg.PROVIDER = "claude"
assert driver_claude.run(cli) == 3 and "lectura" in (work / "pendientes.json").read_text()  # corrida nueva
bg.PROVIDER = "openai"

# Guion como documento editable: ida y vuelta a través de un editor de markdown simulado
# (según lo observado en Claude Docs: une líneas contiguas, interpreta escapes, y al exportar
# escapa \ _ * fuera del código en línea)
def editor(md):
    out = []
    for par in md.split("\n\n"):
        lines = par.strip("\n").split("\n")
        if not all(l.startswith("- ") for l in lines) and not lines[0].startswith("#"):
            lines = [" ".join(lines)]
        out.append("\n".join(lines))
    return "\n\n".join(out)
def interpretar(md):
    return re.sub(r"(`[^`]*`)|\\([\\_*&#\[\]()~>|!+.\-{}])", lambda m: m.group(1) or m.group(2), md)
def exportar(txt):
    return re.sub(r"(`[^`]*`)|([\\_*])", lambda m: m.group(1) or "\\" + m.group(2), txt)
import re
dificil = {"title": r"\texttt{lsmear}: B\&B por intervalos", "authors": r"A \and B", "venue": "Rev", "notation": r"$x_i$: variable",
           "slides": [{"title": r"Cuatro variantes de $\lambda^\star$", "bullets": [r"D suma lambda*_{n+j} J_ji", r"\texttt{rr} y B\&B"],
                       "kind": "table", "sources": ["sec1"], "section": "Propuesta", "aviso": "¿tab1 o tab2?"}]}
for o_ in (dificil, {**res["outline"], "slides": [x for x in res["outline"]["slides"] if x["kind"] not in bg.FIXED_KINDS]}):
    back = bg.guion_desde_md(exportar(interpretar(editor(bg.guion_doc_md(o_, res["chunks"])))))
    for k in ("title", "authors", "venue", "notation"):
        assert back[k] == o_[k], (k, back[k], o_[k])
    for a, b in zip(o_["slides"], back["slides"]):
        for k in ("title", "bullets", "kind", "sources", "section", "aviso"):
            assert a.get(k, "") == b[k], (k, a.get(k), b[k])
    assert len(back["slides"]) == len(o_["slides"])
    crudo = bg.guion_doc_md(o_, res["chunks"], crudo=True)       # archivo: LaTeX tal cual
    assert "\\\\texttt" not in crudo and "`$" not in crudo
    assert o_ is not dificil or "\\texttt{lsmear}: B\\&B" in crudo
    back = bg.guion_desde_md(crudo)
    assert all(back[k] == o_[k] for k in ("title", "authors", "venue", "notation")), back
    for a, b in zip(o_["slides"], back["slides"]):
        for k in ("title", "bullets", "kind", "sources", "section", "aviso"):
            assert a.get(k, "") == b[k], ("crudo", k, a.get(k), b[k])

# Figuras: se recortan del PDF (el pie «Fig. N» sí, la mención «Figure N shows» no) y una
# diapo figure compila con \includegraphics{figuras/figN.png}
import pymupdf
pdf = tmp / "con_figura.pdf"
with pymupdf.open() as doc:
    page = doc.new_page(width=440, height=666)
    page.insert_text((50, 90), "Figure 1 shows the results of the new method on all instances.", fontsize=9)
    page.draw_rect(pymupdf.Rect(80, 120, 360, 320), color=(0, 0, 1), fill=(0.8, 0.8, 1))
    page.insert_text((80, 340), "Fig. 1 Resultado del metodo nuevo", fontsize=9)
    doc.save(pdf)
figs = bg.extraer_figuras(pdf, tmp / "figuras")
assert list(figs) == [1] and (tmp / "figuras" / "fig1.png").exists(), figs
assert figs[1]["caption"].startswith("Fig. 1 Resultado") and figs[1]["archivo"] == "figuras/fig1.png"
fig_frame = bg.KINDS["figure"][1].replace("fig2", "fig1")
assert not bg.lint_frame(fig_frame) and not bg.kind_check(fig_frame, "figure", ["fig1"])
assert bg.kind_check(r"\begin{frame}{T}x\end{frame}", "figure", ["fig1"])        # cita fig1 sin mostrarla
assert bg.lint_frame(r"\begin{frame}{T}\includegraphics{/etc/passwd}\end{frame}")  # solo figuras/figN.png
head = bg.split_base(bg.DEFAULT_BASE.read_text(), None)[0]
tex, off = bg.standalone(head, fig_frame)
errs, _ = bg.compile_tex(tex, offset=off, figuras=str(tmp / "figuras"))
assert not errs, errs

# Consumo de tokens: se acumula por modelo y aparece en el informe
class _Msg:
    usage_metadata = {"input_tokens": 1200, "output_tokens": 300}
bg.USO.clear()
bg.registrar_uso("m", _Msg()); bg.registrar_uso("m", _Msg())
assert bg.USO["m"] == {"llamadas": 2, "entrada": 2400, "salida": 600}
assert "| **Total** | 2 | 2,400 | 600 | 3,000 |" in bg.informe_uso()
print("OK")
