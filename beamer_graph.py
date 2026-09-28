"""
Paper -> presentación Beamer con LangGraph, validando cada diapositiva por separado.

Flujo:
    ingest -> outline -> review_outline -> [slide × N] -> assemble
           -> compile_full <-> refine_global -> write_outputs

Subgrafo `slide` (uno por diapositiva, en paralelo con Send):
    write_slide -> compile_slide <-> refine_slide -> finish_slide

Uso:
    python beamer_graph.py paper.pdf --out salida/ [--base base.tex] [--review] [--extractor marker]

La base (base.tex) es tuya: preámbulo, tema, macros y un marcador %%SLIDES%%.
El modelo solo escribe frames sueltos; nunca toca la base.
Las reglas de estilo (estilo.toml) tienen una guía en texto, que va al prompt, y
límites medibles, que se verifican en código y disparan refinados.
    python beamer_graph.py paper.tex --out salida/
"""
from __future__ import annotations

import argparse
import itertools
import os
import json
import operator
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import unicodedata
from pathlib import Path
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field, field_validator
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------
# Proveedor y modelos: se eligen con variables de entorno (o --proveedor en la CLI).
#   LLM_PROVIDER=openai | anthropic | claude
#     claude: sin API. Cada llamada al modelo pausa el grafo (interrupt) y deja una
#     tarea en archivo para que la responda Claude Code; ver driver_claude.py.
#   LLM_MODEL_OUTLINE / LLM_MODEL_SLIDES: nombres de modelo de tu cuenta
# El guion es la decisión más importante: usa ahí tu modelo más capaz, y uno más
# rápido/barato para las N diapositivas y los refinados.
PROVIDER = os.environ.get("LLM_PROVIDER") or "openai"
_DEFAULT_MODELS = {"anthropic": ("claude-opus-5-5", "claude-sonnet-5"),
                   "openai": ("gpt-5.4-mini", "gpt-5.4-mini"),
                   "claude": ("claude-code", "claude-code")}
MODEL_OUTLINE = os.environ.get("LLM_MODEL_OUTLINE") or _DEFAULT_MODELS[PROVIDER][0]
MODEL_SLIDES = os.environ.get("LLM_MODEL_SLIDES") or _DEFAULT_MODELS[PROVIDER][1]
# Esfuerzo de razonamiento (solo OpenAI): low | medium | high. Vacío = el del modelo.
# El guion es la decisión que más pesa; las diapos van sin razonamiento extra.
REASONING_OUTLINE = os.environ.get("LLM_REASONING_OUTLINE") or "medium"
REASONING_SLIDES = os.environ.get("LLM_REASONING_SLIDES") or None
# Revisor de afirmaciones por diapositiva (juez LLM contra los fragmentos fuente)
MODEL_REVIEW = os.environ.get("LLM_MODEL_REVIEW") or MODEL_SLIDES
REASONING_REVIEW = os.environ.get("LLM_REASONING_REVIEW") or "medium"
MAX_SLIDE_ATTEMPTS = 3      # refinados por compilación/estilo
MAX_REVIEWS = 2             # revisión inicial + una verificación tras corregir afirmaciones
MAX_GLOBAL_ATTEMPTS = 2
MAX_OUTLINE_ATTEMPTS = 3
OVERFULL_TOLERANCE_PT = 2.0
MAX_TEXT_CHARS = 900                # densidad máxima orientativa por diapo
LATEX_TIMEOUT_S = 60
N_SLIDES = (12, 16)

DEFAULT_BASE = Path(__file__).with_name("base.tex")
DEFAULT_STYLE = Path(__file__).with_name("estilo.toml")
# Registro persistente de lo que rechazan los validadores (compilación, lint, formato, estilo):
# los errores más frecuentes se avisan en el prompt de las diapos de las próximas ejecuciones.
# BEAMER_ERRORES=ruta para otro archivo; BEAMER_ERRORES=off para desactivarlo.
ERRORES_LOG = os.environ.get("BEAMER_ERRORES") or str(Path(__file__).with_name("errores_validacion.jsonl"))
MAX_ERRORES_PREVIOS = 6              # categorías que se avisan en el prompt
MIN_VECES_PREVIOS = 2                # veces que debe haberse visto una categoría para avisarla
STYLE_DEFAULTS = {
    "max_items": 6, "max_palabras_item": 18, "max_anidamiento": 2,
    "max_caracteres_titulo": 60, "max_alerts": 2, "max_negritas": 3,
    "sin_punto_final": False, "prohibir_colores_directos": True,
    "prohibir_vspace_negativo": True, "tamanos_permitidos": [r"\small"],
    "max_fraccion_vinetas": 0.4, "max_bloques": 2, "prohibir_alert_en_alertblock": True,
    # [estructura] de estilo.toml
    "secciones": [],                 # vacío: las decide el guion; una lista las fija (y su orden)
    "agenda": True,
}
SLIDES_MARK = "%%SLIDES%%"

# Tipos de diapositiva. La descripción va al guion; la plantilla (genérica, no de
# ningún paper) va al prompt de la diapo; el contrato se verifica en kind_check.
KINDS = {
    "bullets": (
        "lista breve; úsala solo cuando ningún otro formato sirva",
        r"""\begin{frame}{El método reduce el tiempo sin perder precisión}
\begin{itemize}
  \item Idea principal en una línea.
  \item Segunda idea, en paralelo gramatical.
  \item \alert{La consecuencia que importa.}
\end{itemize}
\end{frame}""", None),
    "columns": (
        "dos columnas: comparar dos enfoques, o texto a un lado y fórmula/tabla al otro",
        r"""\begin{frame}{Los enfoques clásicos ignoran la estructura}
\begin{columns}[T]
\column{0.48\textwidth}
\textbf{Enfoque A}
\begin{itemize}
  \item Simple y barato.
  \item No usa información del problema.
\end{itemize}
\column{0.48\textwidth}
\textbf{Enfoque B}
\[ s_i = |a_i|\cdot w_i \]
Mide el efecto estimado de cada componente.
\end{columns}
\end{frame}""", (r"\\begin\{columns\}", "usa un entorno columns con dos \\column")),
    "block": (
        "bloques de Beamer: definición, resultado clave, limitación o mensaje para recordar",
        r"""\begin{frame}{Promediar todo diluye la señal relevante}
\begin{block}{Definición}
Una restricción está \emph{activa} si se cumple con igualdad en el óptimo.
\end{block}
\begin{alertblock}{Problema}
Las estrategias actuales pesan igual las restricciones activas e inactivas.
\end{alertblock}
\begin{exampleblock}{Idea}
Ponderar cada restricción por su relevancia.
\end{exampleblock}
\end{frame}""", (r"\\begin\{(?:alert|example)?block\}", "usa al menos un block, alertblock o exampleblock")),
    "equation": (
        "una o dos ecuaciones centrales en display, cada una con una línea que explique qué significa",
        r"""\begin{frame}{El Lagrangiano combina objetivo y restricciones}
\[ L(x,\lambda) = f(x) + \sum_{j=1}^{m} \lambda_j\, g_j(x) \]
\begin{itemize}
  \item $\lambda_j \ge 0$ pondera la restricción $g_j$.
  \item \alert{$\lambda_j = 0$ si la restricción no influye en el óptimo.}
\end{itemize}
\end{frame}""", (r"\\\[|\\begin\{(?:equation|align|gather|multline)\*?\}",
                    "incluye al menos una ecuación en display (\\[ \\] o align)")),
    "table": (
        "tabla booktabs con solo las filas y columnas que sostienen el mensaje, y 2-3 líneas de lectura",
        r"""\begin{frame}{El método nuevo gana en todas las comparaciones}
\begin{center}
\begin{tabular}{@{}lcc@{}}
\toprule
Método & Tiempo (s) & Nodos \\ \midrule
Base      & 120 & 5400 \\
Nuevo     & \alert{64} & \alert{2900} \\
\bottomrule
\end{tabular}
\end{center}
\begin{itemize}
  \item Reduce el tiempo casi a la mitad.
\end{itemize}
\end{frame}""", (r"\\begin\{tabular", "incluye la tabla con un entorno tabular (booktabs)")),
    "figure": (
        "una figura del paper (fig*), ya recortada como imagen, a buen tamaño y con 1-2 líneas de lectura",
        r"""\begin{frame}{El método nuevo domina en casi todas las instancias}
\begin{center}
\includegraphics[width=0.85\textwidth,height=0.62\textheight,keepaspectratio]{figuras/fig2.png}
\end{center}
\small Cada punto es una instancia: bajo la diagonal, el método nuevo es más rápido.
\end{frame}""", (r"\\includegraphics", "incluye la figura con \\includegraphics{figuras/figN.png}")),
    "algorithm": (
        "pseudocódigo con algorithm2e, simplificado a los pasos esenciales",
        r"""\begin{frame}{El algoritmo tiene dos fases}
\begin{algorithm}[H]
\scriptsize
\KwIn{caja $x$}
\KwOut{índice $i^*$}
calcular pesos $w$\;
\For{$i \in 1..n$}{ $s_i \gets w_i \cdot d_i$\; }
\Return $\arg\max_i s_i$\;
\end{algorithm}
\end{frame}""", (r"\\begin\{algorithm\}", "incluye el pseudocódigo en un entorno algorithm (algorithm2e)")),
    "diagram": (
        "diagrama de un proceso, flujo, secuencia de mensajes o arquitectura; se escribe como código "
        "(Mermaid o DOT de Graphviz) y lo dibuja el pipeline, con 1-2 líneas de lectura",
        r"""\begin{frame}{Cada pedido pasa por tres etapas}
\begin{diagrama}
digraph {
  rankdir=LR;
  entrada [label="Pedido"];
  validar [label="Validar\ndatos"];
  procesar [label="Procesar"];
  salida [label="Respuesta"];
  entrada -> validar -> procesar -> salida;
  validar -> entrada [label="error", style=dashed];
}
\end{diagrama}
\small Un pedido inválido vuelve al inicio.
\end{frame}""", (r"\\begin\{diagrama\}", "dibuja el diagrama en DOT dentro de \\begin{diagrama} ... \\end{diagrama}")),
}

KIND_ALIASES = {"alertblock": "block", "exampleblock": "block", "blocks": "block",
                "two_columns": "columns", "column": "columns", "itemize": "bullets",
                "list": "bullets", "tabular": "table", "math": "equation", "pseudocode": "algorithm",
                "diagrama": "diagram", "graphviz": "diagram", "flowchart": "diagram"}

# Diapos que arma el código, sin LLM ni revisor
FIXED_KINDS = ("title", "agenda")
TITLE_FRAME = "\\begin{frame}[plain]\n\\titlepage\n\\end{frame}"
AGENDA_FRAME = "\\begin{frame}{Contenido}\n\\tableofcontents\n\\end{frame}"

# Una diapo de solo viñetas que cita una tabla o ecuación pasa a mostrarla.
# (Un alg* puede citarse como contexto sin mostrar el pseudocódigo.)
BULLETS_UPGRADE = {"tab": "table", "eq": "equation", "fig": "figure"}

# ----------------------------------------------------------------------------
# Diagramas (Graphviz): el modelo escribe DOT; el pipeline lo dibuja con estilo fijo
# ----------------------------------------------------------------------------
DIAGRAMA = re.compile(r"\\begin\{diagrama\}(?:\[([^\]]*)\])?(.*?)\\end\{diagrama\}", re.S)
DIAGRAMA_ESTILO = {
    "graph": 'fontname="Helvetica", bgcolor="transparent", pad="0.08", nodesep="0.35", ranksep="0.45"',
    "node": ('shape=box, style="rounded,filled", fillcolor="#DCE6F2", color="#4F81BD", penwidth=1.3, '
             'fontname="Helvetica", fontcolor="#002060", fontsize=14, margin="0.15,0.06"'),
    "edge": 'color="#1F497D", penwidth=1.2, arrowsize=0.8, fontname="Helvetica", fontsize=12, fontcolor="#1F497D"',
}
MAX_NODOS_DIAGRAMA = 14
MIN_LETRA_DIAGRAMA = 6.0          # pt que tendría la letra del nodo una vez ajustado a la diapo
AREA_DIAGRAMA_IN = (5.0, 1.9)     # ancho y alto útiles (pulgadas) en una diapo 16:9 de metropolis


_MOTORES: dict[str, tuple[bool, str]] = {}      # motor → (funciona, motivo); se prueba una vez


def probar_motor(motor: str) -> tuple[bool, str]:
    """Que el comando exista no basta (p. ej. un mermaid-cli mal instalado): se dibuja un
    diagrama mínimo una vez por proceso."""
    if motor in _MOTORES:
        return _MOTORES[motor]
    if motor == "graphviz":
        if not shutil.which("dot"):
            res = (False, "Graphviz (dot) no está instalado")
        else:
            r = subprocess.run(["dot", "-Tplain"], input="digraph { a -> b }", capture_output=True,
                               text=True, timeout=30)
            res = (r.returncode == 0, "" if r.returncode == 0 else f"dot falla: {r.stderr.strip()[:200]}")
    else:
        if not shutil.which("mmdc"):
            res = (False, "Mermaid (mmdc) no está instalado")
        else:
            with tempfile.TemporaryDirectory() as t:
                _MOTORES[motor] = (True, "")                  # para que render_mermaid lo intente
                ruta, err, _ = render_mermaid("flowchart LR\n  a --> b", Path(t) / "figuras", prueba=True)
                res = (bool(ruta), "" if ruta else (err[0] if err else "mermaid-cli no dibujó la prueba"))
    _MOTORES[motor] = res
    return res


def hay_graphviz() -> bool:
    return os.environ.get("BEAMER_DIAGRAMAS") != "mermaid" and probar_motor("graphviz")[0]


def hay_mermaid() -> bool:
    return os.environ.get("BEAMER_DIAGRAMAS") != "graphviz" and probar_motor("mermaid")[0]


def kinds_disponibles() -> dict:
    return {k: v for k, v in KINDS.items() if k != "diagram" or hay_graphviz() or hay_mermaid()}


# Mermaid (mermaid-cli, con Chromium sin interfaz): tema con los colores del pipeline
MERMAID_CONFIG = {
    "theme": "base",
    "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif", "fontSize": "16px",
                       "primaryColor": "#DCE6F2", "primaryBorderColor": "#4F81BD",
                       "primaryTextColor": "#002060", "lineColor": "#1F497D",
                       "secondaryColor": "#F2DCDB", "tertiaryColor": "#FFFFFF",
                       "edgeLabelBackground": "#FFFFFF", "clusterBkg": "#F4F7FB",
                       "clusterBorder": "#4F81BD", "actorBkg": "#DCE6F2", "actorBorder": "#4F81BD",
                       "noteBkgColor": "#F2DCDB"},
    "flowchart": {"curve": "basis", "htmlLabels": False},
    "sequence": {"mirrorActors": False, "actorMargin": 40, "messageMargin": 28},
}
MERMAID_ESCALA = 3                # el PNG sale a 3× (px de 1/96"): nítido en la diapo
ES_DOT = re.compile(r"^\s*(?:strict\s+)?(?:di)?graph\b[^\n{]*\{")


def motor_diagrama(codigo: str) -> str:
    """DOT si empieza como un grafo de Graphviz (digraph {…}); si no, Mermaid."""
    return "graphviz" if ES_DOT.match(codigo) else "mermaid"


def regla_diagramas() -> str:
    """Cómo escribir un diagrama según los motores instalados (va en SLIDE_PROMPT)."""
    base = ("dentro de \\begin{diagrama} … \\end{diagrama} (sin \\includegraphics ni TikZ); solo nodos, "
            "flechas y etiquetas breves, con el contenido del CONTEXTO. El estilo lo pone el pipeline: no uses "
            "colores ni fuentes. Pocos nodos (máximo 14).")
    if hay_mermaid():
        extra = (" Para un grafo general con muchas conexiones cruzadas puedes usar DOT de Graphviz "
                 "(digraph { … })." if hay_graphviz() else "")
        return ("escribe el diagrama en Mermaid (flowchart LR o TD, sequenceDiagram, stateDiagram-v2, "
                "classDiagram, timeline, mindmap) " + base + extra)
    if hay_graphviz():
        return "escribe el grafo en DOT de Graphviz (digraph { … }) " + base
    return ("no hay motor de diagramas en esta máquina: no escribas \\begin{diagrama} ni TikZ; muestra "
            "procesos y flujos como lista numerada, columnas o bloques.")


EJEMPLO_MERMAID = r"""\begin{frame}{Cada pedido pasa por tres etapas}
\begin{diagrama}
flowchart LR
  entrada["Pedido"] --> validar["Validar datos"] --> procesar["Procesar"] --> salida["Respuesta"]
  validar -. "error" .-> entrada
\end{diagrama}
\small Un pedido inválido vuelve al inicio.
\end{frame}"""


def ejemplo_kind(kind: str) -> str:
    if kind == "diagram" and hay_mermaid():
        return EJEMPLO_MERMAID
    return KINDS[kind][1]


def es_snap(ruta: str) -> bool:
    """Navegador de snap (o script que lo lanza): su confinamiento no deja leer
    /usr/local/lib/node_modules, donde está la página de mermaid-cli."""
    real = Path(ruta).resolve()
    if str(real).startswith("/snap/"):
        return True
    try:
        return real.stat().st_size < 20000 and "/snap/" in real.read_text(errors="ignore")
    except OSError:
        return False


def _chromium() -> str | None:
    if os.environ.get("PUPPETEER_EXECUTABLE_PATH"):     # elección explícita del usuario
        return os.environ["PUPPETEER_EXECUTABLE_PATH"]
    for c in ("/opt/pw-browsers/chromium", shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
              shutil.which("chromium"), shutil.which("chromium-browser")):
        if c and Path(c).exists() and not es_snap(c):
            return c
    return None


ERROR_DE_CODIGO_MERMAID = ("Parse error", "Lexical error", "Syntax error", "UnknownDiagramError",
                           "No diagram type detected", "Expecting ")


def render_mermaid(codigo: str, figdir: Path, prueba: bool = False) -> tuple[str | None, list[str], list[str]]:
    """Dibuja Mermaid en figdir/diag_<hash>.png (transparente) con mermaid-cli (mmdc). Beamer usa
    también el PNG: el PDF de mermaid-cli trae fondo blanco, que no calza con el tema."""
    import hashlib
    if not shutil.which("mmdc"):
        return None, ["Diagrama: Mermaid (mmdc) no está instalado; escríbelo en DOT de Graphviz (digraph { … })"], []
    conf = json.dumps(MERMAID_CONFIG, sort_keys=True)
    h = hashlib.sha1((conf + codigo.strip()).encode()).hexdigest()[:10]
    figdir.mkdir(parents=True, exist_ok=True)
    png = figdir / f"diag_{h}.png"
    if not png.exists():
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "d.mmd").write_text(codigo.strip() + "\n")
            (t / "c.json").write_text(conf)
            pp = {"args": ["--no-sandbox"], **({"executablePath": _chromium()} if _chromium() else {})}
            (t / "p.json").write_text(json.dumps(pp))
            for dest, extra in ((png, ["-s", str(MERMAID_ESCALA), "-b", "transparent"]),):
                try:
                    r = subprocess.run(["mmdc", "-q", "-p", str(t / "p.json"), "-c", str(t / "c.json"),
                                        "-i", str(t / "d.mmd"), "-o", str(dest), *extra],
                                       capture_output=True, text=True, timeout=90)
                except subprocess.TimeoutExpired:
                    return None, ["Diagrama: Mermaid tardó demasiado; simplifica el diagrama"], []
                if r.returncode or not dest.exists():
                    lineas = [l for l in (r.stderr or r.stdout).splitlines() if l.strip()]
                    msg = " ".join(l.strip() for l in lineas[:4] if not l.strip().startswith(("at ", "Parser.")))
                    if prueba or any(k in msg for k in ERROR_DE_CODIGO_MERMAID):
                        return None, [f"Diagrama: el código Mermaid no es válido ({msg[:300]})"], []
                    # no es el código: mermaid-cli no funciona aquí (instalación, navegador…)
                    _MOTORES["mermaid"] = (False, msg[:200])
                    otro = ("escribe el diagrama en DOT de Graphviz (digraph { … })" if hay_graphviz()
                            else "no hay otro motor: muestra el proceso como lista numerada o bloques, sin TikZ")
                    return None, [f"Diagrama: Mermaid no funciona en esta máquina ({msg[:160]}); {otro}"], []
    avisos = []
    import pymupdf
    pm = pymupdf.Pixmap(str(png))
    ancho_in, alto_in = pm.width / (96 * MERMAID_ESCALA), pm.height / (96 * MERMAID_ESCALA)
    letra = 12 * min(1.0, AREA_DIAGRAMA_IN[0] / max(ancho_in, 0.01), AREA_DIAGRAMA_IN[1] / max(alto_in, 0.01))
    if letra < MIN_LETRA_DIAGRAMA:
        avisos.append(f"Diagrama demasiado grande para la diapo: la letra quedaría de {letra:.1f}pt; "
                      "usa menos nodos o cambia la dirección (LR si es alto, TD si es ancho)")
    return f"figuras/{png.name}", [], avisos


def _dot_con_estilo(dot: str) -> str:
    """El estilo por defecto va primero: lo que escriba el modelo en el grafo lo puede cambiar."""
    i = dot.find("{")
    if i < 0:
        return dot
    defaults = "".join(f"\n  {k} [{v}];" for k, v in DIAGRAMA_ESTILO.items())
    return dot[:i + 1] + defaults + dot[i + 1:]


def render_diagrama(dot: str, figdir: Path) -> tuple[str | None, list[str], list[str]]:
    """Dibuja un diagrama (DOT o Mermaid, según el código) en figdir/diag_<hash>.pdf y .png.
    Devuelve (ruta relativa, errores, avisos de estilo)."""
    import hashlib
    if motor_diagrama(dot) == "mermaid":
        return render_mermaid(dot, figdir)
    if not hay_graphviz():
        otro = ("escribe el diagrama en Mermaid (flowchart LR, sequenceDiagram…)" if hay_mermaid()
                else "no hay motor de diagramas: muestra el proceso como lista numerada o bloques, sin TikZ")
        return None, [f"Diagrama: Graphviz (dot) no está disponible; {otro}"], []
    codigo = _dot_con_estilo(dot.strip())
    h = hashlib.sha1(codigo.encode()).hexdigest()[:10]
    figdir.mkdir(parents=True, exist_ok=True)
    pdf, png = figdir / f"diag_{h}.pdf", figdir / f"diag_{h}.png"
    try:
        plano = subprocess.run(["dot", "-Tplain"], input=codigo, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None, ["Diagrama: Graphviz tardó demasiado; simplifica el grafo"], []
    if plano.returncode:
        msg = " ".join(plano.stderr.split())[:300]
        return None, [f"Diagrama: el DOT no es válido ({msg})"], []
    nodos = [l for l in plano.stdout.splitlines() if l.startswith("node ")]
    avisos = []
    if len(nodos) > MAX_NODOS_DIAGRAMA:
        avisos.append(f"Diagrama con {len(nodos)} nodos; máximo {MAX_NODOS_DIAGRAMA}: agrupa o simplifica")
    cab = plano.stdout.split("\n", 1)[0].split()           # graph escala ancho alto
    if len(cab) >= 4:
        ancho, alto = float(cab[2]), float(cab[3])
        letra = 14 * min(1.0, AREA_DIAGRAMA_IN[0] / max(ancho, 0.1), AREA_DIAGRAMA_IN[1] / max(alto, 0.1))
        if letra < MIN_LETRA_DIAGRAMA:
            avisos.append(f"Diagrama demasiado grande para la diapo: la letra quedaría de {letra:.1f}pt; "
                          "usa menos nodos o cambia rankdir (LR si es alto, TB si es ancho)")
    for fmt, dest, extra in (("pdf", pdf, []), ("png", png, ["-Gdpi=220"])):
        if not dest.exists():
            r = subprocess.run(["dot", f"-T{fmt}", *extra, "-o", str(dest)], input=codigo,
                               capture_output=True, text=True, timeout=60)
            if r.returncode:
                return None, [f"Diagrama: Graphviz no pudo dibujarlo ({' '.join(r.stderr.split())[:200]})"], avisos
    return f"figuras/{pdf.name}", [], avisos


def expandir_diagramas(frame: str, figdir: str | Path | None) -> tuple[str, list[str], list[str]]:
    """Reemplaza cada \\begin{diagrama}…\\end{diagrama} por la imagen dibujada con Graphviz."""
    if "\\begin{diagrama}" not in frame:
        return frame, [], []
    figdir = Path(figdir) if figdir else Path(tempfile.mkdtemp()) / "figuras"
    errores, avisos = [], []

    def cambiar(m):
        ruta, e, a = render_diagrama(m.group(2), figdir)
        errores.extend(e)
        avisos.extend(a)
        if not ruta:
            return "\\fbox{Diagrama pendiente}"
        opts = m.group(1) or "width=0.9\\textwidth"
        if "height" not in opts:
            opts += ",height=0.62\\textheight"
        if "keepaspectratio" not in opts:
            opts += ",keepaspectratio"
        return f"\\begin{{center}}\n\\includegraphics[{opts}]{{{ruta}}}\n\\end{{center}}"
    return DIAGRAMA.sub(cambiar, frame), errores, avisos


def sin_diagramas(frame: str) -> str:
    """El frame sin el código DOT (para las reglas de estilo, que miran el LaTeX)."""
    return DIAGRAMA.sub(r"\\begin{center}\\end{center}", frame)

# ----------------------------------------------------------------------------
# Esquemas
# ----------------------------------------------------------------------------
class SlideSpec(BaseModel):
    title: str
    bullets: list[str]
    kind: str = Field(description="bullets, columns, block, equation, table, figure o algorithm")
    sources: list[str] = Field(default_factory=list,
                               description="IDs de fragmentos que usa la diapo")
    section: str = Field(default="", description="sección de la presentación a la que pertenece")
    aviso: str = Field(default="", description="duda para quien revisa el guion (p. ej. qué tabla usar); vacío si no hay")

    @field_validator("kind")
    @classmethod
    def _alias(cls, v: str) -> str:
        # el modelo a veces usa el nombre del entorno LaTeX en vez del tipo
        v = v.strip().lower()
        return KIND_ALIASES.get(v, v)


class Outline(BaseModel):
    title: str
    authors: str
    venue: str
    notation: str = Field(description="Glosario breve de símbolos comunes a todas las diapos")
    slides: list[SlideSpec]


class Claim(BaseModel):
    afirmacion: str
    veredicto: str = Field(description="respaldada | no_respaldada | contradicha")
    evidencia: str = Field(description="cita breve de la fuente, o por qué no hay respaldo")

    @field_validator("veredicto")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "_")


class Review(BaseModel):
    claims: list[Claim]


class NotaDiapo(BaseModel):
    diapo: int = Field(description="número de la diapositiva, el que va entre corchetes")
    texto: str = Field(description="lo que dice quien presenta, en texto plano (sin LaTeX)")


class Notas(BaseModel):
    """Guion del expositor: una nota por diapositiva."""
    notas: list[NotaDiapo]


class TablaLeida(BaseModel):
    numero: int
    pagina: int
    titulo: str
    encabezados: list[str] = Field(description="encabezados reales de columnas (y de filas agrupadas)")
    metodos: list[str] = Field(description="métodos/estrategias/variantes que compara, como aparecen")
    que_mide: str = Field(description="qué mide cada celda y qué significa la fila de totales, si hay")


class ElementoLeido(BaseModel):
    id: str = Field(description="alg1, alg2... o eq1, eq2... en orden de aparición")
    pagina: int
    titulo: str = Field(description="nombre del algoritmo o de qué define la ecuación")
    latex: str = Field(default="", description="solo ecuaciones: la fórmula en LaTeX")


class SeccionLeida(BaseModel):
    id: str = Field(description="el id sec* recibido")
    paginas: str = Field(description='rango de páginas, p. ej. "9-10"')


class Lectura(BaseModel):
    """Inventario de lo que Claude leyó como imagen (modo claude). No es una transcripción."""
    tablas: list[TablaLeida]
    algoritmos: list[ElementoLeido]
    ecuaciones: list[ElementoLeido]
    secciones: list[SeccionLeida]


FrameResult = tuple[int, str, str, int, list[str]]  # (idx, frame, status, intentos, avisos)


class State(TypedDict, total=False):
    source_path: str
    base_path: str
    style_path: str
    out_dir: str
    outline_path: str       # guion ya revisado: se usa en vez de generarlo
    extractor: str
    human_review: bool
    chunks: dict[str, str]
    texto: str              # capa de texto completa del paper (chequeo de cifras en modo claude)
    figuras_dir: str        # PNG de las figuras recortadas del PDF (figN.png)
    outline: dict
    head: str               # base hasta el marcador (incluye \\begin{document})
    tail: str               # base desde el marcador
    frames: Annotated[list[FrameResult], operator.add]
    tex: str
    log_errors: list[str]
    global_attempts: int
    pdf_path: str
    inicio: float           # cuándo empezó la ejecución (para el resumen de errores del informe)
    pptx: str               # plantilla .pptx: si está, también se exporta presentacion.pptx
    extras: list[str]       # fuentes adicionales (.pdf, .tex, .md, .txt): fragmentos x1…, x2…
    instrucciones: str      # instrucciones de quien presenta (texto libre)
    notas_expositor: bool   # --notas: escribir el guion del expositor
    notas: dict             # {str(idx): nota} de cada diapositiva
    avisos_notas: list[str]


class SlideState(TypedDict, total=False):
    idx: int
    spec: dict
    context: str
    head: str
    notation: str
    macros: str
    packages: str
    style_guide: str
    limits: dict
    damaged: list[str]      # tablas citadas que la extracción dejó dañadas
    context_cifras: str     # modo claude: capa de texto completa para el chequeo de cifras
    figuras_dir: str
    tables: str             # índice de todas las tablas (título y encabezados)
    plan: str               # títulos del guion completo
    previos: str            # errores frecuentes de ejecuciones anteriores (aviso en el prompt)
    instrucciones: str      # instrucciones de quien presenta (bloque ya formateado)
    paper: str
    style_errors: list[str]
    fact_errors: list[str]  # afirmaciones que el revisor no encontró respaldadas
    reviews: int            # revisiones hechas (presupuesto propio, no gasta attempts)
    best_frame: str         # última versión que compiló: nunca se pierde
    frame: str
    errors: list[str]
    warnings: list[str]
    attempts: int
    frames: Annotated[list[FrameResult], operator.add]


class SlideOutput(TypedDict):
    frames: Annotated[list[FrameResult], operator.add]

# ----------------------------------------------------------------------------
# LLM (aislado para poder simularlo en pruebas)
# ----------------------------------------------------------------------------
def _chat(model: str, effort: str | None = None):
    if PROVIDER == "openai":                      # lee OPENAI_API_KEY del entorno
        from langchain_openai import ChatOpenAI
        if not effort:
            return ChatOpenAI(model=model, max_tokens=8000, max_retries=3)
        # Razonamiento + function calling solo se admite en la Responses API. El
        # razonamiento consume tokens de salida: se amplía el tope.
        return ChatOpenAI(model=model, max_tokens=24000, max_retries=3,
                          use_responses_api=True, reasoning={"effort": effort})
    from langchain_anthropic import ChatAnthropic  # lee ANTHROPIC_API_KEY del entorno
    return ChatAnthropic(model=model, max_tokens=8000, max_retries=3)


# Consumo de tokens por modelo; las diapositivas se escriben en paralelo.
USO: dict[str, dict[str, int]] = {}
_USO_LOCK = threading.Lock()


def registrar_uso(model: str, msg) -> None:
    u = getattr(msg, "usage_metadata", None) or {}
    with _USO_LOCK:
        d = USO.setdefault(model, {"llamadas": 0, "entrada": 0, "salida": 0})
        d["llamadas"] += 1
        d["entrada"] += u.get("input_tokens", 0)
        d["salida"] += u.get("output_tokens", 0)


class RespuestasAgotadas(RuntimeError):
    """El modelo no dio una respuesta válida en los intentos que tiene el nodo."""


def pedir_a_claude(payload: dict):
    """Proveedor 'claude': pausa el grafo con la tarea; la respuesta llega al reanudar."""
    return interrupt({"tipo_llm": True, **payload})


def call_text(model: str, prompt: str) -> str:
    if PROVIDER == "claude":
        return strip_fences(pedir_a_claude({"formato": "texto", "prompt": prompt}))
    msg = _chat(model, REASONING_SLIDES).invoke(prompt)
    registrar_uso(model, msg)
    out = msg.content
    if isinstance(out, list):
        out = "".join(b.get("text", "") for b in out if isinstance(b, dict))
    return strip_fences(out)


def call_structured(model: str, schema: type[BaseModel], prompt: str,
                    effort: str | None = None) -> BaseModel:
    if PROVIDER == "claude":
        data = pedir_a_claude({"formato": "json", "esquema": schema.__name__,
                               "json_schema": schema.model_json_schema(), "prompt": prompt})
        return schema.model_validate(restaurar_escapes(data))
    # function_calling tolera campos opcionales del esquema en ambos proveedores
    kw = {"method": "function_calling"} if PROVIDER == "openai" else {}
    res = _chat(model, effort).with_structured_output(schema, include_raw=True, **kw).invoke(prompt)
    registrar_uso(model, res["raw"])
    if res["parsed"] is None:
        raise res["parsing_error"] or RuntimeError("El modelo no devolvió el esquema pedido")
    return schema.model_validate(restaurar_escapes(res["parsed"].model_dump()))


# En JSON, "\t", "\b", "\f", "\r" y "\n" son escapes: si el modelo escribe \texttt
# o \frac sin doblar la barra, llegan como caracteres de control. Se restauran.
_CONTROL = {"\t": r"\t", "\b": r"\b", "\f": r"\f", "\r": r"\r"}
_NEWLINE_CMD = re.compile(r"\n(?=(?:abla|eq|eg|ot|oindent|ewline|u|i|leq|geq|mid)(?![A-Za-z]))")


def restaurar_escapes(obj):
    if isinstance(obj, str):
        # a veces el modelo emite \u0000 + hex en vez del carácter (á → "\0e1")
        obj = re.sub("\x00([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), obj)
        obj = _NEWLINE_CMD.sub(lambda _: "\\n", obj)
        # y otras veces escapa de más: "\\\\texttt" (salto de línea + texto) → "\\texttt"
        obj = re.sub(r"\\\\(?=[A-Za-z])", lambda _: "\\", obj)
        return "".join(_CONTROL.get(c, c) for c in obj)
    if isinstance(obj, list):
        return [restaurar_escapes(x) for x in obj]
    if isinstance(obj, dict):
        return {k: restaurar_escapes(v) for k, v in obj.items()}
    return obj


def strip_fences(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:latex|tex)?\s*\n", "", s)
    return re.sub(r"\n```\s*$", "", s).strip()

# ----------------------------------------------------------------------------
# Extracción y troceado
# ----------------------------------------------------------------------------
def extract_text(path: Path, extractor: str) -> tuple[str, str]:
    """Devuelve (texto, formato) con formato 'tex' o 'md'."""
    if path.suffix == ".tex":
        return path.read_text(errors="replace"), "tex"
    if path.suffix in (".md", ".txt"):
        return path.read_text(errors="replace"), "md"
    if extractor == "marker":
        # marker conserva ecuaciones como LaTeX; más lento, mejor para papers con fórmulas
        tmp = Path(tempfile.mkdtemp())
        subprocess.run(["marker_single", str(path), "--output_dir", str(tmp),
                        "--output_format", "markdown"], check=True)
        md = next(tmp.rglob("*.md"))
        return md.read_text(errors="replace"), "md"
    import pymupdf4llm
    return pymupdf4llm.to_markdown(str(path)), "md"


def chunk_document(text: str, fmt: str) -> dict[str, str]:
    """Separa ecuaciones, tablas y algoritmos como fragmentos propios y trocea
    el resto por secciones. En el texto de cada sección queda un marcador [id]."""
    chunks: dict[str, str] = {}
    counters: dict[str, int] = {}

    def take(pattern: str, prefix: str, flags: int) -> None:
        nonlocal text

        def repl(m: re.Match) -> str:
            counters[prefix] = counters.get(prefix, 0) + 1
            cid = f"{prefix}{counters[prefix]}"
            chunks[cid] = m.group(0).strip()
            return f" [{cid}] "
        text = re.sub(pattern, repl, text, flags=flags)

    if fmt == "tex":
        body = re.search(r"\\begin\{document\}(.*)\\end\{document\}", text, re.S)
        text = body.group(1) if body else text
        text = re.sub(r"(?<!\\)%.*", "", text)
        take(r"\\begin\{(algorithm)\*?\}.*?\\end\{\1\*?\}", "alg", re.S)
        take(r"\\begin\{(table)\*?\}.*?\\end\{\1\*?\}", "tab", re.S)
        take(r"\\begin\{(equation|align|eqnarray|gather)\*?\}.*?\\end\{\1\*?\}", "eq", re.S)
        heading = r"^\\(?:sub)*section\*?\{(.+?)\}"
    else:
        take(r"\$\$.+?\$\$", "eq", re.S)
        take(r"(?:^\|.*\|[ \t]*\n?){2,}", "tab", re.M)
        take(r"^(?:\*\*)?Algorithm \d+.*?(?=\n\s*\n)", "alg", re.M | re.S)
        heading = r"^#{1,4}\s+(.+)$"

    if fmt != "tex":
        text = _ordenar_tablas_md(text, chunks)

    parts = re.split(heading, text, flags=re.M)
    sections = [("Preámbulo", parts[0])] + list(zip(parts[1::2], parts[2::2]))
    n = 0
    for title, content in sections:
        content = content.strip()
        if len(content) < 40:
            continue
        n += 1
        chunks[f"sec{n}"] = f"{title.strip()}\n{content}"
    return chunks

_FIG_CAPTION = re.compile(r"^(?:Fig\.?|Figure)\s*(\d+)\s*[.:|]?\s+[A-Z(]")


def extraer_figuras(pdf: Path, dest: Path, dpi: int = 200) -> dict[int, dict]:
    """Recorta cada figura del PDF como PNG (dest/figN.png). Determinista: busca los pies
    («Fig. N» / «Figure N» + mayúscula; las menciones en el texto no calzan) y toma la zona
    de imágenes y dibujos sobre cada pie, con sus etiquetas, sin cabecera ni pie de página."""
    import pymupdf
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[int, dict] = {}
    with pymupdf.open(pdf) as doc:
        for pno, page in enumerate(doc, 1):
            head, foot = page.rect.height * 0.08, page.rect.height * 0.94
            txt = [(pymupdf.Rect(b["bbox"]), " ".join(sp["text"] for ln in b["lines"] for sp in ln["spans"]).strip())
                   for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            txt = [(r, t) for r, t in txt if r.y0 >= head and r.y1 <= foot]
            graf = [pymupdf.Rect(d["rect"]) for d in page.get_drawings()]
            graf += [pymupdf.Rect(r) for img in page.get_images(full=True) for r in page.get_image_rects(img[0])]
            graf = [g for g in graf if g.width > 20 and g.height > 20 and g.y0 >= head and g.y1 <= foot]
            ancho = 0.6 * page.rect.width
            prev = head
            for r, t, n in sorted(((r, t, int(m.group(1))) for r, t in txt if (m := _FIG_CAPTION.match(t))),
                                  key=lambda c: c[0].y0):
                # la figura empieza bajo el último párrafo de texto corrido (o el pie anterior)
                parrafos = [tr.y1 for tr, _ in txt if prev < tr.y1 <= r.y0 and tr.width > ancho
                            and not any(g.intersects(tr) for g in graf)]
                top = max(parrafos + [prev])
                sel = [g for g in graf if g.y0 >= top - 2 and g.y1 <= r.y0 + 3]
                prev = r.y1
                if not sel or n in out:
                    continue
                box = pymupdf.Rect(sel[0])
                for g in sel[1:]:
                    box |= g
                cerca = box + (-25, -25, 25, 25)            # etiquetas de ejes, (a), (b)...
                for tr, _ in txt:
                    if tr.y0 >= top - 2 and tr.y1 <= r.y0 and tr.width < ancho and cerca.intersects(tr):
                        box |= tr
                box = (box + (-3, -3, 3, 3)) & page.rect
                f = dest / f"fig{n}.png"
                page.get_pixmap(clip=box, dpi=dpi).save(f)
                out[n] = {"pagina": pno, "caption": re.sub(r"\s+", " ", t), "archivo": f"figuras/{f.name}"}
    return out


TABLA_DANADA = ("[TABLA DAÑADA EN LA EXTRACCIÓN: celdas mezcladas o sin encabezados. "
                "No la reproduzcas como tabla; toma las cifras del texto de la sección.]")
_CAPTION = re.compile(r"\*\*Table\s+(\d+)\*\*[.:]?\s*([^\n]*)")
# Entre dos trozos de una tabla partida solo hay cabeceras/pies de página (números, revista)
_ENTRE_PAGINAS = re.compile(r"^(?:\s|\d+|[A-Z][A-Za-z. ]{0,40})*$")


def tabla_danada(md: str) -> bool:
    """Heurística: pymupdf4llm pega celdas con <br> cuando no entiende la tabla
    (típico de tablas rotadas o partidas entre páginas)."""
    rows = [r for r in md.splitlines() if r.startswith("|") and not re.fullmatch(r"[|\-: ]+", r)]
    return sum(r.count("<br>") for r in rows[1:]) > 2


def _ordenar_tablas_md(text: str, chunks: dict[str, str]) -> str:
    """Pega a cada tabla su título, la numera como en el paper y une los trozos de
    una tabla partida en varias páginas. Las tablas rotas quedan marcadas."""
    marks = list(re.finditer(r" \[(tab\d+)\] ", text))
    if not marks:
        return text
    tabs = {m.group(1): chunks.pop(m.group(1)) for m in marks}
    groups: list[dict] = []                # {num, caption, ids}
    used, prev_end = set(), 0
    for m in marks:
        before = text[max(prev_end, m.start() - 600):m.start()]
        caps = [c for c in _CAPTION.finditer(before) if c.group(1) not in used]
        if caps:
            used.add(caps[-1].group(1))
            groups.append({"num": int(caps[-1].group(1)), "caption": caps[-1].group(2).strip(), "ids": [m.group(1)]})
        elif groups and _ENTRE_PAGINAS.match(text[prev_end:m.start()]):
            groups[-1]["ids"].append(m.group(1))          # continuación de la tabla anterior
        else:
            num = (groups[-1]["num"] + 1) if groups else 1
            groups.append({"num": num, "caption": "", "ids": [m.group(1)]})
        prev_end = m.end()
    for g in groups:
        new = f"tab{g['num']}"
        md = "\n".join(tabs[i] for i in g["ids"])
        head = f"Table {g['num']}" + (f": {g['caption']}" if g["caption"] else "")
        # una tabla rota solo confunde al modelo: se deja el aviso y no su contenido
        chunks[new] = head + "\n" + (TABLA_DANADA if tabla_danada(md) else md)
        for k, old in enumerate(g["ids"]):
            text = text.replace(f" [{old}] ", f" [@{new}] " if k == 0 else " ", 1)
    return text.replace("[@tab", "[tab")

# ----------------------------------------------------------------------------
# LaTeX: compilación, log, lint
# ----------------------------------------------------------------------------
def preflight(base: str) -> None:
    """Verifica, una sola vez, que los paquetes de la base existan y que la base compile."""
    if not shutil.which("pdflatex"):
        raise RuntimeError("pdflatex no está instalado")
    if base.count(SLIDES_MARK) != 1:
        raise RuntimeError(f"La base debe contener exactamente un marcador {SLIDES_MARK}")
    missing = [f for f in base_files(base)
               if not subprocess.run(["kpsewhich", f], capture_output=True, text=True).stdout.strip()]
    if missing:
        raise RuntimeError(f"Faltan paquetes LaTeX: {missing}. Instálalos o ajusta la base.")
    errs, _ = compile_tex(fill_base(base, None).replace(SLIDES_MARK, ""))
    if errs:
        raise RuntimeError("La base no compila:\n" + "\n".join(errs))


def compile_tex(tex: str, passes: int = 1, offset: int = 0,
                ignore_vbox: bool = False, figuras: str | None = None) -> tuple[list[str], Path]:
    d = Path(tempfile.mkdtemp(prefix="beamer_"))   # directorio propio: seguro en paralelo
    (d / "doc.tex").write_text(tex)
    if figuras and Path(figuras).is_dir():
        shutil.copytree(figuras, d / "figuras")
    cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
           "-no-shell-escape", "doc.tex"]
    for _ in range(passes):
        try:
            subprocess.run(cmd, cwd=d, capture_output=True, timeout=LATEX_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return ["Tiempo de compilación agotado (¿bucle en TikZ o macro recursiva?)"], d
    log_file = d / "doc.log"
    log = log_file.read_text(errors="replace") if log_file.exists() else ""
    errors = parse_log(log, tex, offset, ignore_vbox)
    if not (d / "doc.pdf").exists() and not errors:
        errors = ["No se generó PDF; revisa el log"]
    return errors, d


def parse_log(log: str, src: str, offset: int = 0, ignore_vbox: bool = False) -> list[str]:
    lines, src_lines, out = log.splitlines(), src.splitlines(), []

    def where(ln: int | None) -> str:
        if ln is None:
            return ""
        code = src_lines[ln - 1].strip()[:120] if 0 < ln <= len(src_lines) else ""
        return f" (línea {ln - offset}: `{code}`)"

    for i, line in enumerate(lines):
        if line.startswith("! ") and "Fatal error" not in line and "Emergency stop" not in line:
            # Beamer lee el frame como argumento: l.NN suele ser \end{frame} y el token
            # culpable aparece en las líneas '<argument> ...' previas.
            ln, ctx = None, []
            for nxt in lines[i + 1:i + 15]:
                m = re.match(r"^l\.(\d+)(.*)", nxt)
                if m:
                    ln = int(m.group(1))
                    break
                if nxt.strip():
                    ctx.append(nxt.strip())
            hint = f" contexto: {' '.join(ctx)[-160:]}" if ctx else ""
            out.append(f"Error: {line[2:]}{where(ln)}{hint}")
        m = re.match(r"^Overfull \\([hv])box \((\d+(?:\.\d+)?)pt too (wide|high)\)", line)
        if m and float(m.group(2)) > OVERFULL_TOLERANCE_PT:
            if m.group(1) == "v" and ignore_vbox:
                continue
            lm = re.search(r"lines? (\d+)", line)
            kind = "demasiado ancho" if m.group(3) == "wide" else "demasiado alto (no cabe)"
            out.append(f"Desborde: contenido {kind} por {m.group(2)}pt"
                       f"{where(int(lm.group(1)) if lm else None)}")
    return list(dict.fromkeys(out))


FORBIDDEN = [r"\\documentclass", r"\\usepackage", r"\\begin\{document\}", r"\\end\{document\}",
             r"\\newcommand", r"\\renewcommand", r"\\def\b", r"\\input", r"\\include\b",
             r"\\write18", r"\\openout"]


def lint_frame(frame: str) -> list[str]:
    errs = []
    if frame.count(r"\begin{frame}") != 1 or frame.count(r"\end{frame}") != 1:
        errs.append("Debe haber exactamente un \\begin{frame} y un \\end{frame}")
    for pat in FORBIDDEN:
        if re.search(pat, frame):
            errs.append(f"Comando no permitido en un frame: {pat.replace(chr(92) * 2, chr(92))}")
    for ruta in re.findall(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", frame):
        if not re.fullmatch(r"figuras/fig\d+\.png", ruta.strip()):
            errs.append(f"\\includegraphics solo admite figuras/figN.png del paper, no «{ruta}»")
    if re.search(r"\\verb|verbatim|lstlisting", frame) and "[fragile" not in frame:
        errs.append("Usa \\begin{frame}[fragile] si incluyes verbatim o \\verb")
    return errs


def _norm_numbers(s: str) -> str:
    s = re.sub(r"\\[,;!]|~|\{,\}", "", s)
    return re.sub(r"(?<=\d)[ ,](?=\d{3}\b)", "", s)


def categoria_error(msg: str) -> str:
    """Normaliza un mensaje de validador para agrupar los del mismo tipo entre ejecuciones:
    sin líneas, contexto ni números concretos, pero con la macro culpable si la hay."""
    macro = re.findall(r"\\[A-Za-z@]+", msg.split("contexto:", 1)[1]) if "contexto:" in msg else []
    t = re.split(r" \(línea | contexto:", msg)[0].strip()
    if macro and "Undefined control sequence" in t:
        t = t.rstrip(".") + f" ({macro[-1]})"
    t = re.sub(r"'[^']*'|«[^»]*»", "…", t)
    return re.sub(r"\d+(?:\.\d+)?", "N", t)


def registrar_errores(tipo: str, kind: str, msgs: list[str], paper: str = "") -> None:
    """Agrega al registro persistente lo que rechazó un validador (una línea JSON por error)."""
    if not msgs or ERRORES_LOG == "off":
        return
    ahora = time.time()
    lineas = "".join(json.dumps({"ts": ahora, "paper": Path(paper).name, "tipo": tipo, "kind": kind,
                                 "categoria": categoria_error(m), "mensaje": m[:300]},
                                ensure_ascii=False) + "\n" for m in msgs)
    try:
        with open(ERRORES_LOG, "a") as f:      # escrituras cortas en modo append: seguras en paralelo
            f.write(lineas)
    except OSError:
        pass                                   # el registro nunca debe frenar la generación


def leer_errores(desde: float = 0.0, ruta: str | None = None) -> list[dict]:
    ruta = ruta or ERRORES_LOG
    p = Path(ruta)
    if ruta == "off" or not p.exists():
        return []
    out = []
    for l in p.read_text().splitlines():
        try:
            r = json.loads(l)
        except json.JSONDecodeError:
            continue
        if r.get("ts", 0) >= desde:
            out.append(r)
    return out


def errores_previos(n: int = MAX_ERRORES_PREVIOS, min_veces: int = MIN_VECES_PREVIOS) -> str:
    """Párrafo para el prompt con las categorías de error más frecuentes; "" si no hay."""
    from collections import Counter
    cuenta = Counter(r["categoria"] for r in leer_errores())
    top = [(c, k) for c, k in cuenta.most_common(n) if k >= min_veces]
    if not top:
        return ""
    return ("Errores que los validadores detectaron en diapositivas de ejecuciones anteriores "
            "(evítalos desde el primer intento):\n" + "\n".join(f"- {c} ({k} veces)" for c, k in top))


def resumen_errores(ruta: str | None = None) -> str:
    """--resumen-errores: tabla markdown del registro, por categoría (la más frecuente primero),
    para revisarlo antes de proponer mejoras a la herramienta."""
    import datetime as dt
    from collections import Counter, defaultdict
    rs = leer_errores(ruta=ruta)
    ruta = ruta or ERRORES_LOG
    if not rs:
        return f"Sin errores registrados en {ruta}."
    grupos: dict[str, list[dict]] = defaultdict(list)
    for r in rs:
        grupos[r["categoria"]].append(r)
    fecha = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    cel = lambda t: " ".join(str(t).replace("|", "/").split())
    papers = {r.get("paper") for r in rs}
    lineas = [f"# Resumen de errores de los validadores\n",
              f"{len(rs)} errores en {len(grupos)} categorías, {len(papers)} paper(s), "
              f"del {fecha(min(r['ts'] for r in rs))} al {fecha(max(r['ts'] for r in rs))} "
              f"(registro: `{ruta}`).\n",
              "Por tipo: " + ", ".join(f"{t} {k}" for t, k in Counter(r["tipo"] for r in rs).most_common())
              + " · por tipo de diapo: "
              + ", ".join(f"{t} {k}" for t, k in Counter(r["kind"] for r in rs).most_common()) + "\n",
              "| Veces | Categoría | Tipo | Diapos | Papers | Último | Ejemplo |",
              "|---|---|---|---|---|---|---|"]
    for cat, g in sorted(grupos.items(), key=lambda kv: -len(kv[1])):
        kinds = ", ".join(f"{k} {n}" for k, n in Counter(r["kind"] for r in g).most_common())
        lineas.append(f"| {len(g)} | {cel(cat)} | {cel(g[-1]['tipo'])} | {kinds} | "
                      f"{len({r.get('paper') for r in g})} | {fecha(max(r['ts'] for r in g))} | "
                      f"{cel(g[-1]['mensaje'])[:140]} |")
    return "\n".join(lineas)


def soft_checks(frame: str, context: str) -> list[str]:
    """Avisos que no bloquean: densidad y cifras que no aparecen en las fuentes."""
    warns = []
    text = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", " ", frame)
    text = re.sub(r"[{}$\\&_^]", "", text)
    if len(" ".join(text.split())) > MAX_TEXT_CHARS:
        warns.append("Diapositiva densa: considera partirla")
    frame = re.sub(r"\\includegraphics\s*(\[[^\]]*\])?\s*\{[^}]*\}", "", frame)
    body = re.sub(r"\d*\.?\d+\s*(pt|em|ex|cm|mm|in)\b|\d*\.?\d+\\(text|line|column)(width|height)", "",
                  _norm_numbers(frame))
    ctx = _norm_numbers(context)
    for n in sorted(set(re.findall(r"\d+(?:\.\d+)?", body))):
        if len(n.replace(".", "")) >= 2 and n not in ctx:
            warns.append(f"Cifra {n} no aparece en las fuentes: verificar")
    return warns

# ----------------------------------------------------------------------------
# Estilo
# ----------------------------------------------------------------------------
def _guia(path: str | None, clave: str) -> str:
    p = Path(path or DEFAULT_STYLE)
    return tomllib.loads(p.read_text()).get("guia", {}).get(clave, "").strip() if p.exists() else ""


def guia_guion(path: str | None) -> str:
    """Reglas de [guia].guion: solo para el guion (estructura, reparto), no para cada diapo."""
    return _guia(path, "guion")


def guia_notas(path: str | None) -> str:
    """Reglas de [guia].notas: solo para las notas del expositor."""
    return _guia(path, "notas")


def load_style(path: str | None) -> tuple[str, dict]:
    p = Path(path or DEFAULT_STYLE)
    if not p.exists():
        return "", dict(STYLE_DEFAULTS)
    data = tomllib.loads(p.read_text())
    return data.get("guia", {}).get("texto", "").strip(), {
        **STYLE_DEFAULTS, **data.get("limites", {}), **data.get("estructura", {})}


def describe_limits(lim: dict, guion: bool = False) -> str:
    out = [f"máximo {lim['max_items']} viñetas", f"máximo {lim['max_palabras_item']} palabras por viñeta",
           f"máximo {lim['max_anidamiento']} niveles de listas",
           f"título de máximo {lim['max_caracteres_titulo']} caracteres",
           f"máximo {lim['max_alerts']} \\alert y {lim['max_negritas']} \\textbf"]
    if lim["sin_punto_final"]:
        out.append("viñetas sin punto final")
    if lim["prohibir_colores_directos"]:
        out.append("sin \\color ni \\textcolor")
    if lim["prohibir_vspace_negativo"]:
        out.append("sin \\vspace negativo")
    if guion:                                     # regla del conjunto, no de una diapo
        out.append(f"máximo {lim['max_fraccion_vinetas']:.0%} de diapositivas solo con viñetas")
    out.append(f"máximo {lim['max_bloques']} bloques por diapositiva")
    if lim["prohibir_alert_en_alertblock"]:
        out.append("sin \\alert dentro de alertblock")
    out.append("tamaños de letra fuera de tablas/algoritmos: " + (", ".join(lim["tamanos_permitidos"]) or "ninguno"))
    return "; ".join(out)


def _plain_words(s: str) -> int:
    s = re.sub(r"\$[^$]*\$|\\\(.*?\\\)", " M ", s)          # una fórmula = una palabra
    s = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", " ", s)
    s = re.sub(r"[{}\\]", " ", s)
    return len(s.split())


def kind_check(frame: str, kind: str, sources: list[str], damaged: tuple = ()) -> list[str]:
    """El frame debe tener el formato de su tipo y mostrar las tablas/algoritmos/ecuaciones que cita."""
    body = re.sub(r"(?<!\\)%.*", "", frame)
    rules = [KINDS[kind][2]] if kind in KINDS and KINDS[kind][2] else []
    if any(re.fullmatch(r"tab\d+", c) and c not in damaged for c in sources):  # tabla citada → se muestra
        rules.append(KINDS["table"][2])
    errs = [f"Formato ({kind}): {msg}" for pat, msg in dict(rules).items() if not re.search(pat, body)]
    for c in sources:                                   # una figura citada se muestra
        if re.fullmatch(r"fig\d+", c) and f"figuras/{c}.png" not in body:
            errs.append(f"Formato ({kind}): cita {c} pero no la incluye: \\includegraphics{{figuras/{c}.png}}")
    return errs + filas_repetidas(body)


def _sin_colspec(tab: str) -> str:
    """Quita \\begin{tabular}{colspec}, que puede tener llaves anidadas (@{})."""
    i = tab.index("}") + 1                       # fin de \\begin{tabular}
    if i < len(tab) and tab[i] == "{":
        depth = 0
        for j in range(i, len(tab)):
            depth += {"{": 1, "}": -1}.get(tab[j], 0)
            if depth == 0:
                return tab[j + 1:]
    return tab[i:]


def filas_repetidas(body: str) -> list[str]:
    """Filas de tabla con exactamente los mismos números: señal de datos copiados/inventados."""
    errs = []
    for tab in re.findall(r"\\begin\{tabular\*?\}.*?\\end\{tabular\*?\}", body, re.S):
        seen: dict[tuple, str] = {}
        tab = _sin_colspec(tab)
        for row in tab.split(r"\\"):
            cells = row.split("&")
            nums = tuple(re.findall(r"\d[\d.,]*", "&".join(cells[1:])))
            name = re.sub(r"\\[a-zA-Z]+|[{}]", "", cells[0]).split("\n")[-1].strip()
            if len(cells) > 1 and all(re.fullmatch(r"\s*(?:--+|—|-|n/?a|)\s*", c) for c in cells[1:]):
                errs.append(f"La fila '{name}' no tiene datos: omítela en vez de rellenarla con guiones")
                continue
            vacias = [c for c in cells[1:] if re.fullmatch(r"\s*(?:--+|—|–)\s*", c)]
            if vacias and len(cells) > 1:
                errs.append(f"La fila '{name}' tiene celdas con guiones: pon el dato de la fuente, "
                            "déjala sin esa columna o quita la fila; no rellenes con «--»")
            if len(nums) < 2:
                continue
            if nums in seen:
                errs.append(f"Las filas '{seen[nums]}' y '{name}' de la tabla tienen los mismos números: "
                            "no copies filas; incluye solo elementos con datos en la fuente")
            else:
                seen[nums] = name
    return errs


def style_check(frame: str, lim: dict) -> list[str]:
    """Reglas medibles de estilo sobre el código del frame."""
    errs = []
    body = re.sub(r"(?<!\\)%.*", "", frame)

    items = re.split(r"\\item\b", body)[1:]
    if len(items) > lim["max_items"]:
        errs.append(f"{len(items)} viñetas; máximo {lim['max_items']}")
    for k, it in enumerate(items, 1):
        it = re.split(r"\\(?:begin|end)\{(?:itemize|enumerate)\}", it)[0].strip()
        n = _plain_words(it)
        if n > lim["max_palabras_item"]:
            errs.append(f"Viñeta {k} tiene {n} palabras; máximo {lim['max_palabras_item']}")
        if lim["sin_punto_final"] and it.rstrip().endswith("."):
            errs.append(f"Viñeta {k} termina en punto")

    depth = maxd = 0
    for m in re.finditer(r"\\(begin|end)\{(itemize|enumerate)\}", body):
        depth += 1 if m.group(1) == "begin" else -1
        maxd = max(maxd, depth)
    if maxd > lim["max_anidamiento"]:
        errs.append(f"Listas anidadas a {maxd} niveles; máximo {lim['max_anidamiento']}")

    t = re.search(r"\\begin\{frame\}(?:<[^>]*>)?(?:\[[^\]]*\])?\{(.+?)\}\s*$|\\frametitle\{(.+?)\}",
                  body, re.M)
    title = (t.group(1) or t.group(2)) if t else ""
    if not title.strip():
        errs.append("La diapositiva no tiene título")
    elif len(re.sub(r"\\[a-zA-Z]+|[{}$]", "", title)) > lim["max_caracteres_titulo"]:
        errs.append(f"Título demasiado largo (máximo {lim['max_caracteres_titulo']} caracteres)")

    if len(re.findall(r"\\alert\b", body)) > lim["max_alerts"]:
        errs.append(f"Demasiados \\alert (máximo {lim['max_alerts']})")
    if len(re.findall(r"\\textbf\b|\\bfseries\b", body)) > lim["max_negritas"]:
        errs.append(f"Demasiadas negritas (máximo {lim['max_negritas']})")
    if lim["prohibir_colores_directos"] and re.search(r"\\(text)?color\b", body):
        errs.append("Colores directos no permitidos: usa \\alert o macros de la base")
    n_blocks = len(re.findall(r"\\begin\{(?:alert|example)?block\}", body))
    if n_blocks > lim["max_bloques"]:
        errs.append(f"{n_blocks} bloques; máximo {lim['max_bloques']}: combina con texto, lista o fórmula")
    if lim["prohibir_alert_en_alertblock"] and any(
            "\\alert" in b for b in re.findall(r"\\begin\{alertblock\}(.*?)\\end\{alertblock\}", body, re.S)):
        errs.append("\\alert dentro de alertblock es redundante: quítalo")
    if lim["prohibir_vspace_negativo"] and re.search(r"\\vspace\*?\{\s*-", body):
        errs.append("\\vspace negativo no permitido: reduce contenido en vez de comprimir")

    outside = re.sub(r"\\begin\{(algorithm|tabular\*?|table)\}.*?\\end\{\1\}", "", body, flags=re.S)
    for size in ("\\tiny", "\\scriptsize", "\\footnotesize", "\\small"):
        if size not in lim["tamanos_permitidos"] and re.search(re.escape(size) + r"\b", outside):
            errs.append(f"{size} solo se permite dentro de tablas o algoritmos")
    return errs


# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------
OUTLINE_PROMPT = """Diseñas una presentación académica en Beamer, en español, de {nmin} a {nmax} \
diapositivas, sobre el paper cuyos fragmentos aparecen abajo (y las fuentes adicionales, si hay).

{instrucciones}
Devuelve:
- title, authors, venue: en LaTeX válido (escapa &, %, _).
- notation: glosario breve de los símbolos que TODAS las diapositivas deben usar igual.
  Puedes apoyarte en estas macros ya definidas: {macros}
- slides: la portada y la agenda se agregan solas; devuelve solo las diapositivas de contenido. \
Cada una debe listar en sources \
los IDs exactos de los fragmentos que necesita (incluidos eq*, tab*, alg* si usa esa ecuación, \
tabla o algoritmo). Si un tema no cabe en los límites, divídelo en dos diapositivas.
- section: la sección de cada diapo. {regla_secciones}
- aviso: si NO estás seguro de qué tabla o fragmento corresponde a una diapo (p. ej. dos tablas \
parecidas, o una tabla cuyo título no dice qué métodos compara), escríbelo aquí y nombra las \
candidatas ("¿tab1 o tab2? tab2 parece de variantes"). Una persona revisará estos avisos. Déjalo \
vacío cuando estés seguro; no inventes dudas.
- kind: elige el formato que mejor muestre cada idea y VARÍALOS a lo largo de la presentación:
{kinds}
  Cita un tab* solo si la diapo muestra esa tabla (kind table o columns); cita un eq* si muestra \
esa ecuación. Si un tab* está marcado como TABLA DAÑADA, no lo reemplaces por otra tabla del \
paper: arma la tabla con las cifras que da el texto de esa sección (cita la sección), o usa otro \
formato. Las fórmulas que definen el método deben aparecer en display, no descritas con \
palabras. Si el algoritmo es central, dedícale una diapo algorithm. Cita un fig* \
(kind figure) cuando una figura del paper muestre el resultado mejor que una tabla o viñetas.
- bullets: el contenido que debe cubrir la diapo (ideas y datos concretos: cifras, nombres), \
no el texto literal de viñetas.

Guía de estilo:
{guide}
Límites: {limits}
{feedback}
FRAGMENTOS:
{chunks}"""

SLIDE_PROMPT = r"""Escribe UNA diapositiva Beamer (tema metropolis, 16:9, 10pt) en español.

Reglas:
- Devuelve solo el bloque \begin{{frame}}{{...}} ... \end{{frame}}, sin explicación ni ```.
- El preámbulo ya existe: no uses \usepackage, \newcommand ni \documentclass.
- Paquetes cargados: {packages}
- Macros disponibles: {macros}
- Notación común (respétala): {notation}
- Usa solo información del CONTEXTO. No inventes cifras ni resultados.
- Tablas: solo filas y columnas que existan en una tabla del CONTEXTO o cifras explícitas del \
texto. Si un elemento no tiene datos, NO inventes ni copies su fila: omítelo o arma la tabla con \
las cifras del texto; nunca dejes filas con "--". No crees columnas agregadas (mejor/peor, promedios) que la fuente no tenga. \
Respeta qué mide cada cifra (p. ej. "ganancia frente al mejor competidor" no es "frente a X").
- Figuras: solo las fig* del CONTEXTO, con \includegraphics y la ruta exacta que indica \
([archivo: figuras/figN.png]); escala con width y height (keepaspectratio) para que quepa.
- Diagramas (tipo diagram): {diagramas}
- Debe caber en una pantalla. Si el contenido es mucho, prioriza y resume.

Guía de estilo:
{guide}
Límites obligatorios (se verifican automáticamente): {limits}

Tipo: {kind} ({kind_desc})
Ejemplo de formato para este tipo (solo la estructura; el contenido es inventado y no debe copiarse):
{kind_example}
Puedes combinar con un alertblock o exampleblock para el mensaje clave si cabe.

Lugar en la presentación: esta es la diapo {num} (sección: {section}). No repitas lo que \
cubren las demás diapos del guion completo:

{plan}

{instrucciones}{previos}Título: {title}
Contenido a cubrir (ideas y datos, no viñetas literales):
{bullets}
{aviso}
CONTEXTO:
{context}"""

REFINE_SLIDE_PROMPT = r"""Esta diapositiva Beamer no pasa la validación. Corrígela.

Errores del compilador (las líneas son relativas al frame):
{errors}

Reglas de estilo incumplidas:
{style_errors}

Afirmaciones sin respaldo en la fuente (corrígelas según el CONTEXTO o elimínalas):
{fact_errors}

Para desbordes o exceso de contenido: acorta, fusiona o elimina lo menos importante.
No cambies el contenido factual respaldado. Respeta la guía y los límites:
{guide}
Límites: {limits}
Macros disponibles: {macros}
Devuelve solo el frame completo corregido.

FRAME:
{frame}

CONTEXTO (para verificar datos):
{context}"""

LECTURA_PROMPT = """Lee el paper COMPLETO como imagen, página por página (Read con pages, de a 20 como máximo): {paper} ({n_paginas} páginas). Quedará en tu contexto: las tareas siguientes solo te darán referencias (sección, página, tabla) y deberás apoyarte en lo que viste, volviendo a mirar la página si no la tienes presente.

No transcribas. Devuelve un INVENTARIO breve:
- tablas: cada tabla del paper con su número, página, título, encabezados reales, qué métodos compara (tal como están escritos) y qué mide cada celda. Decide por los encabezados, no solo por el título: hay títulos que dicen "all the strategies" en tablas que solo tienen variantes.
- algoritmos: cada algoritmo (id alg1, alg2... en orden), página y nombre.
- ecuaciones: las ecuaciones que definen el problema y el método (id eq1, eq2...), página, qué definen y su LaTeX.
- secciones: para cada id de esta lista, su rango de páginas:
{secciones}
{feedback}"""

REVIEW_SLIDE_PROMPT = r"""Eres un revisor exigente y objetivo. Verifica esta diapositiva contra el \
CONTEXTO, que es la única fuente válida (fragmentos del paper). Revísala como si la hubiera escrito \
otra persona: que suene bien o coincida con lo que recuerdas del paper o del guion no la respalda.

Enumera solo las afirmaciones sustantivas: hechos, cifras y a qué método/métrica/configuración \
corresponden, comparaciones, causas y conclusiones. NO evalúes rótulos ni encabezados (títulos de \
bloque, "Idea clave", "Contexto", nombres de sección), frases introductorias ni el formato LaTeX.

Para cada afirmación con una cifra, un método, una métrica o una comparación, localiza el pasaje o \
la celda exacta de la fuente y cítalo textualmente en evidencia. Comprueba en particular:
- que cada cifra sea de la fila, columna y tabla que dice la diapo (no de la vecina ni de otra tabla);
- que los métodos no estén intercambiados y que la métrica sea la correcta (frente a quién es una \
ganancia, qué mide la fila de totales);
- el sentido de cada comparación, y que superlativos o cuantificadores ("el mejor", "siempre", \
"la más cercana", "todas") no digan más que la fuente;
- causas, conclusiones o trabajo futuro que la fuente no afirma como hecho.
Veredictos:
- respaldada: la fuente lo dice, lo parafrasea fielmente o se deduce directamente (un cálculo \
simple con sus cifras: dilo en evidencia).
- no_respaldada: no encuentras en la fuente el dato o la afirmación.
- contradicha: la fuente dice otra cosa; cita ese pasaje.
Una duda sobre la redacción no es un error; una duda sobre un dato sí: si no lo encuentras, no está \
respaldado.

Usa el ÍNDICE DE TABLAS (todas las tablas del paper, con sus encabezados) para comprobar que \
cada cifra de una tabla o comparación corresponde a los métodos y métricas correctos: si una \
cifra viene de una tabla sobre otros métodos (p. ej. variantes en vez de estrategias \
clásicas), es contradicha.

DIAPOSITIVA:
{frame}

ÍNDICE DE TABLAS:
{tables}

CONTEXTO:
{context}"""

# Los prompts r"""…""" usan «\» al final de línea para partir frases largas: se unen aquí.
SLIDE_PROMPT = SLIDE_PROMPT.replace("\\\n", "")
REVIEW_SLIDE_PROMPT = REVIEW_SLIDE_PROMPT.replace("\\\n", "")

REFINE_GLOBAL_PROMPT = r"""El cuerpo de esta presentación Beamer compila bien diapositiva por
diapositiva, pero falla al compilarse completo. Corrige solo lo necesario; la
cabecera es fija y no puedes cambiarla, así que no agregues paquetes ni macros.

Errores:
{errors}

Devuelve solo el cuerpo corregido (lo que va entre \begin{{document}} y \end{{document}}, sin
incluir esas dos líneas).

CUERPO:
{body}"""

NOTAS_PROMPT = """Escribe las notas del expositor de esta presentación Beamer: lo que dice quien \
presenta en cada diapositiva. Devuelve una nota por cada diapositiva de abajo, con su número \
(el que va entre corchetes).

{instrucciones}\
Reglas:
- Texto plano, para leer en voz alta: sin LaTeX, sin viñetas y sin Markdown. Una fórmula se \
dice con palabras.
- Explica lo que muestra la diapositiva, no la leas: qué significa, por qué importa y cómo \
se conecta con la anterior. Cifras, nombres y resultados: solo los que están en la diapositiva \
(no agregues datos).
- La nota de la portada presenta el tema en una o dos frases.

Guía para las notas:
{guide}

Notación de la presentación: {notation}
{feedback}
DIAPOSITIVAS:
{diapos}"""

# ----------------------------------------------------------------------------
# Utilidades de cabecera
# ----------------------------------------------------------------------------
def base_packages(base: str) -> list[str]:
    pk = []
    for group in re.findall(r"\\usepackage(?:\[[^\]]*\])?\{([^}]+)\}", base):
        pk += [p.strip() for p in group.split(",")]
    return pk


def base_files(base: str) -> list[str]:
    files = [f"{c}.cls" for c in re.findall(r"\\documentclass(?:\[[^\]]*\])?\{([^}]+)\}", base)]
    files += [f"beamertheme{t}.sty" for t in re.findall(r"\\usetheme(?:\[[^\]]*\])?\{([^}]+)\}", base)]
    return files + [f"{p}.sty" for p in base_packages(base)]


def base_definiciones(base: str) -> str:
    """Las definiciones de macros de la base tal cual (para compilar o convertir fuera de Beamer)."""
    return "\n".join(l for l in base.splitlines()
                     if re.match(r"\s*\\(newcommand|renewcommand|providecommand|DeclareMathOperator)", l))


def base_macros(base: str) -> str:
    """Lista legible de las macros definidas en la base, para informárselas al modelo."""
    out = []
    pat = (r"\\(?:newcommand|renewcommand|providecommand)\*?\{?\\([A-Za-z]+)\}?(?:\[(\d)\])?\{(.*)\}\s*$"
           r"|\\DeclareMathOperator\*?\{\\([A-Za-z]+)\}\{(.*)\}\s*$")
    for m in re.finditer(pat, base, re.M):
        if m.group(1):
            n = m.group(2) or "0"
            out.append(f"\\{m.group(1)} ({n} args) = {m.group(3)}")
        else:
            out.append(f"\\{m.group(4)} (operador) = {m.group(5)}")
    return "; ".join(out) or "ninguna"


def fill_base(base: str, o: dict | None) -> str:
    """Rellena <<TITLE>>, <<AUTHORS>>, <<VENUE>> si siguen en la base."""
    vals = {"TITLE": "Título", "AUTHORS": "Autores", "VENUE": ""}
    if o:
        vals = {"TITLE": o["title"], "AUTHORS": o["authors"], "VENUE": o["venue"]}
    for k, v in vals.items():
        base = base.replace(f"<<{k}>>", v)
    return base


def split_base(base: str, o: dict) -> tuple[str, str]:
    head, tail = fill_base(base, o).split(SLIDES_MARK)
    return head.rstrip() + "\n", "\n" + tail.lstrip()


def standalone(head: str, frame: str) -> tuple[str, int]:
    """Documento mínimo para validar un frame: cabecera de la base + frame + cierre."""
    return head + frame + "\n\\end{document}\n", head.count("\n")


def load_base(state: State) -> str:
    return Path(state.get("base_path") or DEFAULT_BASE).read_text()

# ----------------------------------------------------------------------------
# Nodos del grafo principal
# ----------------------------------------------------------------------------
def indice_tablas(chunks: dict[str, str]) -> str:
    """Título y encabezados de cada tabla: permite notar cifras atribuidas a otra tabla."""
    out = []
    for cid, txt in chunks.items():
        if not cid.startswith("tab"):
            continue
        lines = [l for l in txt.splitlines() if l.strip()]
        head = lines[0][:200] if lines else ""
        if TABLA_DANADA in txt:
            out.append(f"[{cid}] {head} (dañada: sin contenido)")
            continue
        rows = [l for l in lines[1:] if l.startswith("|") and not re.fullmatch(r"[|\-: ]+", l)]
        if len(lines) > 1 and lines[1].startswith("Encabezados:"):   # inventario de la lectura
            out.append(f"[{cid}] {head}\n    " + "\n    ".join(lines[1:3]))
            continue
        cols = " / ".join(r[:160] for r in rows[:2]) or " ".join(lines[1:3])[:300]
        out.append(f"[{cid}] {head}\n    encabezados: {cols}")
    return "\n".join(out) or "(el paper no tiene tablas)"


def pie_corto(caption: str, n: int = 300) -> str:
    """Pie de figura sin el rótulo «Fig. N», cortado en el fin de una frase (o de una palabra)."""
    t = re.sub(r"^(?:Fig\.?|Figure)\s*\d+\s*[.:|]?\s*", "", caption.strip())
    if len(t) <= n:
        return t
    corte = t.rfind(". ", 0, n)
    return t[:corte + 1] if corte > 40 else t[:t.rfind(" ", 0, n)] + "…"


def ingest(state: State) -> dict:
    preflight(load_base(state))
    text, fmt = extract_text(Path(state["source_path"]), state.get("extractor", "pymupdf"))
    chunks = chunk_document(text, fmt)
    if not chunks:
        raise RuntimeError("No se pudo extraer texto del documento")
    out = {"chunks": chunks, "texto": text, "inicio": time.time()}
    path = Path(state["source_path"])
    dest = (Path(state["out_dir"]) if state.get("out_dir") else Path(tempfile.mkdtemp())) / "figuras"
    dest.mkdir(parents=True, exist_ok=True)
    out["figuras_dir"] = str(dest)
    if path.suffix == ".pdf":
        for n, f in extraer_figuras(path, dest).items():
            chunks[f"fig{n}"] = (f"Figure {n} (pág. {f['pagina']}): {pie_corto(f['caption'])}\n"
                                 f"[archivo: {f['archivo']}]")
        out["figuras_dir"] = str(dest)
        import pymupdf
        with pymupdf.open(path) as doc:          # capa de texto plana: la extracción en markdown
            plano = "\n".join(pg.get_text() for pg in doc)   # trunca celdas de tablas rotadas
        out["texto"] = text + "\n\n" + plano     # (se usa para validar la lectura y las cifras)
    for n, extra in enumerate(state.get("extras") or [], 1):
        et, efmt = extract_text(Path(extra), state.get("extractor", "pymupdf"))
        chunks.update(fragmentos_extra(et, efmt, n, Path(extra).name))
        out["texto"] += "\n\n" + et
    return out


def fragmentos_extra(texto: str, fmt: str, n: int, nombre: str) -> dict[str, str]:
    """Fragmentos de una fuente adicional con ids x{n}sec1, x{n}tab1…, marcados con su archivo.
    Van siempre como texto (en modo claude no se leen como imagen) y no aportan figuras."""
    base = chunk_document(texto, fmt)
    ren = {c: f"x{n}{c}" for c in base}
    out = {}
    for c, t in base.items():
        t = re.sub(r"\[(sec|tab|alg|eq)(\d+)\]", lambda m: f"[x{n}{m.group(1)}{m.group(2)}]", t)
        out[ren[c]] = f"[Fuente adicional: {nombre}] {t}"
    return out


def asegurar_portada(o: Outline, lim: dict) -> Outline:
    """La presentación empieza con portada y, si se pide, la agenda (ambas sin LLM)."""
    fixed = [s for s in o.slides if s.kind in FIXED_KINDS]
    rest = [s for s in o.slides if s.kind not in FIXED_KINDS]
    title = next((s for s in fixed if s.kind == "title"), SlideSpec(title=o.title, bullets=[], kind="title"))
    slides = [title]
    if lim.get("agenda", True):
        slides.append(SlideSpec(title="Contenido", bullets=[], kind="agenda"))
    o.slides = slides + rest
    return o


def _tokens(t: str) -> set[str]:
    # NFKC: la capa de texto del PDF trae ligaduras (ﬁ, ﬂ) que partirían las palabras
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", unicodedata.normalize("NFKC", t))   # cortes de línea con guion
    return {w for w in re.split(r"[^0-9a-záéíóúñ]+", t.lower()) if len(w) > 1}


def validar_lectura(le: Lectura, texto: str, n_paginas: int) -> list[str]:
    """Chequeo determinista del inventario contra la capa de texto del PDF: lo que Claude
    dice haber visto (páginas, números de tabla, encabezados, métodos) tiene que existir."""
    errs, vocab = [], _tokens(texto)
    items = [("Tabla", t.numero, t.pagina) for t in le.tablas] + \
            [(e.id, e.id, e.pagina) for e in le.algoritmos + le.ecuaciones]
    for nombre, num, pag in items:
        if n_paginas and not 1 <= pag <= n_paginas:
            errs.append(f"{nombre} {num}: página {pag} fuera de 1..{n_paginas}")
    nums = [t.numero for t in le.tablas]
    if len(nums) != len(set(nums)):
        errs.append(f"Números de tabla repetidos: {nums}")
    for t in le.tablas:
        if not re.search(rf"\bTable\s+{t.numero}\b|\bTabla\s+{t.numero}\b", texto):
            errs.append(f"No existe 'Table {t.numero}' en el texto del paper")
        falt = sorted(_tokens(" ".join(t.encabezados + t.metodos)) - vocab)
        if falt:
            errs.append(f"Tabla {t.numero}: encabezados/métodos que no están en el paper: {falt}; "
                        "escríbelos tal como aparecen")
    return errs


def lectura(state: State) -> dict:
    """Modo claude: Claude lee el paper como imagen y deja un inventario verificable; las
    tablas/algoritmos/ecuaciones pasan a ser referencias a páginas, no texto extraído."""
    if PROVIDER != "claude":
        return {}
    chunks, texto, path = dict(state["chunks"]), state.get("texto", ""), Path(state["source_path"])
    n_pag = 0
    if path.suffix == ".pdf":
        import pymupdf
        with pymupdf.open(path) as doc:
            n_pag = doc.page_count
    secs = "\n".join(f"  - {c}: {t.splitlines()[0][:80]}" for c, t in chunks.items() if c.startswith("sec"))
    feedback, errs = "", []
    for _ in range(MAX_OUTLINE_ATTEMPTS):
        le = call_structured(MODEL_OUTLINE, Lectura, prompt=LECTURA_PROMPT.format(
            paper=path, n_paginas=n_pag or "?", secciones=secs, feedback=feedback))
        errs = validar_lectura(le, texto, n_pag)
        if not errs:
            break
        feedback = "\nCORRIGE estos problemas del intento anterior:\n- " + "\n- ".join(errs) + "\n"
    else:
        raise RespuestasAgotadas(f"Lectura inválida tras {MAX_OUTLINE_ATTEMPTS} intentos: {errs}")
    out = {c: t for c, t in chunks.items() if c.startswith(("sec", "fig", "x"))}
    pags = {sc.id: sc.paginas for sc in le.secciones}
    for c in out:
        if c in pags:
            first, _, rest = out[c].partition("\n")
            out[c] = f"{first} (págs. {pags[c]})\n{rest}"
    for t in le.tablas:
        out[f"tab{t.numero}"] = (f"Table {t.numero} (pág. {t.pagina}): {t.titulo}\n"
                                 f"Encabezados: {' | '.join(t.encabezados)}\n"
                                 f"Compara: {', '.join(t.metodos)}\nMide: {t.que_mide}")
    for e in le.algoritmos + le.ecuaciones:
        out[e.id] = f"{e.titulo} (pág. {e.pagina})" + (f"\n{e.latex}" if e.latex else "")
    return {"chunks": out}


def fuentes_claude(chunks: dict[str, str], ids) -> str:
    """Modo claude: referencias compactas en vez del texto de los fragmentos."""
    lines = ["(Modo Claude: leíste el paper como imagen. Estas son las referencias de las fuentes; "
             "apóyate en lo que viste y vuelve a mirar esas páginas si no las tienes presentes.)"]
    for c in ids:
        t = chunks.get(c, "")
        lines.append(f"[{c}] " + (t.splitlines()[0] if c.startswith("sec") else t))
    return "\n".join(lines)


def ajustar_kinds(o: Outline, chunks: dict | None = None) -> Outline:
    chunks = chunks or {}
    for s in o.slides:
        if s.kind == "bullets":
            for pref, kind in BULLETS_UPGRADE.items():
                if any(re.fullmatch(pref + r"\d+", c) and TABLA_DANADA not in chunks.get(c, "")
                       for c in s.sources):
                    s.kind = kind
                    break
    return o


def validate_outline(o: Outline, chunks: dict, lim: dict | None = None) -> list[str]:
    errs = []
    lim = lim or STYLE_DEFAULTS
    for i, s in enumerate(o.slides):
        if s.kind not in FIXED_KINDS and s.kind not in kinds_disponibles():
            errs.append(f"Diapositiva {i} ('{s.title}') tiene kind='{s.kind}' inválido; "
                        f"usa uno de: {', '.join(kinds_disponibles())}")
            continue
        if len(s.bullets) > lim["max_items"]:
            errs.append(f"Diapositiva {i} ('{s.title}') tiene {len(s.bullets)} puntos; "
                        f"máximo {lim['max_items']}: divídela")
        bad = [c for c in s.sources if c not in chunks]
        if bad:
            errs.append(f"Diapositiva {i} ('{s.title}') cita fragmentos inexistentes: {bad}")
        if s.kind not in FIXED_KINDS and not s.sources:
            errs.append(f"Diapositiva {i} ('{s.title}') no tiene sources")
    secs = lim.get("secciones") or []
    content = [(i, s) for i, s in enumerate(o.slides) if s.kind not in FIXED_KINDS]
    if secs:
        bad = [f"{i} ('{s.title}'): '{s.section}'" for i, s in content if s.section not in secs]
        if bad:
            errs.append(f"Diapositivas sin sección válida {bad}; usa una de: {', '.join(secs)}")
    else:                                   # secciones libres: las decide el guion
        bad = [f"{i} ('{s.title}')" for i, s in content if not s.section.strip()]
        if bad:
            errs.append(f"Diapositivas sin sección válida {bad}: ponle una sección a cada diapo")
        orden = [s.section for _, s in content if s.section.strip()]
        vistas = list(dict.fromkeys(orden))
        if len(vistas) > MAX_SECCIONES:
            errs.append(f"{len(vistas)} secciones; máximo {MAX_SECCIONES}: agrupa más")
        tramos = [k for k, _ in itertools.groupby(orden)]         # secciones seguidas, en orden
        partidas = [x for x in vistas if tramos.count(x) > 1]
        if partidas:
            errs.append(f"Las secciones {partidas} aparecen partidas: cada sección debe agrupar diapos seguidas")
    return errs


MAX_SECCIONES = 7


def regla_secciones(lim: dict) -> str:
    secs = lim.get("secciones") or []
    if secs:
        return (f"Una de: {', '.join(secs)}. Respeta ese orden y dale a cada sección al menos una "
                "diapo si el paper tiene contenido para ella.")
    return (f"Elige tú las secciones según el paper y las instrucciones: de 3 a {MAX_SECCIONES - 2}, con "
            "nombres breves (p. ej. Introducción, Propuesta, Experimentos, Conclusiones, u otros que le "
            "calcen mejor al paper). Cada sección agrupa diapos seguidas: no vuelvas a una anterior.")


def bloque_instrucciones(state: dict) -> str:
    """Instrucciones de quien presenta (público, duración, énfasis, idioma, secciones...)."""
    t = (state.get("instrucciones") or "").strip()
    if not t:
        return ""
    return ("INSTRUCCIONES DE QUIEN PRESENTA (mandan sobre la guía de estilo en contenido, público, "
            "énfasis, idioma y secciones; no sobre el formato LaTeX ni los límites que se verifican):\n"
            + t + "\n\n")


def variedad_outline(o: Outline, lim: dict) -> list[str]:
    """Aviso (no bloquea): demasiadas diapos de solo viñetas."""
    content = [s for s in o.slides if s.kind not in FIXED_KINDS]
    n_bul = sum(s.kind == "bullets" for s in content)
    if len(content) >= 4 and n_bul > lim["max_fraccion_vinetas"] * len(content):
        return [f"{n_bul} de {len(content)} diapositivas son solo viñetas; máximo "
                f"{lim['max_fraccion_vinetas']:.0%}. Usa columns, block, equation o table"]
    return []


def outline(state: State) -> dict:
    chunks = state["chunks"]
    if state.get("outline_path"):                    # guion revisado por una persona
        return cargar_guion(state, json.loads(Path(state["outline_path"]).read_text()))
    listing = (fuentes_claude(chunks, chunks) if PROVIDER == "claude"
               else "\n\n".join(f"[{cid}]\n{txt}" for cid, txt in chunks.items()))
    base = load_base(state)
    guide, lim = load_style(state.get("style_path"))
    feedback = ""
    for attempt in range(1, MAX_OUTLINE_ATTEMPTS + 1):
        o = call_structured(MODEL_OUTLINE, Outline, effort=REASONING_OUTLINE, prompt=OUTLINE_PROMPT.format(
            kinds="\n".join(f"  - {k}: {d}" for k, (d, _, _) in kinds_disponibles().items()),
            regla_secciones=regla_secciones(lim), instrucciones=bloque_instrucciones(state),
            nmin=N_SLIDES[0], nmax=N_SLIDES[1], chunks=listing, feedback=feedback,
            macros=base_macros(base), guide="\n".join(filter(None, [guide, guia_guion(state.get("style_path"))])) or "-",
            limits=describe_limits(lim, guion=True)))
        o = asegurar_portada(ajustar_kinds(o, chunks), lim)
        errs = validate_outline(o, chunks, lim)
        soft = variedad_outline(o, lim)
        if not errs and (not soft or attempt == MAX_OUTLINE_ATTEMPTS):
            for w in soft:
                print(f"Aviso del guion: {w}")
            d = o.model_dump()
            head, tail = split_base(base, d)
            return {"outline": d, "head": head, "tail": tail}
        feedback = "\nCORRIGE estos problemas del intento anterior:\n- " + "\n- ".join(errs + soft) + "\n"
    raise RespuestasAgotadas(f"Outline inválido tras {MAX_OUTLINE_ATTEMPTS} intentos: {errs}")


def cargar_guion(state: State, data: dict) -> dict:
    lim = load_style(state.get("style_path"))[1]
    o = asegurar_portada(ajustar_kinds(Outline.model_validate(data), state["chunks"]), lim)
    errs = validate_outline(o, state["chunks"], lim)
    if errs:
        raise RuntimeError(f"El guion editado no es válido: {errs}")
    d = o.model_dump()
    head, tail = split_base(load_base(state), d)
    return {"outline": d, "head": head, "tail": tail}


def avisos_guion(outline: dict, chunks: dict) -> dict[int, list[str]]:
    """Dudas del modelo + chequeo en código: métodos que la diapo menciona (\\texttt{...})
    y que no aparecen en ninguna de las tablas que cita, o tablas citadas que están dañadas."""
    flat = lambda t: re.sub(r"[|\s*_]+", "", t).lower()
    out: dict[int, list[str]] = {}
    for i, sl in enumerate(outline["slides"]):
        ws = [f"Duda del guion: {sl['aviso']}"] if sl.get("aviso") else []
        tabs = [c for c in sl["sources"] if c.startswith("tab") and c in chunks]
        ws += [f"{c} está dañada: sus cifras deben salir del texto, verifícalas"
               for c in tabs if TABLA_DANADA in chunks[c]]
        ok_tabs = [c for c in tabs if TABLA_DANADA not in chunks[c]]
        if ok_tabs:
            text = " ".join([sl["title"], *sl["bullets"]])
            names = {n for n in re.findall(r"\\texttt\{([^}]+)\}", text)}
            tab_text = flat(" ".join(chunks[c] for c in ok_tabs))
            missing = sorted(n for n in names if flat(n) not in tab_text)
            if missing:
                heads = "; ".join(indice_tablas({c: chunks[c]}).split("encabezados: ")[-1][:120]
                                  for c in ok_tabs)
                ws.append(f"Menciona {', '.join(missing)}, que no aparecen en {', '.join(ok_tabs)} "
                          f"(encabezados: {heads}): ¿es la tabla correcta? Si lo es, las cifras de "
                          f"esos métodos deben salir del texto")
        if ws:
            out[i] = ws
    return out


_META = {"Título": "title", "Autores": "authors", "Revista": "venue", "Notación": "notation"}


def _esc_md(t: str) -> str:
    """Escapa lo que un editor de markdown tomaría como formato (* _) y deja cada fórmula
    $…$ como código en línea (los editores no aceptan matemática en línea), para el ida y vuelta."""
    parts = re.split(r"(\$[^$]+\$)", t)
    return "".join(f"`{x}`" if x.startswith("$") and x.endswith("$") and len(x) > 1
                   else re.sub(r"([\\*_])", r"\\\1", x) for x in parts)


MARCA_CRUDO = "<!-- guion en markdown crudo: el LaTeX va tal cual y las fórmulas entre $…$ -->"


def guion_doc_md(outline: dict, chunks: dict, crudo: bool = False) -> str:
    """Guion como documento editable, pensado para editarse a mano y volver a leerse con
    guion_desde_md. crudo=False: para Claude Docs (escapa * _ \ y pone las fórmulas como
    código, porque el editor no acepta matemática en línea). crudo=True: para editarlo como
    archivo (VS Code, GitHub): LaTeX tal cual y fórmulas $…$, que la vista previa muestra."""
    lim = STYLE_DEFAULTS
    _esc_md_ = (lambda t: t) if crudo else _esc_md
    out = ([MARCA_CRUDO, ""] if crudo else []) + [f"# Guion: {_esc_md_(outline['title'])}", ""]
    for k, v in _META.items():
        out += [f"{k}: {_esc_md_(outline[v])}", ""]
    out += ["", "Edita libremente: textos, viñetas, y borra o mueve secciones «## N. …». Mantén en cada "
            "diapo la línea «Sección: … · Tipo: … · Fuentes: …». Secciones: "
            + (", ".join(lim["secciones"]) if lim["secciones"] else "las que quieras (una por diapo; "
               "cada sección agrupa diapos seguidas)") + ". Tipos: " + ", ".join(KINDS) + ". Fuentes: ids de la "
            "lista de abajo. La portada y la agenda se agregan solas."
            + (" Escribe el LaTeX tal cual; las fórmulas, entre $…$." if crudo else ""), ""]
    avisos = avisos_guion(outline, chunks)
    if avisos:
        out += ["## ⚠ Revisar primero", ""]
        out += [f"- Diapo {i} ({_esc_md_(outline['slides'][i]['title'])}): {_esc_md_(w)}"
                for i, ws in avisos.items() for w in ws]
        out.append("")
    out += ["## Fuentes disponibles", ""]
    out += [f"- {c}: {_esc_md_(re.sub(r'[*_]{2,}', '', t.splitlines()[0]))}" for c, t in chunks.items()]
    out.append("")
    for i, sl in enumerate(outline["slides"]):
        if sl["kind"] in FIXED_KINDS:
            continue
        out += [f"## {i}. {_esc_md_(sl['title'])}", ""]
        out.append(f"Sección: {sl.get('section') or '-'} · Tipo: {sl['kind']} · Fuentes: {', '.join(sl['sources'])}")
        out.append("")
        if sl.get("aviso"):
            out += [f"Aviso: {_esc_md_(sl['aviso'])}", ""]
        out += [f"- {_esc_md_(b)}" for b in sl["bullets"]]
        out.append("")
    return "\n".join(out)


def _desescapar_md(t: str) -> str:
    """Deshace los escapes de markdown (\\_ \\* \\# ...) fuera del código en línea, que se toma
    literal (ahí van las fórmulas $…$)."""
    parts = re.split(r"(`[^`]*`)", t)
    return "".join(x[1:-1] if x.startswith("`") and x.endswith("`") and len(x) > 1
                   else re.sub(r"\\([\\_*#\[\]()`~>|!+.\-{}&])", r"\1", x) for x in parts).strip()


def guion_desde_md(md: str) -> dict:
    """Inversa de guion_doc_md (reconoce solo si es crudo): el documento editado vuelve a
    ser un guion (dict de Outline)."""
    _desescapar_md_ = (lambda t: t.strip()) if MARCA_CRUDO in md else _desescapar_md
    data = {v: "" for v in _META.values()}
    slides, cur = [], None
    for raw in md.splitlines():
        line = raw.strip()
        m = re.match(r"^#{2,3}\s*\**\s*(\d+)\\?\.\s*(.+?)\**$", line)
        if m:
            cur = {"title": _desescapar_md_(m.group(2)), "bullets": [], "kind": "bullets",
                   "sources": [], "section": "", "aviso": ""}
            slides.append(cur)
            continue
        if line.startswith("#"):                      # otro encabezado: Fuentes, Revisar primero...
            cur = None
            continue
        field = re.match(r"^\**(Título|Autores|Revista|Notación)\**:\s*(.*)$", line)
        if field and cur is None:
            data[_META[field.group(1)]] = _desescapar_md_(field.group(2))
            continue
        if cur is None:
            continue
        if re.match(r"^\**Secci[oó]n\**:", line):
            line, _, aviso = line.partition("Aviso:")        # por si el editor unió las líneas
            if aviso:
                cur["aviso"] = _desescapar_md_(aviso)
            for part in re.split(r"\s*[·|]\s*", line):
                k, _, v = part.partition(":")
                k, v = k.strip("* ").lower(), _desescapar_md_(v)
                if k.startswith("secci"):
                    cur["section"] = "" if v == "-" else v
                elif k == "tipo":
                    cur["kind"] = v
                elif k == "fuentes":
                    cur["sources"] = [x.strip("` ") for x in re.split(r"[,\s]+", v) if x.strip("` ")]
        elif re.match(r"^\**Aviso\**:", line):
            cur["aviso"] = _desescapar_md_(line.split(":", 1)[1])
        elif re.match(r"^([-*+]|\d+[.)])\s+", line):
            cur["bullets"].append(_desescapar_md_(re.sub(r"^([-*+]|\d+[.)])\s+", "", line)))
    return {**data, "slides": slides}


def guion_md(outline: dict, chunks: dict, paper: str) -> str:
    """Vista legible del guion para revisarlo en un PR (lo que se edita es el .json)."""
    titles = {c: chunks[c].splitlines()[0] for c in chunks if c.startswith("tab")}
    out = [f"# Guion: {outline['title']}\n", f"Paper: `{paper}`\n",
           "Revisa el orden, el tipo de cada diapo y sobre todo **qué fragmentos usa** "
           "(`sources`). Para cambiar algo edita el `.json` de este PR; al fusionarlo se "
           "generan las diapositivas.\n", "## Tablas del paper\n"]
    out += [f"- `{c}`: {t}" for c, t in titles.items()] or ["- (ninguna)"]
    out.append("")
    avisos = avisos_guion(outline, chunks)
    if avisos:
        out.append("## ⚠ Revisar primero\n")
        out += [f"- **Diapo {i}** ({outline['slides'][i]['title']}): {w}" for i, ws in avisos.items() for w in ws]
        out.append("")
    for i, sl in enumerate(outline["slides"]):
        out.append(f"## {i}. {sl['title']}  \n`{sl['kind']}` · sección: {sl.get('section') or '-'} · fuentes: "
                   f"{', '.join(f'`{c}`' for c in sl['sources']) or '-'}\n")
        out += [f"> ⚠ {w}" for w in avisos.get(i, [])]
        out += [f"- {b}" for b in sl["bullets"]]
        out.append("")
    return "\n".join(out)


def review_outline(state: State) -> dict:
    """Punto de revisión humana: el guion es lo más barato de corregir."""
    if not state.get("human_review"):
        return {}
    edited = interrupt({"outline": state["outline"], "chunks_tablas": {
        c: t.splitlines()[0] for c, t in state["chunks"].items() if c.startswith("tab")}})
    if isinstance(edited, dict) and edited.get("slides"):
        return cargar_guion(state, edited)
    return {}


def plan_guion(slides: list[dict]) -> str:
    """Títulos del guion por sección: evita repetir a las vecinas. Es igual para todas las
    diapos (cada prompt dice cuál es la suya), así el modo claude lo comparte una sola vez."""
    out, sec = [], None
    for i, sl in enumerate(slides):
        if sl["kind"] in FIXED_KINDS:
            continue
        if (sl.get("section") or "") != sec:
            sec = sl.get("section") or ""
            out.append(f"[{sec or '-'}]")
        out.append(f"  {i}. {sl['title']}")
    return "\n".join(out)


def fan_out(state: State) -> list[Send]:
    o, chunks = state["outline"], state["chunks"]
    base = load_base(state)
    macros, packages = base_macros(base), ", ".join(base_packages(base))
    guide, lim = load_style(state.get("style_path"))
    previos = errores_previos()
    return [Send("slide", {
        "idx": i, "spec": s, "head": state["head"], "notation": o["notation"],
        "macros": macros, "packages": packages, "style_guide": guide or "-", "limits": lim, "context": (fuentes_claude(chunks, s["sources"]) if PROVIDER == "claude"
                    else "\n\n".join(chunks[c] for c in s["sources"])),
        "context_cifras": state.get("texto", "") if PROVIDER == "claude" else "",
        "damaged": [c for c in s["sources"] if TABLA_DANADA in chunks[c]],
        "figuras_dir": state.get("figuras_dir", ""),
        "tables": indice_tablas(chunks), "plan": plan_guion(o["slides"]),
        "previos": previos, "paper": state.get("source_path", ""),
        "instrucciones": bloque_instrucciones(state),
        "attempts": 0, "errors": [], "style_errors": [], "fact_errors": [], "reviews": 0, "best_frame": "",
        "warnings": [],
    }) for i, s in enumerate(map(sin_motor_a_bloque, o["slides"]))]


def sin_motor_a_bloque(s: dict) -> dict:
    """Una diapo diagram sin ningún motor que funcione pasa a block (con aviso), en vez de
    obligar al modelo a improvisar con TikZ."""
    if s.get("kind") != "diagram" or hay_graphviz() or hay_mermaid():
        return s
    motivo = "; ".join(m for _, m in (probar_motor("mermaid"), probar_motor("graphviz")) if m)
    aviso = f"Se pidió un diagrama, pero no hay motor que funcione ({motivo}): va como bloque."
    return {**s, "kind": "block", "aviso": " ".join(filter(None, [s.get("aviso", ""), aviso]))}


def assemble(state: State) -> dict:
    body, current = [], None
    slides = state["outline"]["slides"]
    for idx, frame, status, _, _ in sorted(state["frames"], key=lambda f: f[0]):
        sec = slides[idx].get("section") or ""
        if slides[idx]["kind"] not in FIXED_KINDS and sec and sec != current:
            body.append(f"\\section{{{sec}}}")
            current = sec
        if status == "failed":
            title = state["outline"]["slides"][idx]["title"]
            frame = (f"% TODO: la diapositiva {idx} no compiló tras {MAX_SLIDE_ATTEMPTS} intentos\n"
                     f"\\begin{{frame}}{{{title}}}\n\\alert{{Diapositiva pendiente de revisión}}\n"
                     f"\\end{{frame}}")
        body.append(expandir_diagramas(frame, state.get("figuras_dir"))[0])
    tex = state["head"] + "\n" + "\n\n".join(body) + "\n" + state["tail"]
    return {"tex": tex, "global_attempts": 0}


def compile_full(state: State) -> dict:
    errors, d = compile_tex(state["tex"], passes=2, ignore_vbox=True, figuras=state.get("figuras_dir"))
    return {"log_errors": errors, "pdf_path": str(d / "doc.pdf")}


def route_full(state: State) -> str:
    if state["log_errors"] and state["global_attempts"] < MAX_GLOBAL_ATTEMPTS:
        return "refine_global"
    return "notas_expositor"


def refine_global(state: State) -> dict:
    head, tail = state["head"], state["tail"]          # la base nunca se modifica
    body = state["tex"][len(head):len(state["tex"]) - len(tail)]
    new_body = call_text(MODEL_SLIDES, REFINE_GLOBAL_PROMPT.format(
        errors="\n".join(state["log_errors"]), body=body))
    new_body = re.sub(r"\\(begin|end)\{document\}", "", new_body)
    return {"tex": head + new_body + tail, "global_attempts": state["global_attempts"] + 1}


MAX_NOTAS_ATTEMPTS = 2


def diapos_con_nota(state: State) -> list[tuple[int, str]]:
    """(idx, frame) de las diapos que llevan nota: todas menos la agenda y las que no compilaron."""
    slides = state["outline"]["slides"]
    return [(idx, frame) for idx, frame, status, _, _ in sorted(state["frames"], key=lambda f: f[0])
            if status != "failed" and slides[idx]["kind"] != "agenda"]


def validar_notas(n: Notas, esperadas: list[int]) -> list[str]:
    dadas = {x.diapo: x.texto.strip() for x in n.notas}
    errs = [f"falta la nota de la diapositiva [{i}]" for i in esperadas if not dadas.get(i)]
    errs += [f"no hay diapositiva [{i}]" for i in dadas if i not in esperadas]
    errs += [f"la nota de [{i}] trae LaTeX ({m.group(0)}): escríbela en texto plano"
             for i, t in dadas.items() if i in esperadas
             for m in [re.search(r"\\[A-Za-z]+|\$[^$]+\$", t)] if m]
    return errs


def notas_expositor(state: State) -> dict:
    """--notas: el guion de quien presenta, una nota por diapo, a partir de los frames finales.
    No toca las diapos; las notas van a notas.md, a \\note{} en el .tex y a las notas del .pptx."""
    if not state.get("notas_expositor"):
        return {}
    diapos = diapos_con_nota(state)
    esperadas = [i for i, _ in diapos]
    o = state["outline"]
    listado = "\n\n".join(f"[{i}]\n" + (f"Portada: {o['title']} ({o['authors']}; {o['venue']})"
                                          if o["slides"][i]["kind"] == "title" else fr) for i, fr in diapos)
    feedback, n = "", Notas(notas=[])
    for _ in range(MAX_NOTAS_ATTEMPTS):
        n = call_structured(MODEL_SLIDES, Notas, NOTAS_PROMPT.format(
            instrucciones=bloque_instrucciones(state), guide=guia_notas(state.get("style_path")) or "-",
            notation=o.get("notation", "-"), feedback=feedback, diapos=listado))
        errs = validar_notas(n, esperadas)
        if not errs:
            break
        feedback = "\nCORRIGE estos problemas del intento anterior:\n- " + "\n- ".join(errs) + "\n"
    notas = {str(x.diapo): x.texto.strip() for x in n.notas
             if x.diapo in esperadas and x.texto.strip()}
    # cifras que no están en su diapo ni en las fuentes: aviso, como en las diapos
    fuentes = state.get("texto") or "\n".join(state.get("chunks", {}).values())
    frames = dict(diapos)
    avisos = [f"Nota de la diapo {i}: {w}" for i, t in notas.items()
              for w in soft_checks(t, frames[int(i)] + "\n" + fuentes) if w.startswith("Cifra")]
    avisos += [f"Nota de la diapo {i}: falta" for i in esperadas if str(i) not in notas]
    return {"notas": notas, "avisos_notas": avisos}


def nota_a_latex(t: str) -> str:
    """Texto plano → argumento de \\note{} (se escapan los caracteres especiales de LaTeX)."""
    esp = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
           "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(esp.get(c, c) for c in t)


def nota_desde_latex(t: str) -> str:
    for a, b in ((r"\textbackslash{}", "\\"), (r"\textasciitilde{}", "~"), (r"\textasciicircum{}", "^")):
        t = t.replace(a, b)
    return re.sub(r"\\([&%$#_{}])", r"\1", t).strip()


def separar_nota(frame: str) -> tuple[str, str]:
    """frame con \\note{…} → (frame sin la nota, texto de la nota)."""
    m = re.search(r"\\note\s*\{", frame)
    if not m:
        return frame, ""
    depth, j = 1, m.end()
    while j < len(frame):
        if frame[j] == "\\":                     # \{ \} \\ escapados: no cuentan
            j += 2
            continue
        depth += {"{": 1, "}": -1}.get(frame[j], 0)
        if depth == 0:
            return (frame[:m.start()].rstrip() + "\n" + frame[j + 1:].lstrip(),
                    nota_desde_latex(frame[m.end():j]))
        j += 1
    return frame, ""


def tex_con_notas(tex: str, idxs: list[int], notas: dict) -> str:
    """Agrega \\note{} al final de cada frame del cuerpo (idxs: el idx de cada frame, en orden).
    Beamer no muestra las notas salvo que la base lo pida (\\setbeameroption{show notes})."""
    frames = list(re.finditer(r"\\end\{frame\}", tex))
    if not notas or len(frames) != len(idxs):
        return tex
    out, prev = [], 0
    for m, idx in zip(frames, idxs):
        out.append(tex[prev:m.start()])
        if notas.get(str(idx)):
            out.append(f"\\note{{{nota_a_latex(notas[str(idx)])}}}\n")
        prev = m.start()
    return "".join(out) + tex[prev:]


def notas_md(state: State) -> str:
    slides, notas = state["outline"]["slides"], state.get("notas", {})
    out = [f"# Notas del expositor: {_texto_titulo(state['outline'].get('title', ''))}\n"]
    for idx, frame in diapos_con_nota(state):
        if str(idx) in notas:
            titulo = titulo_frame(frame) or slides[idx]["title"]
            out.append(f"## {idx}. {_texto_titulo(titulo) if slides[idx]['kind'] != 'title' else 'Portada'}\n\n"
                       f"{notas[str(idx)]}\n")
    return "\n".join(out)


def _texto_titulo(t: str) -> str:
    """Título LaTeX legible en Markdown (sin comandos ni llaves)."""
    t = re.sub(r"\\(?:alert|textbf|emph|texttt|textit)\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\[A-Za-z]+\*?", "", t)
    return " ".join(t.replace("{", "").replace("}", "").replace("~", " ").split())


def informe_uso() -> list[str]:
    if not USO:
        return []
    lines = ["\n## Consumo de tokens\n", "| Modelo | Llamadas | Entrada | Salida | Total |",
             "|---|---|---|---|---|"]
    tot = {"llamadas": 0, "entrada": 0, "salida": 0}
    for model, d in sorted(USO.items()):
        lines.append(f"| {model} | {d['llamadas']} | {d['entrada']:,} | {d['salida']:,} | "
                     f"{d['entrada'] + d['salida']:,} |")
        tot = {k: tot[k] + d[k] for k in tot}
    lines.append(f"| **Total** | {tot['llamadas']} | {tot['entrada']:,} | {tot['salida']:,} | "
                 f"{tot['entrada'] + tot['salida']:,} |")
    return lines


def titulo_frame(frame: str) -> str:
    """Título de \\begin{frame}[opciones]{título}, con llaves anidadas; "" si no tiene."""
    m = re.search(r"\\begin\{frame\}\s*(?:\[[^\]]*\]\s*)?\{", frame)
    if not m:
        return ""
    depth, i = 1, m.end()
    for j in range(i, len(frame)):
        depth += {"{": 1, "}": -1}.get(frame[j], 0)
        if depth == 0:
            return " ".join(frame[i:j].split())
    return ""


def exportar_pptx(state: State, out: Path) -> str:
    """presentacion.pptx a partir de los frames finales (ver pptx_export). Un fallo no detiene el
    pipeline: el PDF de Beamer ya está y el informe dice qué pasó."""
    slides = state["outline"]["slides"]
    frames, notas = [], []
    for idx, frame, status, _, _ in sorted(state["frames"], key=lambda f: f[0]):
        notas.append((state.get("notas") or {}).get(str(idx), ""))
        if status == "failed":
            frame = f"\\begin{{frame}}{{{slides[idx]['title']}}}\nDiapositiva pendiente de revisión\n\\end{{frame}}"
        frames.append((slides[idx]["kind"], expandir_diagramas(frame, state.get("figuras_dir"))[0]))
    try:
        import pptx_export
        destino = pptx_export.exportar(
            frames, state["outline"], out / "presentacion.pptx", plantilla=Path(state["pptx"]),
            figuras=Path(state["figuras_dir"]) if state.get("figuras_dir") else None,
            pdf_beamer=out / "presentacion.pdf", macros=base_definiciones(load_base(state)), notas=notas)
        return (f"PowerPoint: `{destino.name}` (plantilla `{Path(state['pptx']).name}`; las fórmulas son "
                "ecuaciones editables en PowerPoint; LibreOffice y Google Slides muestran una imagen)")
    except Exception as e:                       # noqa: BLE001
        return f"PowerPoint: no se pudo generar ({type(e).__name__}: {e})"


def write_outputs(state: State) -> dict:
    out = Path(state["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    notas = state.get("notas") or {}
    idxs = [f[0] for f in sorted(state["frames"], key=lambda f: f[0])]
    (out / "presentacion.tex").write_text(tex_con_notas(state["tex"], idxs, notas))
    if notas:
        (out / "notas.md").write_text(notas_md(state))
    pdf = Path(state["pdf_path"])
    if pdf.exists():
        shutil.copy(pdf, out / "presentacion.pdf")
    (out / "outline.json").write_text(json.dumps(state["outline"], ensure_ascii=False, indent=2))
    lines = ["# Informe de generación\n",
             f"Compilación global: {'OK' if not state['log_errors'] else 'con errores'} "
             f"({state['global_attempts']} refinados globales)\n"]
    lines += [f"- {e}" for e in state["log_errors"]]
    av = avisos_guion(state["outline"], state.get("chunks", {}))
    if av:
        lines.append("\n## Avisos del guion (revisar las tablas usadas)\n")
        lines += [f"- Diapo {i}: {w}" for i, ws in av.items() for w in ws]
    lines.append("\n| # | Diapositiva | Estado | Intentos | Avisos |\n|---|---|---|---|---|")
    for idx, frame, status, attempts, warns in sorted(state["frames"], key=lambda f: f[0]):
        # el título de la diapo final: un refinado pudo acortar el del guion
        title = ((status != "failed" and titulo_frame(frame))
                 or state["outline"]["slides"][idx]["title"]).replace("|", "/")
        warns = [" ".join(w.replace("|", "/").split()) for w in warns]   # una celda de tabla md
        lines.append(f"| {idx} | {title} | {status} | {attempts} | {'<br>'.join(warns) or '-'} |")
    del_run = [r for r in leer_errores(state.get("inicio", time.time()))
               if r.get("paper") == Path(state.get("source_path", "")).name]
    if del_run:
        from collections import Counter
        lines.append("\n## Errores que detectaron los validadores (y se corrigieron o quedaron como aviso)\n")
        lines += [f"- {c} ({k})" for c, k in Counter(r["categoria"] for r in del_run).most_common()]
        lines.append(f"\nSe acumulan en `{ERRORES_LOG}`; los frecuentes se avisan en el prompt de las "
                     "próximas ejecuciones.")
    if state.get("notas_expositor"):
        lines.append(f"\nNotas del expositor: `notas.md` ({len(notas)} diapositivas; también como "
                     "\\note{} en `presentacion.tex`" + (" y en las notas del .pptx" if state.get("pptx") else "")
                     + ")")
        lines += [f"- {w}" for w in state.get("avisos_notas", [])]
    if state.get("pptx"):
        lines.append("\n" + exportar_pptx(state, out))
    lines += informe_uso()
    (out / "informe.md").write_text("\n".join(lines) + "\n")
    return {}

# ----------------------------------------------------------------------------
# Nodos del subgrafo por diapositiva
# ----------------------------------------------------------------------------
def write_slide(s: SlideState) -> dict:
    spec = s["spec"]
    if spec["kind"] in FIXED_KINDS:  # deterministas: no hace falta LLM
        return {"frame": TITLE_FRAME if spec["kind"] == "title" else AGENDA_FRAME}
    frame = call_text(MODEL_SLIDES, SLIDE_PROMPT.format(
        macros=s["macros"], packages=s["packages"], guide=s["style_guide"],
        limits=describe_limits(s["limits"]), notation=s["notation"], kind=spec["kind"],
        kind_desc=KINDS[spec["kind"]][0], kind_example=ejemplo_kind(spec["kind"]), title=spec["title"],
        diagramas=regla_diagramas(),
        bullets="\n".join(f"- {b}" for b in spec["bullets"]), context=s["context"],
        section=spec.get("section") or "-", plan=s.get("plan") or "-", num=s["idx"],
        previos=(s["previos"] + "\n\n") if s.get("previos") else "",
        instrucciones=s.get("instrucciones", ""),
        aviso=(f"Aviso del guion (tenlo en cuenta al elegir las cifras): {spec['aviso']}\n"
               if spec.get("aviso") else "")))
    return {"frame": frame}


def compile_slide(s: SlideState) -> dict:
    # los diagramas DOT se dibujan antes; lint y estilo miran el frame sin el código DOT
    frame_tex, diag_err, diag_avisos = expandir_diagramas(s["frame"], s.get("figuras_dir"))
    errors = lint_frame(sin_diagramas(s["frame"])) + diag_err   # barato: antes de llamar a pdflatex
    if not errors:
        tex, offset = standalone(s["head"], frame_tex)
        errors, _ = compile_tex(tex, offset=offset, ignore_vbox=s["spec"]["kind"] in FIXED_KINDS,
                                figuras=s.get("figuras_dir"))
    spec = s["spec"]
    style = [] if spec["kind"] in FIXED_KINDS else (
        kind_check(s["frame"], spec["kind"], spec["sources"], tuple(s.get("damaged", ())))
        + style_check(sin_diagramas(s["frame"]), s["limits"]) + diag_avisos)
    if spec["kind"] not in FIXED_KINDS:
        registrar_errores("compilación", spec["kind"], errors, s.get("paper", ""))
        registrar_errores("formato/estilo", spec["kind"], style, s.get("paper", ""))
    out = {"errors": errors, "style_errors": style}
    if not errors:
        out["best_frame"] = s["frame"]
    return out


def route_slide(s: SlideState) -> str:
    if (s["errors"] or s["style_errors"]) and s["attempts"] < MAX_SLIDE_ATTEMPTS:
        return "refine_slide"
    if s["errors"] or s["spec"]["kind"] in FIXED_KINDS or s.get("reviews", 0) >= MAX_REVIEWS:
        return "finish_slide"
    return "review_slide"                       # compila: se revisan las afirmaciones


def review_slide(s: SlideState) -> dict:
    r = call_structured(MODEL_REVIEW, Review, effort=REASONING_REVIEW,
                        prompt=REVIEW_SLIDE_PROMPT.format(frame=s["frame"], context=s["context"],
                                                          tables=s.get("tables", "-")))
    bad = [f"{c.veredicto}: «{c.afirmacion}» ({c.evidencia})"
           for c in r.claims if c.veredicto in ("no_respaldada", "contradicha")]
    return {"fact_errors": bad, "reviews": s.get("reviews", 0) + 1}


def route_review(s: SlideState) -> str:
    if s["fact_errors"] and s["reviews"] < MAX_REVIEWS:
        return "refine_facts"
    return "finish_slide"


def refine_slide(s: SlideState) -> dict:
    frame = call_text(MODEL_SLIDES, REFINE_SLIDE_PROMPT.format(
        errors="\n".join(s["errors"]) or "ninguno",
        style_errors="\n".join(s["style_errors"]) or "ninguna",
        fact_errors="\n".join(s.get("fact_errors", [])) or "ninguna",
        guide=s["style_guide"], limits=describe_limits(s["limits"]),
        macros=s["macros"], frame=s["frame"], context=s["context"]))
    return {"frame": frame, "attempts": s["attempts"] + 1}


def refine_facts(s: SlideState) -> dict:
    """Corrige afirmaciones: usa el presupuesto del revisor, no el de compilación/estilo."""
    return {"frame": refine_slide(s)["frame"]}


def finish_slide(s: SlideState) -> dict:
    frame, warns = s["frame"], []
    if s["errors"] and s.get("best_frame"):
        # un refinado rompió la compilación: se vuelve a la última versión que compilaba
        frame = s["best_frame"]
        spec = s["spec"]
        style = kind_check(frame, spec["kind"], spec["sources"], tuple(s.get("damaged", ()))) + \
            style_check(frame, s["limits"])
        warns.append("Se conservó la última versión que compilaba (los refinados posteriores fallaron)")
        errors = []
    else:
        style, errors = s["style_errors"], s["errors"]
    status = "failed" if errors else "ok"        # el estilo nunca descarta una diapo que compila
    warns += [f"Compilación: {e}" for e in errors]
    warns += [f"Estilo: {e}" for e in style]
    warns += [f"Revisor: {e}" for e in s.get("fact_errors", [])]
    # modo claude: solo contra el texto del paper (el guion también lo escribe Claude)
    ref = s.get("context_cifras") or (s["context"] + "\n" + json.dumps(s["spec"]))
    warns += soft_checks(frame, ref)
    return {"frames": [(s["idx"], frame, status, s["attempts"], warns)]}

# ----------------------------------------------------------------------------
# Construcción de los grafos
# ----------------------------------------------------------------------------
def build_slide_graph():
    g = StateGraph(SlideState, output_schema=SlideOutput)
    g.add_node("write_slide", write_slide)
    g.add_node("compile_slide", compile_slide)
    g.add_node("refine_slide", refine_slide)
    g.add_node("review_slide", review_slide)
    g.add_node("refine_facts", refine_facts)
    g.add_node("finish_slide", finish_slide)
    g.add_edge(START, "write_slide")
    g.add_edge("write_slide", "compile_slide")
    g.add_conditional_edges("compile_slide", route_slide, ["refine_slide", "review_slide", "finish_slide"])
    g.add_conditional_edges("review_slide", route_review, ["refine_facts", "finish_slide"])
    g.add_edge("refine_facts", "compile_slide")
    g.add_edge("refine_slide", "compile_slide")
    g.add_edge("finish_slide", END)
    return g.compile()


def build_graph(checkpointer=None):
    g = StateGraph(State)
    g.add_node("ingest", ingest)
    g.add_node("lectura", lectura)
    g.add_node("outline", outline)
    g.add_node("review_outline", review_outline)
    g.add_node("slide", build_slide_graph())
    g.add_node("assemble", assemble)
    g.add_node("compile_full", compile_full)
    g.add_node("refine_global", refine_global)
    g.add_node("notas_expositor", notas_expositor)
    g.add_node("write_outputs", write_outputs)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "lectura")
    g.add_edge("lectura", "outline")
    g.add_edge("outline", "review_outline")
    g.add_conditional_edges("review_outline", fan_out, ["slide"])
    g.add_edge("slide", "assemble")
    g.add_edge("assemble", "compile_full")
    g.add_conditional_edges("compile_full", route_full, ["refine_global", "notas_expositor"])
    g.add_edge("refine_global", "compile_full")
    g.add_edge("notas_expositor", "write_outputs")
    g.add_edge("write_outputs", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())

# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def leer_instrucciones(valor: str | None) -> str:
    """--instrucciones acepta el texto o la ruta de un archivo con él."""
    if not valor:
        return ""
    p = Path(valor)
    return p.read_text(errors="replace").strip() if len(valor) < 300 and p.is_file() else valor.strip()


def opciones_fuentes(args) -> dict:
    out = {}
    extras = getattr(args, "extra", None) or []
    for e in extras:
        if not Path(e).is_file():
            raise SystemExit(f"--extra: no existe {e}")
    if extras:
        out["extras"] = [str(Path(e)) for e in extras]
    if getattr(args, "notas", False):
        out["notas_expositor"] = True
    instr = leer_instrucciones(getattr(args, "instrucciones", None))
    if instr:
        out["instrucciones"] = instr
    return out


def tex_a_pptx(tex: Path, plantilla: str | None = None) -> Path:
    """--a-pptx: una presentación Beamer ya compilada (o editada a mano) → .pptx junto a ella.
    Usa figuras/ y el .pdf del mismo nombre si están (el pseudocódigo se recorta del PDF)."""
    import pptx_export
    src = tex.read_text()
    head, _, body = src.partition("\\begin{document}")

    def campo(cmd: str) -> str:
        m = re.search(r"\\" + cmd + r"\{((?:[^{}]|\{[^{}]*\})*)\}", head)
        return m.group(1).strip() if m and "<<" not in m.group(1) else ""
    frames, slides, notas, sec = [], [], [], ""
    for m in re.finditer(r"\\section\*?\{([^}]*)\}|\\begin\{frame\}.*?\\end\{frame\}", body, re.S):
        if m.group(1) is not None:
            sec = m.group(1)
            continue
        fr, nota = separar_nota(m.group(0))
        notas.append(nota)
        kind = "title" if "\\titlepage" in fr else "agenda" if "\\tableofcontents" in fr else "bullets"
        frames.append((kind, fr))
        slides.append({"kind": kind, "section": "" if kind in FIXED_KINDS else sec})
    if not frames:
        raise SystemExit(f"--a-pptx: {tex} no tiene frames")
    outline = {"title": campo("title"), "authors": campo("author"), "venue": campo("subtitle"),
               "slides": slides}
    figuras = tex.parent / "figuras"
    return pptx_export.exportar(
        frames, outline, tex.with_suffix(".pptx"),
        plantilla=Path(plantilla) if plantilla else None,
        figuras=figuras if figuras.is_dir() else None,
        pdf_beamer=tex.with_suffix(".pdf"), macros=base_definiciones(head), notas=notas)


# Lo que genera el pipeline (solo esto se borra al empezar de nuevo; nunca la carpeta entera)
GENERADOS_SALIDA = ["presentacion.tex", "presentacion.pdf", "presentacion.pptx", "informe.md", "notas.md",
                    "outline.json", "outline_borrador.json", "figuras"]
GENERADOS_TRABAJO = ["estado.sqlite", "estado.sqlite-wal", "estado.sqlite-shm", "meta.json",
                     "pendientes.json", "comun.md", "tiempos.jsonl", "guion.md", "guion.json",
                     "guion_doc.md", "error.log", "tareas", "respuestas"]


def limpiar(carpeta: str | Path | None, nombres: list[str]) -> list[str]:
    """Borra de la carpeta los archivos/carpetas generados con esos nombres; devuelve lo borrado."""
    if not carpeta:
        return []
    borrados = []
    for n in nombres:
        p = Path(carpeta) / n
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
        else:
            continue
        borrados.append(n)
    return borrados


def empezar_de_nuevo(args) -> None:
    """Con un paper y sin --reanudar, una ejecución empieza de cero: se borra el trabajo previo
    (estado, tareas, respuestas) y lo generado en la salida, para no mezclar corridas."""
    if not getattr(args, "source", None) or getattr(args, "reanudar", False):
        return
    b = limpiar(getattr(args, "claude", None), GENERADOS_TRABAJO) + limpiar(args.out, GENERADOS_SALIDA)
    if b:
        print(f"Se borró el trabajo previo ({', '.join(b)}); usa --reanudar para continuarlo.")


def opciones_pptx(args) -> dict:
    if not (getattr(args, "pptx", False) or getattr(args, "plantilla", None)):
        return {}
    import pptx_export
    return {"pptx": str(args.plantilla or pptx_export.DEFAULT_PLANTILLA)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", help="paper en .pdf, .tex o .md (con --guion, opcional)")
    ap.add_argument("--out", default="salida")
    ap.add_argument("--base", default=str(DEFAULT_BASE), help="plantilla con %%SLIDES%%")
    ap.add_argument("--estilo", default=str(DEFAULT_STYLE), help="reglas de estilo (TOML)")
    ap.add_argument("--review", action="store_true", help="pausar para revisar el guion")
    ap.add_argument("--extractor", choices=["pymupdf", "marker"], default="pymupdf")
    ap.add_argument("--concurrency", type=int, default=4, help="diapos en paralelo")
    ap.add_argument("--solo-guion", metavar="JSON",
                    help="solo genera el guion y lo guarda en JSON (y una vista .md) para revisarlo")
    ap.add_argument("--guion", metavar="JSON", help="genera las diapos desde un guion ya revisado")
    ap.add_argument("--claude", metavar="DIR",
                    help="modo Claude Code (LLM_PROVIDER=claude): estado y tareas en DIR; "
                         "vuelve a ejecutar con --claude DIR para continuar")
    ap.add_argument("--reanudar", action="store_true",
                    help="con un paper, continúa el trabajo previo en vez de empezar de cero (por "
                         "defecto se borra lo generado antes en --claude DIR y --out)")
    ap.add_argument("--pptx", action="store_true",
                    help="exporta también presentacion.pptx (fórmulas editables) sobre la plantilla")
    ap.add_argument("--plantilla", default=None, metavar="PPTX",
                    help="plantilla de PowerPoint para --pptx (por defecto plantilla.pptx)")
    ap.add_argument("--notas", action="store_true",
                    help="escribe también las notas del expositor (notas.md, \\note{} y notas del .pptx)")
    ap.add_argument("--extra", action="append", default=[], metavar="ARCHIVO",
                    help="fuente adicional (.pdf, .tex, .md, .txt), p. ej. otro paper o notas; repetible")
    ap.add_argument("--instrucciones", metavar="TEXTO|ARCHIVO",
                    help="instrucciones para armar la presentación (público, duración, énfasis, idioma, "
                         "secciones...), como texto o archivo")
    ap.add_argument("--a-pptx", metavar="TEX",
                    help="convierte una presentacion.tex ya generada (o editada) a .pptx y termina")
    ap.add_argument("--resumen-errores", nargs="?", const="", metavar="JSONL",
                    help="resume el registro de errores de los validadores (por defecto "
                         "errores_validacion.jsonl) y termina")
    args = ap.parse_args()
    if args.resumen_errores is not None:
        print(resumen_errores(args.resumen_errores or None))
        return
    if args.a_pptx:
        print(f"PowerPoint: {tex_a_pptx(Path(args.a_pptx), args.plantilla)}")
        return
    args.instrucciones = leer_instrucciones(args.instrucciones)
    if args.claude:
        empezar_de_nuevo(args)
        from driver_claude import run
        try:
            raise SystemExit(run(args))
        except RespuestasAgotadas as e:          # las respuestas, no el código
            print(f"RESPUESTAS AGOTADAS: {e}\nSe rechazaron todos los intentos de esa tarea. Muéstrale "
                  "estos problemas al usuario y detente: para reintentar hay que empezar de nuevo (el "
                  "primer comando, con el paper). No modifiques el código.")
            raise SystemExit(1)
        except Exception as e:                   # fallo del pipeline, no de una respuesta
            import traceback
            log = Path(args.claude) / "error.log"
            log.write_text(traceback.format_exc())
            print(f"ERROR del pipeline: {type(e).__name__}: {e}\n(detalle en {log})\n"
                  "No es un problema de tus respuestas: no modifiques el código; muéstrale este "
                  "error al usuario y detente.")
            raise SystemExit(1)
    guion = json.loads(Path(args.guion).read_text()) if args.guion else {}
    args.source = args.source or guion.get("paper")
    if not args.source:
        ap.error("falta el paper (o un --guion que lo indique en su campo 'paper')")

    if args.solo_guion:
        state = {"source_path": args.source, "base_path": args.base, "style_path": args.estilo,
                 "extractor": args.extractor}
        state.update(ingest(state))
        o = outline(state)["outline"]
        path = Path(args.solo_guion)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"paper": args.source, **o}, ensure_ascii=False, indent=2))
        path.with_suffix(".md").write_text(guion_md(o, state["chunks"], args.source))
        print("\n".join(informe_uso()))
        print(f"Guion: {path} (vista: {path.with_suffix('.md')})")
        return

    empezar_de_nuevo(args)
    graph = build_graph()
    config = {"configurable": {"thread_id": "beamer"}, "max_concurrency": args.concurrency}
    result = graph.invoke({"source_path": args.source, "base_path": args.base, "style_path": args.estilo,
                           "out_dir": args.out, **({"outline_path": args.guion} if args.guion else {}),
                           "extractor": args.extractor, "human_review": args.review,
                           **opciones_pptx(args), **opciones_fuentes(args)}, config)

    while "__interrupt__" in result:
        path = Path(args.out) / "outline_borrador.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result["__interrupt__"][0].value["outline"],
                                   ensure_ascii=False, indent=2))
        input(f"Guion guardado en {path}. Edítalo si quieres y pulsa Enter para continuar... ")
        result = graph.invoke(Command(resume=json.loads(path.read_text())), config)

    print("\n".join(informe_uso()))
    extra = (" y presentacion.pptx" if opciones_pptx(args) else "") + (" (notas en notas.md)" if args.notas else "")
    print(f"Listo: {args.out}/presentacion.pdf{extra} (ver {args.out}/informe.md)")


if __name__ == "__main__":
    main()
