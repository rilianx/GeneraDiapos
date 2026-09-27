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
import os
import json
import operator
import re
import shutil
import subprocess
import tempfile
import threading
import tomllib
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
#   LLM_PROVIDER=openai | anthropic
#   LLM_MODEL_OUTLINE / LLM_MODEL_SLIDES: nombres de modelo de tu cuenta
# El guion es la decisión más importante: usa ahí tu modelo más capaz, y uno más
# rápido/barato para las N diapositivas y los refinados.
PROVIDER = os.environ.get("LLM_PROVIDER") or "openai"
_DEFAULT_MODELS = {"anthropic": ("claude-opus-5-5", "claude-sonnet-5"),
                   "openai": ("gpt-5.4-mini", "gpt-5.4-mini")}
MODEL_OUTLINE = os.environ.get("LLM_MODEL_OUTLINE") or _DEFAULT_MODELS[PROVIDER][0]
MODEL_SLIDES = os.environ.get("LLM_MODEL_SLIDES") or _DEFAULT_MODELS[PROVIDER][1]
# Esfuerzo de razonamiento (solo OpenAI): low | medium | high. Vacío = el del modelo.
# El guion es la decisión que más pesa; las diapos van sin razonamiento extra.
REASONING_OUTLINE = os.environ.get("LLM_REASONING_OUTLINE") or "medium"
REASONING_SLIDES = os.environ.get("LLM_REASONING_SLIDES") or None
MAX_SLIDE_ATTEMPTS = 3
MAX_GLOBAL_ATTEMPTS = 2
MAX_OUTLINE_ATTEMPTS = 3
OVERFULL_TOLERANCE_PT = 2.0
MAX_TEXT_CHARS = 900                # densidad máxima orientativa por diapo
LATEX_TIMEOUT_S = 60
N_SLIDES = (12, 16)

DEFAULT_BASE = Path(__file__).with_name("base.tex")
DEFAULT_STYLE = Path(__file__).with_name("estilo.toml")
STYLE_DEFAULTS = {
    "max_items": 6, "max_palabras_item": 18, "max_anidamiento": 2,
    "max_caracteres_titulo": 60, "max_alerts": 2, "max_negritas": 3,
    "sin_punto_final": False, "prohibir_colores_directos": True,
    "prohibir_vspace_negativo": True, "tamanos_permitidos": [r"\small"],
    "max_fraccion_vinetas": 0.4, "max_bloques": 2, "prohibir_alert_en_alertblock": True,
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
}

KIND_ALIASES = {"alertblock": "block", "exampleblock": "block", "blocks": "block",
                "two_columns": "columns", "column": "columns", "itemize": "bullets",
                "list": "bullets", "tabular": "table", "math": "equation", "pseudocode": "algorithm"}

# Una diapo de solo viñetas que cita una tabla o ecuación pasa a mostrarla.
# (Un alg* puede citarse como contexto sin mostrar el pseudocódigo.)
BULLETS_UPGRADE = {"tab": "table", "eq": "equation"}

# ----------------------------------------------------------------------------
# Esquemas
# ----------------------------------------------------------------------------
class SlideSpec(BaseModel):
    title: str
    bullets: list[str]
    kind: str = Field(description="title, bullets, columns, block, equation, algorithm o table")
    sources: list[str] = Field(default_factory=list,
                               description="IDs de fragmentos que usa la diapo")

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


FrameResult = tuple[int, str, str, int, list[str]]  # (idx, frame, status, intentos, avisos)


class State(TypedDict, total=False):
    source_path: str
    base_path: str
    style_path: str
    out_dir: str
    extractor: str
    human_review: bool
    chunks: dict[str, str]
    outline: dict
    head: str               # base hasta el marcador (incluye \\begin{document})
    tail: str               # base desde el marcador
    frames: Annotated[list[FrameResult], operator.add]
    tex: str
    log_errors: list[str]
    global_attempts: int
    pdf_path: str


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
    style_errors: list[str]
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


def call_text(model: str, prompt: str) -> str:
    msg = _chat(model, REASONING_SLIDES).invoke(prompt)
    registrar_uso(model, msg)
    out = msg.content
    if isinstance(out, list):
        out = "".join(b.get("text", "") for b in out if isinstance(b, dict))
    return strip_fences(out)


def call_structured(model: str, schema: type[BaseModel], prompt: str,
                    effort: str | None = None) -> BaseModel:
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
        obj = _NEWLINE_CMD.sub(lambda _: "\\n", obj)
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
        chunks[new] = head + "\n" + (TABLA_DANADA + "\n" if tabla_danada(md) else "") + md
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
                ignore_vbox: bool = False) -> tuple[list[str], Path]:
    d = Path(tempfile.mkdtemp(prefix="beamer_"))   # directorio propio: seguro en paralelo
    (d / "doc.tex").write_text(tex)
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
    if re.search(r"\\verb|verbatim|lstlisting", frame) and "[fragile" not in frame:
        errs.append("Usa \\begin{frame}[fragile] si incluyes verbatim o \\verb")
    return errs


def _norm_numbers(s: str) -> str:
    s = re.sub(r"\\[,;!]|~|\{,\}", "", s)
    return re.sub(r"(?<=\d)[ ,](?=\d{3}\b)", "", s)


def soft_checks(frame: str, context: str) -> list[str]:
    """Avisos que no bloquean: densidad y cifras que no aparecen en las fuentes."""
    warns = []
    text = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", " ", frame)
    text = re.sub(r"[{}$\\&_^]", "", text)
    if len(" ".join(text.split())) > MAX_TEXT_CHARS:
        warns.append("Diapositiva densa: considera partirla")
    body = re.sub(r"\d*\.?\d+\s*(pt|em|ex|cm|mm|in)\b|\d*\.?\d+\\(text|line|column)width", "",
                  _norm_numbers(frame))
    ctx = _norm_numbers(context)
    for n in sorted(set(re.findall(r"\d+(?:\.\d+)?", body))):
        if len(n.replace(".", "")) >= 2 and n not in ctx:
            warns.append(f"Cifra {n} no aparece en las fuentes: verificar")
    return warns

# ----------------------------------------------------------------------------
# Estilo
# ----------------------------------------------------------------------------
def load_style(path: str | None) -> tuple[str, dict]:
    p = Path(path or DEFAULT_STYLE)
    if not p.exists():
        return "", dict(STYLE_DEFAULTS)
    data = tomllib.loads(p.read_text())
    return data.get("guia", {}).get("texto", "").strip(), {**STYLE_DEFAULTS, **data.get("limites", {})}


def describe_limits(lim: dict) -> str:
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
    return errs + filas_repetidas(body)


def filas_repetidas(body: str) -> list[str]:
    """Filas de tabla con exactamente los mismos números: señal de datos copiados/inventados."""
    errs = []
    for tab in re.findall(r"\\begin\{tabular\*?\}.*?\\end\{tabular\*?\}", body, re.S):
        seen: dict[tuple, str] = {}
        for row in tab.split(r"\\"):
            cells = row.split("&")
            nums = tuple(re.findall(r"\d[\d.,]*", "&".join(cells[1:])))
            if len(nums) < 2:
                continue
            name = re.sub(r"\\[a-zA-Z]+|[{}]", "", cells[0]).split("\n")[-1].strip()
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
diapositivas, sobre el paper cuyos fragmentos aparecen abajo.

Devuelve:
- title, authors, venue: en LaTeX válido (escapa &, %, _).
- notation: glosario breve de los símbolos que TODAS las diapositivas deben usar igual.
  Puedes apoyarte en estas macros ya definidas: {macros}
- slides: la primera con kind='title' y sin sources. Cada una de las demás debe listar en sources \
los IDs exactos de los fragmentos que necesita (incluidos eq*, tab*, alg* si usa esa ecuación, \
tabla o algoritmo). Si un tema no cabe en los límites, divídelo en dos diapositivas.
- kind: elige el formato que mejor muestre cada idea y VARÍALOS a lo largo de la presentación:
{kinds}
  Cita un tab* solo si la diapo muestra esa tabla (kind table o columns); cita un eq* si muestra \
esa ecuación. Las fórmulas que definen el método deben aparecer en display, no descritas con \
palabras. Si el algoritmo es central, dedícale una diapo algorithm.
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
las cifras del texto. No crees columnas agregadas (mejor/peor, promedios) que la fuente no tenga. \
Respeta qué mide cada cifra (p. ej. "ganancia frente al mejor competidor" no es "frente a X").
- Debe caber en una pantalla. Si el contenido es mucho, prioriza y resume.

Guía de estilo:
{guide}
Límites obligatorios (se verifican automáticamente): {limits}

Tipo: {kind} ({kind_desc})
Ejemplo de formato para este tipo (solo la estructura; el contenido es inventado y no debe copiarse):
{kind_example}
Puedes combinar con un alertblock o exampleblock para el mensaje clave si cabe.

Título: {title}
Contenido a cubrir (ideas y datos, no viñetas literales):
{bullets}

CONTEXTO:
{context}"""

REFINE_SLIDE_PROMPT = r"""Esta diapositiva Beamer no pasa la validación. Corrígela.

Errores del compilador (las líneas son relativas al frame):
{errors}

Reglas de estilo incumplidas:
{style_errors}

Para desbordes o exceso de contenido: acorta, fusiona o elimina lo menos importante.
No cambies el contenido factual. Respeta la guía y los límites:
{guide}
Límites: {limits}
Macros disponibles: {macros}
Devuelve solo el frame completo corregido.

FRAME:
{frame}

CONTEXTO (para verificar datos):
{context}"""

REFINE_GLOBAL_PROMPT = r"""El cuerpo de esta presentación Beamer compila bien diapositiva por
diapositiva, pero falla al compilarse completo. Corrige solo lo necesario; la
cabecera es fija y no puedes cambiarla, así que no agregues paquetes ni macros.

Errores:
{errors}

Devuelve solo el cuerpo corregido (lo que va entre \begin{{document}} y \end{{document}}, sin
incluir esas dos líneas).

CUERPO:
{body}"""

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
def ingest(state: State) -> dict:
    preflight(load_base(state))
    text, fmt = extract_text(Path(state["source_path"]), state.get("extractor", "pymupdf"))
    chunks = chunk_document(text, fmt)
    if not chunks:
        raise RuntimeError("No se pudo extraer texto del documento")
    return {"chunks": chunks}


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
        if s.kind != "title" and s.kind not in KINDS:
            errs.append(f"Diapositiva {i} ('{s.title}') tiene kind='{s.kind}' inválido; "
                        f"usa uno de: {', '.join(KINDS)}")
            continue
        if len(s.bullets) > lim["max_items"]:
            errs.append(f"Diapositiva {i} ('{s.title}') tiene {len(s.bullets)} puntos; "
                        f"máximo {lim['max_items']}: divídela")
        bad = [c for c in s.sources if c not in chunks]
        if bad:
            errs.append(f"Diapositiva {i} ('{s.title}') cita fragmentos inexistentes: {bad}")
        if s.kind != "title" and not s.sources:
            errs.append(f"Diapositiva {i} ('{s.title}') no tiene sources")
    return errs


def variedad_outline(o: Outline, lim: dict) -> list[str]:
    """Aviso (no bloquea): demasiadas diapos de solo viñetas."""
    content = [s for s in o.slides if s.kind != "title"]
    n_bul = sum(s.kind == "bullets" for s in content)
    if len(content) >= 4 and n_bul > lim["max_fraccion_vinetas"] * len(content):
        return [f"{n_bul} de {len(content)} diapositivas son solo viñetas; máximo "
                f"{lim['max_fraccion_vinetas']:.0%}. Usa columns, block, equation o table"]
    return []


def outline(state: State) -> dict:
    chunks = state["chunks"]
    listing = "\n\n".join(f"[{cid}]\n{txt}" for cid, txt in chunks.items())
    base = load_base(state)
    guide, lim = load_style(state.get("style_path"))
    feedback = ""
    for attempt in range(1, MAX_OUTLINE_ATTEMPTS + 1):
        o = call_structured(MODEL_OUTLINE, Outline, effort=REASONING_OUTLINE, prompt=OUTLINE_PROMPT.format(
            kinds="\n".join(f"  - {k}: {d}" for k, (d, _, _) in KINDS.items()),
            nmin=N_SLIDES[0], nmax=N_SLIDES[1], chunks=listing, feedback=feedback,
            macros=base_macros(base), guide=guide or "-", limits=describe_limits(lim)))
        o = ajustar_kinds(o, chunks)
        errs = validate_outline(o, chunks, lim)
        soft = variedad_outline(o, lim)
        if not errs and (not soft or attempt == MAX_OUTLINE_ATTEMPTS):
            for w in soft:
                print(f"Aviso del guion: {w}")
            d = o.model_dump()
            head, tail = split_base(base, d)
            return {"outline": d, "head": head, "tail": tail}
        feedback = "\nCORRIGE estos problemas del intento anterior:\n- " + "\n- ".join(errs + soft) + "\n"
    raise RuntimeError(f"Outline inválido tras {MAX_OUTLINE_ATTEMPTS} intentos: {errs}")


def review_outline(state: State) -> dict:
    """Punto de revisión humana: el guion es lo más barato de corregir."""
    if not state.get("human_review"):
        return {}
    edited = interrupt({"outline": state["outline"]})
    if isinstance(edited, dict) and edited.get("slides"):
        o = Outline.model_validate(edited)
        errs = validate_outline(o, state["chunks"], load_style(state.get("style_path"))[1])
        if errs:
            raise RuntimeError(f"El guion editado no es válido: {errs}")
        d = o.model_dump()
        head, tail = split_base(load_base(state), d)
        return {"outline": d, "head": head, "tail": tail}
    return {}


def fan_out(state: State) -> list[Send]:
    o, chunks = state["outline"], state["chunks"]
    base = load_base(state)
    macros, packages = base_macros(base), ", ".join(base_packages(base))
    guide, lim = load_style(state.get("style_path"))
    return [Send("slide", {
        "idx": i, "spec": s, "head": state["head"], "notation": o["notation"],
        "macros": macros, "packages": packages, "style_guide": guide or "-", "limits": lim, "context": "\n\n".join(chunks[c] for c in s["sources"]),
        "damaged": [c for c in s["sources"] if TABLA_DANADA in chunks[c]],
        "attempts": 0, "errors": [], "style_errors": [], "warnings": [],
    }) for i, s in enumerate(o["slides"])]


def assemble(state: State) -> dict:
    body = []
    for idx, frame, status, _, _ in sorted(state["frames"]):
        if status == "failed":
            title = state["outline"]["slides"][idx]["title"]
            frame = (f"% TODO: la diapositiva {idx} no compiló tras {MAX_SLIDE_ATTEMPTS} intentos\n"
                     f"\\begin{{frame}}{{{title}}}\n\\alert{{Diapositiva pendiente de revisión}}\n"
                     f"\\end{{frame}}")
        body.append(frame)
    tex = state["head"] + "\n" + "\n\n".join(body) + "\n" + state["tail"]
    return {"tex": tex, "global_attempts": 0}


def compile_full(state: State) -> dict:
    errors, d = compile_tex(state["tex"], passes=2, ignore_vbox=True)
    return {"log_errors": errors, "pdf_path": str(d / "doc.pdf")}


def route_full(state: State) -> str:
    if state["log_errors"] and state["global_attempts"] < MAX_GLOBAL_ATTEMPTS:
        return "refine_global"
    return "write_outputs"


def refine_global(state: State) -> dict:
    head, tail = state["head"], state["tail"]          # la base nunca se modifica
    body = state["tex"][len(head):len(state["tex"]) - len(tail)]
    new_body = call_text(MODEL_SLIDES, REFINE_GLOBAL_PROMPT.format(
        errors="\n".join(state["log_errors"]), body=body))
    new_body = re.sub(r"\\(begin|end)\{document\}", "", new_body)
    return {"tex": head + new_body + tail, "global_attempts": state["global_attempts"] + 1}


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


def write_outputs(state: State) -> dict:
    out = Path(state["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "presentacion.tex").write_text(state["tex"])
    pdf = Path(state["pdf_path"])
    if pdf.exists():
        shutil.copy(pdf, out / "presentacion.pdf")
    (out / "outline.json").write_text(json.dumps(state["outline"], ensure_ascii=False, indent=2))
    lines = ["# Informe de generación\n",
             f"Compilación global: {'OK' if not state['log_errors'] else 'con errores'} "
             f"({state['global_attempts']} refinados globales)\n"]
    lines += [f"- {e}" for e in state["log_errors"]]
    lines.append("\n| # | Diapositiva | Estado | Intentos | Avisos |\n|---|---|---|---|---|")
    for idx, _, status, attempts, warns in sorted(state["frames"]):
        title = state["outline"]["slides"][idx]["title"].replace("|", "/")
        lines.append(f"| {idx} | {title} | {status} | {attempts} | {'; '.join(warns) or '-'} |")
    lines += informe_uso()
    (out / "informe.md").write_text("\n".join(lines) + "\n")
    return {}

# ----------------------------------------------------------------------------
# Nodos del subgrafo por diapositiva
# ----------------------------------------------------------------------------
def write_slide(s: SlideState) -> dict:
    spec = s["spec"]
    if spec["kind"] == "title":      # determinista: no hace falta LLM
        return {"frame": "\\begin{frame}[plain]\n\\titlepage\n\\end{frame}"}
    frame = call_text(MODEL_SLIDES, SLIDE_PROMPT.format(
        macros=s["macros"], packages=s["packages"], guide=s["style_guide"],
        limits=describe_limits(s["limits"]), notation=s["notation"], kind=spec["kind"],
        kind_desc=KINDS[spec["kind"]][0], kind_example=KINDS[spec["kind"]][1], title=spec["title"],
        bullets="\n".join(f"- {b}" for b in spec["bullets"]), context=s["context"]))
    return {"frame": frame}


def compile_slide(s: SlideState) -> dict:
    errors = lint_frame(s["frame"])          # barato: antes de llamar a pdflatex
    if not errors:
        tex, offset = standalone(s["head"], s["frame"])
        errors, _ = compile_tex(tex, offset=offset, ignore_vbox=s["spec"]["kind"] == "title")
    spec = s["spec"]
    style = [] if spec["kind"] == "title" else (
        kind_check(s["frame"], spec["kind"], spec["sources"], tuple(s.get("damaged", ())))
        + style_check(s["frame"], s["limits"]))
    return {"errors": errors, "style_errors": style}


def route_slide(s: SlideState) -> str:
    if (s["errors"] or s["style_errors"]) and s["attempts"] < MAX_SLIDE_ATTEMPTS:
        return "refine_slide"
    return "finish_slide"


def refine_slide(s: SlideState) -> dict:
    frame = call_text(MODEL_SLIDES, REFINE_SLIDE_PROMPT.format(
        errors="\n".join(s["errors"]) or "ninguno",
        style_errors="\n".join(s["style_errors"]) or "ninguna",
        guide=s["style_guide"], limits=describe_limits(s["limits"]),
        macros=s["macros"], frame=s["frame"], context=s["context"]))
    return {"frame": frame, "attempts": s["attempts"] + 1}


def finish_slide(s: SlideState) -> dict:
    status = "failed" if s["errors"] else "ok"   # el estilo nunca descarta una diapo que compila
    warns = [f"Estilo: {e}" for e in s["style_errors"]]
    warns += soft_checks(s["frame"], s["context"] + "\n" + json.dumps(s["spec"]))
    return {"frames": [(s["idx"], s["frame"], status, s["attempts"], warns)]}

# ----------------------------------------------------------------------------
# Construcción de los grafos
# ----------------------------------------------------------------------------
def build_slide_graph():
    g = StateGraph(SlideState, output_schema=SlideOutput)
    g.add_node("write_slide", write_slide)
    g.add_node("compile_slide", compile_slide)
    g.add_node("refine_slide", refine_slide)
    g.add_node("finish_slide", finish_slide)
    g.add_edge(START, "write_slide")
    g.add_edge("write_slide", "compile_slide")
    g.add_conditional_edges("compile_slide", route_slide, ["refine_slide", "finish_slide"])
    g.add_edge("refine_slide", "compile_slide")
    g.add_edge("finish_slide", END)
    return g.compile()


def build_graph(checkpointer=None):
    g = StateGraph(State)
    g.add_node("ingest", ingest)
    g.add_node("outline", outline)
    g.add_node("review_outline", review_outline)
    g.add_node("slide", build_slide_graph())
    g.add_node("assemble", assemble)
    g.add_node("compile_full", compile_full)
    g.add_node("refine_global", refine_global)
    g.add_node("write_outputs", write_outputs)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "outline")
    g.add_edge("outline", "review_outline")
    g.add_conditional_edges("review_outline", fan_out, ["slide"])
    g.add_edge("slide", "assemble")
    g.add_edge("assemble", "compile_full")
    g.add_conditional_edges("compile_full", route_full, ["refine_global", "write_outputs"])
    g.add_edge("refine_global", "compile_full")
    g.add_edge("write_outputs", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())

# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="paper en .pdf, .tex o .md")
    ap.add_argument("--out", default="salida")
    ap.add_argument("--base", default=str(DEFAULT_BASE), help="plantilla con %%SLIDES%%")
    ap.add_argument("--estilo", default=str(DEFAULT_STYLE), help="reglas de estilo (TOML)")
    ap.add_argument("--review", action="store_true", help="pausar para revisar el guion")
    ap.add_argument("--extractor", choices=["pymupdf", "marker"], default="pymupdf")
    ap.add_argument("--concurrency", type=int, default=4, help="diapos en paralelo")
    args = ap.parse_args()

    graph = build_graph()
    config = {"configurable": {"thread_id": "beamer"}, "max_concurrency": args.concurrency}
    result = graph.invoke({"source_path": args.source, "base_path": args.base, "style_path": args.estilo,
                           "out_dir": args.out,
                           "extractor": args.extractor, "human_review": args.review}, config)

    while "__interrupt__" in result:
        path = Path(args.out) / "outline_borrador.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result["__interrupt__"][0].value["outline"],
                                   ensure_ascii=False, indent=2))
        input(f"Guion guardado en {path}. Edítalo si quieres y pulsa Enter para continuar... ")
        result = graph.invoke(Command(resume=json.loads(path.read_text())), config)

    print("\n".join(informe_uso()))
    print(f"Listo: {args.out}/presentacion.pdf (ver {args.out}/informe.md)")


if __name__ == "__main__":
    main()
