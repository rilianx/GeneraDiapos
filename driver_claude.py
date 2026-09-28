"""Modo Claude Code: el mismo grafo de beamer_graph.py, sin API.

Con LLM_PROVIDER=claude, cada llamada al modelo (call_text / call_structured) hace
interrupt(): el grafo se pausa y el estado queda en DIR/estado.sqlite. Este driver
escribe cada pausa como una tarea en DIR/tareas/ y termina. Claude Code (u otra
persona) la responde escribiendo el archivo que indica en DIR/respuestas/, y al
volver a ejecutar se valida la respuesta y el grafo continúa exactamente donde iba.

    python beamer_graph.py papers/x.pdf --claude trabajo/x --out presentaciones/x --review
    # ...responder las tareas...
    python beamer_graph.py --claude trabajo/x          # repetir hasta que diga "Listo"

Todo lo demás (validaciones, reintentos, best_frame, revisor, secciones, informe) es
el código del pipeline: el driver no decide nada, solo transporta preguntas y respuestas.

Códigos de salida: 0 listo, 3 hay tareas pendientes, 1 error.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import beamer_graph as bg

PENDIENTES = "pendientes.json"
COMUN = "comun.md"
TIEMPOS = "tiempos.jsonl"
MIN_COMUN = 150          # un párrafo más corto no vale la pena compartirlo


def modo_guion() -> str:
    """LLM_GUION: "archivo" (defecto) deja DIR/guion.md para editarlo en tu editor (LaTeX
    tal cual, fórmulas $…$); "docs" lo pasa a un documento de Claude Docs."""
    m = (os.environ.get("LLM_GUION") or "archivo").strip().lower()
    if m not in ("archivo", "docs"):
        raise SystemExit(f"LLM_GUION debe ser 'archivo' o 'docs', no {m!r}")
    return m


def revisor() -> str:
    """LLM_REVISOR: "self" (defecto) revisa Claude mismo, casi sin costo porque el paper ya está
    en su contexto; "subagente" delega todas las revisiones de la ronda en un subagente
    independiente (≈80k tokens por ronda en lsmear, pero no comparte los sesgos del autor)."""
    r = (os.environ.get("LLM_REVISOR") or "self").strip().lower()
    if r not in ("self", "subagente"):
        raise SystemExit(f"LLM_REVISOR debe ser 'self' o 'subagente', no {r!r}")
    return r


def compactar(cuerpos: list[str]) -> tuple[list[str], dict[str, str]]:
    """Párrafos que se repiten en dos o más tareas de la ronda (reglas, guía, plantillas,
    esquemas, guion) pasan a un archivo común y cada tarea los cita por id. Solo cambia
    cómo se transporta el prompt: Claude lee cada bloque una vez en vez de N veces."""
    partes = [c.split("\n\n") for c in cuerpos]
    veces: dict[str, int] = {}
    for ps in partes:
        for q in set(ps):
            veces[q] = veces.get(q, 0) + 1
    comun: dict[str, str] = {}
    salida = []
    for ps in partes:
        nuevas = []
        for q in ps:
            if len(q) >= MIN_COMUN and veces[q] > 1:
                bid = "C" + hashlib.sha1(q.encode()).hexdigest()[:6]
                comun.setdefault(bid, q)
                q = f"[bloque común {bid}: está en {COMUN}]"
            nuevas.append(q)
        salida.append("\n\n".join(nuevas))
    return salida, comun


def expandir(texto: str, comun: Path) -> str:
    """Inversa de compactar: la tarea con sus bloques comunes en línea (tests, depuración)."""
    bloques = dict(re.findall(r"^## (C[0-9a-f]{6})\n\n(.*?)(?=\n\n## C[0-9a-f]{6}\n|\n?\Z)",
                              comun.read_text(), re.S | re.M)) if comun.exists() else {}
    return re.sub(r"\[bloque común (C[0-9a-f]{6}): está en [^\]]+\]", lambda m: bloques[m.group(1)], texto)


def revision_independiente() -> str:
    """Quien escribió la diapo no debería verificarla: se delega en un subagente sin contexto.
    El texto no lleva rutas (están en la cabecera de cada tarea): así va una vez a comun.md."""
    return ("PARA QUIEN COORDINA (un subagente revisor ignora este párrafo): no hagas tú esta "
            "revisión, que escribiste la diapositiva. Lanza UN solo subagente (herramienta Agent) "
            "para todas las revisiones de la ronda y pásale las rutas de las tareas con este encargo: "
            "«Lee cada tarea y los bloques que cita del archivo de bloques comunes. Lee una vez las "
            "páginas del paper que indican los CONTEXTOS (Read con pages) y verifica cada afirmación "
            "contra esas páginas, no contra lo que recuerdes. Escribe cada JSON en el archivo de "
            "Respuesta de su cabecera». Sin subagentes, hazla tú releyendo esas páginas.")


def etiqueta(p: dict) -> str:
    """Nombre legible de la tarea según el nodo que la pidió."""
    if "outline" in p:
        return "revision-guion"
    prompt = p.get("prompt", "")
    title = re.search(r"^Título: (.+)$", prompt, re.M)
    if p.get("esquema") == "Lectura":
        return "lectura"
    if p.get("esquema") == "Outline":
        return "guion"
    if p.get("esquema") == "Review":
        return "revisar-afirmaciones"
    if prompt.startswith("Escribe UNA diapositiva"):
        return "escribir-diapo" + (f" «{title.group(1)}»" if title else "")
    if prompt.startswith("Esta diapositiva"):
        return "corregir-diapo"
    if prompt.startswith("El cuerpo de esta presentación"):
        return "corregir-presentacion"
    return "tarea"


def escribir_tareas(d: Path, interrupts, meta: dict, values: dict) -> list[dict]:
    tareas = d / "tareas"
    for f in tareas.glob("*.md"):
        f.unlink()
    pend, archivos = [], []
    for n, it in enumerate(interrupts, 1):
        p = it.value
        tag = etiqueta(p)
        if "outline" in p:                                   # revisión humana del guion
            ext = "md"
            chunks = values.get("chunks", {})
            (d / "guion.json").write_text(json.dumps(p["outline"], ensure_ascii=False, indent=2))
            if modo_guion() == "archivo":
                (d / "guion.md").write_text(bg.guion_doc_md(p["outline"], chunks, crudo=True))
                cuerpo = (
                    "Revisión humana del guion, en un archivo markdown.\n\n"
                    f"1. Dile al usuario, en dos líneas, que abra `{(d / 'guion.md').resolve()}` en su "
                    "editor (en VS Code, Ctrl+Shift+V muestra la vista previa con las fórmulas), que lo "
                    "edite a su gusto y que te avise cuando lo apruebe.\n"
                    "2. ESPERA su respuesta: no asumas la aprobación ni edites tú el archivo, salvo que "
                    "te pida un cambio concreto.\n"
                    "3. Cuando apruebe, escribe solo la palabra «aprobado» en el archivo de respuesta: "
                    "el pipeline lee el guion.md editado con código y lo valida.")
            else:
                (d / "guion_doc.md").write_text(bg.guion_doc_md(p["outline"], chunks))
                cuerpo = (
                "Revisión humana del guion, en un documento editable.\n\n"
                f"1. Si tienes el conector de documentos de Claude (Claude Docs), crea un documento con el "
                f"contenido EXACTO de `{d}/guion_doc.md` (sin reescribirlo) y da el link al usuario: puede "
                "editar textos, viñetas, tipos, fuentes y borrar o mover diapositivas directamente. Sin "
                f"conector, muéstrale `{d}/guion_doc.md` y aplica tú los cambios que pida sobre ese archivo.\n"
                "2. ESPERA su respuesta: no asumas la aprobación.\n"
                "3. Cuando apruebe, exporta el documento a markdown (export, format markdown) y guarda el "
                "texto decodificado, tal cual, en el archivo de respuesta. Sin conector, copia ahí "
                f"`{d}/guion_doc.md` con los cambios. El pipeline lo lee con código y lo valida.")
        else:
            ext = "tex" if p["formato"] == "texto" else "json"
            cuerpo = p["prompt"]
            if p["formato"] == "json":
                cuerpo += ("\n\n---\nResponde SOLO con un JSON que cumpla este esquema "
                           f"({p['esquema']}):\n```json\n{json.dumps(p['json_schema'], ensure_ascii=False)}\n```")
            else:
                cuerpo += "\n\n---\nResponde SOLO con el contenido pedido (sin explicación ni ```)."
        resp = d / "respuestas" / f"{it.id}.{ext}"
        name = f"{n:02d}_{re.sub(r'[^a-z-]', '', tag.split(' ')[0])}_{it.id[:8]}.md"
        if tag == "revisar-afirmaciones" and revisor() == "subagente":
            cuerpo = revision_independiente() + "\n\n" + cuerpo
        archivos.append((tareas / name, f"# {tag}\n\nRespuesta: `{resp}`\nPaper: `{meta['paper']}`",
                         cuerpo))
        pend.append({"id": it.id, "tarea": str(tareas / name), "respuesta": str(resp),
                     "etiqueta": tag.split(" ")[0],
                     "tipo": "guion" if "outline" in p else p["formato"],
                     "esquema": p.get("esquema")})
    cuerpos, comun = compactar([c for _, _, c in archivos])
    if comun:
        (d / COMUN).write_text(
            "# Bloques comunes de esta ronda\n\nLas tareas los citan por id. Léelos una vez; un "
            "bloque con el mismo id es idéntico aunque aparezca en otra ronda.\n\n"
            + "\n\n".join(f"## {bid}\n\n{txt}" for bid, txt in comun.items()) + "\n")
    for (f, cab, _), cuerpo in zip(archivos, cuerpos):
        aviso = f"\nBloques comunes: `{d / COMUN}`" if "[bloque común" in cuerpo else ""
        f.write_text(f"{cab}{aviso}\n\n---\n\n{cuerpo}\n")
    (d / PENDIENTES).write_text(json.dumps(pend, ensure_ascii=False, indent=2))
    return pend


def leer_respuestas(pend: list[dict], values: dict) -> tuple[dict, list[str], list[str]]:
    """Respuestas válidas por id de interrupt, las que faltan y las que no validan."""
    ans, faltan, errores = {}, [], []
    for t in pend:
        f = Path(t["respuesta"])
        if not f.exists():
            faltan.append(t["tarea"])
            continue
        raw = f.read_text()
        if t["tipo"] == "texto":
            ans[t["id"]] = raw
            continue
        editado = None
        try:
            if t["tipo"] == "guion" and raw.strip().lower().startswith("aprobado"):
                editado = (f.parent.parent / "guion.md").resolve()   # el archivo que editó el usuario
                raw = editado.read_text()
            if f.suffix == ".md":                        # guion editado como documento
                data = bg.guion_desde_md(raw)
            else:
                data = json.loads(bg.strip_fences(raw).removeprefix("json").strip())
            if t["tipo"] == "guion":
                lim = bg.load_style(values.get("style_path"))[1]
                o = bg.asegurar_portada(bg.ajustar_kinds(bg.Outline.model_validate(data),
                                                         values["chunks"]), lim)
                errs = bg.validate_outline(o, values["chunks"], lim)
                if errs:
                    raise ValueError("; ".join(errs))
            else:
                getattr(bg, t["esquema"]).model_validate(bg.restaurar_escapes(data))
            ans[t["id"]] = data
        except Exception as e:                        # la respuesta vuelve a pedirse
            if editado:                               # el error está en el archivo del usuario
                errores.append(f"GUION: el guion que editó el usuario ({editado}) no valida: {e}\n"
                               "Es su archivo: muéstrale estos errores en pocas líneas y espera a que "
                               "lo corrija (corrígelo tú solo si te lo pide). Luego vuelve a ejecutar "
                               "el comando; la respuesta «aprobado» ya está escrita.")
            else:
                errores.append(f"{f}: {e}")
    return ans, faltan, errores


def valor_resume(ans: dict, snap):
    """Un nodo que repite su pregunta (p. ej. validar_lectura rechazó el inventario) reusa el
    id del interrupt: reanudar con {id: valor} deja el grafo roto. Eso solo pasa cuando el
    grafo tiene un único interrupt pendiente, y entonces se reanuda con el valor. Con diapos
    en paralelo el grafo puede listar varios de un mismo paso aunque la ronda tenga una sola
    tarea: ahí se reanuda por id."""
    return next(iter(ans.values())) if len(ans) == 1 and len(snap.interrupts) == 1 else ans


def registrar_tiempo(d: Path, inicio: float, respondidas: list[dict], nuevas: list[dict],
                     invalidas: int = 0) -> None:
    """Una línea por ejecución: cuánto corrió el pipeline y qué tareas cerró y abrió. El
    tiempo de Claude (o de la persona) es el hueco entre el fin de una ejecución y el
    inicio de la siguiente."""
    with open(d / TIEMPOS, "a") as f:
        f.write(json.dumps({"inicio": inicio, "fin": time.time(),
                            "respondidas": dict(Counter(t.get("etiqueta", "?") for t in respondidas)),
                            "nuevas": dict(Counter(t.get("etiqueta", "?") for t in nuevas)),
                            "invalidas": invalidas}, ensure_ascii=False) + "\n")


def resumen_tiempos(d: Path) -> tuple[list[str], str]:
    """Tabla de tiempos por ronda (markdown) y una línea de resumen."""
    f = d / TIEMPOS
    if not f.exists():
        return [], ""
    rs = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    fmt = lambda c: ", ".join(f"{n} {k}" for k, n in c.items()) or "-"
    filas, tot = [], {"claude": 0.0, "persona": 0.0, "pipeline": 0.0}
    for prev, r in zip([None] + rs[:-1], rs):
        pipe = r["fin"] - r["inicio"]
        tot["pipeline"] += pipe
        hueco = r["inicio"] - prev["fin"] if prev else 0.0
        quien = "persona" if "revision-guion" in r["respondidas"] else "claude"
        tot[quien] += hueco
        filas.append(f"| {len(filas) + 1} | {fmt(r['respondidas']) if prev else 'inicio'} | "
                     f"{hueco:.0f} ({quien}) | {pipe:.0f} | {fmt(r['nuevas'])}"
                     + (f" · {r['invalidas']} inválida(s)" if r["invalidas"] else "") + " |")
    tabla = ["\n## Tiempos (modo Claude)\n",
             "| Ronda | Tareas respondidas | Respuesta (s) | Pipeline (s) | Tareas nuevas |",
             "|---|---|---|---|---|"] + filas + [
             f"\nClaude: {tot['claude'] / 60:.1f} min · pipeline: {tot['pipeline'] / 60:.1f} min · "
             f"revisión humana del guion: {tot['persona'] / 60:.1f} min"]
    return tabla, tabla[-1].strip()


def listo(graph, cfg, meta: dict, d: Path | None = None) -> int:
    """Solo se declara terminado si el grafo terminó de verdad y dejó el PDF."""
    snap = graph.get_state(cfg)
    pdf = Path(meta["out"]) / "presentacion.pdf"
    if snap.next or any(t.interrupts for t in snap.tasks) or not pdf.exists():
        raise RuntimeError(f"el grafo quedó sin terminar (siguiente: {snap.next or '-'}, "
                           f"PDF {'sí' if pdf.exists() else 'no'} existe)")
    informe = Path(meta["out"]) / "informe.md"
    tabla, linea = resumen_tiempos(d) if d else ([], "")
    if tabla and informe.exists() and "## Tiempos (modo Claude)" not in informe.read_text():
        informe.write_text(informe.read_text().rstrip() + "\n" + "\n".join(tabla) + "\n")
    print(f"Listo: {pdf} (ver {informe})" + (f"\n{linea}" if linea else ""))
    return 0


def informar(pend: list[dict]) -> None:
    print(f"{len(pend)} tarea(s) pendiente(s):")
    for t in pend:
        print(f"- {t['tarea']}  →  {t['respuesta']}")
    print("Responde todas y vuelve a ejecutar el mismo comando con --claude. Escribe solo los "
          "archivos de respuesta: no modifiques código ni configuración, y no comentes entre rondas.")


def run(args) -> int:
    inicio = time.time()
    bg.PROVIDER = "claude"
    d = Path(args.claude)
    (d / "tareas").mkdir(parents=True, exist_ok=True)
    (d / "respuestas").mkdir(exist_ok=True)
    graph = bg.build_graph(SqliteSaver(sqlite3.connect(d / "estado.sqlite", check_same_thread=False)))
    cfg = {"configurable": {"thread_id": "beamer"}, "max_concurrency": args.concurrency}
    snap = graph.get_state(cfg)
    meta_f = d / "meta.json"

    if not snap.values:                                        # ejecución nueva
        if not args.source:
            print("Falta el paper para empezar: python beamer_graph.py PAPER --claude DIR")
            return 1
        meta = {"paper": args.source, "out": args.out}
        meta_f.write_text(json.dumps(meta, ensure_ascii=False))
        state = {"source_path": args.source, "base_path": args.base, "style_path": args.estilo,
                 "out_dir": args.out, "extractor": args.extractor, "human_review": args.review,
                 **bg.opciones_pptx(args)}
        if args.guion:
            state["outline_path"] = args.guion
        result = graph.invoke(state, cfg)
    else:
        meta = json.loads(meta_f.read_text())
        if not snap.next and not any(t.interrupts for t in snap.tasks):
            return listo(graph, cfg, meta, d)
        pend = json.loads((d / PENDIENTES).read_text())
        ans, faltan, errores = leer_respuestas(pend, snap.values)
        for e in errores:
            print(e.removeprefix("GUION: ") if e.startswith("GUION: ") else f"RESPUESTA INVÁLIDA (corrígela): {e}")
        if faltan or errores:
            informar([t for t in pend if t["id"] not in ans])
            registrar_tiempo(d, inicio, [t for t in pend if t["id"] in ans], [], len(errores) + len(faltan))
            return 3
        result = graph.invoke(Command(resume=valor_resume(ans, snap)), cfg)

    respondidas = [] if not snap.values else pend
    interrupts = result.get("__interrupt__", [])
    if interrupts:
        nuevas = escribir_tareas(d, interrupts, meta, graph.get_state(cfg).values)
        registrar_tiempo(d, inicio, respondidas, nuevas)
        informar(nuevas)
        return 3
    registrar_tiempo(d, inicio, respondidas, [])
    return listo(graph, cfg, meta, d)
