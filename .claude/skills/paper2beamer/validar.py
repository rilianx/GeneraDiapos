"""Valida una presentación Beamer escrita a mano o por Claude, con los mismos
chequeos deterministas del pipeline (beamer_graph.py): compilación aislada por
diapositiva, lint, reglas medibles de estilo.toml, filas de tabla copiadas o
vacías, estructura (portada, agenda, secciones), preámbulo de base.tex y cifras
que no aparecen en el paper.

Uso:
    python .claude/skills/paper2beamer/validar.py presentacion.tex \
        [--paper papers/x.pdf] [--base base.tex] [--estilo estilo.toml]

Sale con código 1 si hay errores (no compila o incumple reglas duras); los
avisos (estilo, cifras) no bloquean. Deja presentacion.pdf junto al .tex.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import beamer_graph as bg  # noqa: E402

FRAME = re.compile(r"\\begin\{frame\}.*?\\end\{frame\}", re.S)


def columnas_vacias(frame: str) -> list[str]:
    """Columnas de una tabla que solo tienen '--' o están vacías (datos inexistentes)."""
    errs = []
    for tab in re.findall(r"\\begin\{tabular\*?\}.*?\\end\{tabular\*?\}", frame, re.S):
        rows = [r.split("&") for r in bg._sin_colspec(tab).split(r"\\")]
        rows = [[c.strip() for c in r] for r in rows if len(r) > 1]
        data = [r for r in rows[1:] if re.search(r"\d", "".join(r))]   # sin encabezado
        if len(data) < 2:
            continue
        for j in range(1, max(len(r) for r in data)):
            cells = [r[j] if j < len(r) else "" for r in data]
            if all(re.fullmatch(r"(?:--+|—|-|n/?a|)", c) for c in cells):
                head = re.sub(r"\\[a-zA-Z]+|[{}]", "", rows[0][j]).strip() if j < len(rows[0]) else j
                errs.append(f"La columna '{head}' no tiene datos: quítala")
    return errs


def estructura(frames: list[str], body: str, lim: dict) -> list[str]:
    errs = []
    if not frames or r"\titlepage" not in frames[0]:
        errs.append("La primera diapositiva debe ser la portada (\\titlepage)")
    if lim.get("agenda", True) and (len(frames) < 2 or r"\tableofcontents" not in frames[1]):
        errs.append("La segunda diapositiva debe ser la agenda (\\tableofcontents)")
    secs = re.findall(r"\\section\*?\{([^}]*)\}", body)
    allowed = lim.get("secciones") or []
    if allowed:
        bad = [s for s in secs if s not in allowed]
        if bad:
            errs.append(f"Secciones fuera de [estructura].secciones: {bad}")
        order = [allowed.index(s) for s in secs if s in allowed]
        if order != sorted(order):
            errs.append(f"Las secciones no siguen el orden de estilo.toml: {secs}")
        if not secs:
            errs.append("No hay \\section{}: la agenda quedará vacía")
    elif lim.get("agenda", True) and not secs:
        errs.append("No hay \\section{}: la agenda quedará vacía")
    if len(secs) != len(set(secs)):
        errs.append(f"Hay secciones repetidas o partidas: {secs}")
    return errs


def preambulo(deck: str, base: str) -> list[str]:
    """El preámbulo de base.tex es del usuario: debe estar intacto en la presentación."""
    head = base.split(r"\begin{document}")[0]
    deck_head = deck.split(r"\begin{document}")[0]
    missing = [l.strip() for l in head.splitlines()
               if l.strip() and not l.strip().startswith("%") and "<<" not in l
               and l.strip() not in deck_head]
    return [f"Falta en el preámbulo (viene de base.tex): {l}" for l in missing]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tex")
    ap.add_argument("--paper", help="paper fuente (.pdf/.tex/.md) para chequear cifras")
    ap.add_argument("--base", default=str(bg.DEFAULT_BASE))
    ap.add_argument("--estilo", default=str(bg.DEFAULT_STYLE))
    args = ap.parse_args()

    path = Path(args.tex)
    deck = path.read_text()
    _, lim = bg.load_style(args.estilo)
    head = deck.split(r"\begin{document}")[0] + "\\begin{document}\n"
    body = deck.split(r"\begin{document}", 1)[-1]
    frames = FRAME.findall(body)
    paper_text = ""
    if args.paper:
        paper_text, _ = bg.extract_text(Path(args.paper), "pymupdf")

    errors = preambulo(deck, Path(args.base).read_text()) + estructura(frames, body, lim)
    warns: list[str] = []
    rows = []
    for i, fr in enumerate(frames):
        fixed = r"\titlepage" in fr or r"\tableofcontents" in fr
        e = bg.lint_frame(fr)
        if not e:
            tex, off = bg.standalone(head, fr)
            e, _ = bg.compile_tex(tex, offset=off, ignore_vbox=fixed)
        e += bg.filas_repetidas(fr) + columnas_vacias(fr)
        w = [] if fixed else bg.style_check(fr, lim)
        if paper_text and not fixed:
            w += bg.soft_checks(fr, paper_text)
        title = re.search(r"\\begin\{frame\}(?:\[[^\]]*\])?\{(.*)\}", fr)
        rows.append((i, (title.group(1) if title else "(portada)")[:60], e, w))

    full_errs, d = bg.compile_tex(deck, passes=2, ignore_vbox=True)
    if (d / "doc.pdf").exists():
        shutil.copy(d / "doc.pdf", path.with_suffix(".pdf"))

    print(f"# Validación de {path.name}\n")
    print(f"Diapositivas: {len(frames)} · compilación completa: "
          f"{'OK' if not full_errs else 'con errores'}\n")
    for e in errors + full_errs:
        print(f"- ERROR: {e}")
    n_err = len(errors) + len(full_errs)
    for i, t, e, w in rows:
        if e or w:
            print(f"\n## {i}. {t}")
            print("\n".join(f"- ERROR: {x}" for x in e))
            print("\n".join(f"- aviso: {x}" for x in w))
            n_err += len(e)
            warns += w
    print(f"\n{n_err} errores, {len(warns)} avisos")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
