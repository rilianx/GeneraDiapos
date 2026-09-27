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

import json
import re
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import beamer_graph as bg

PENDIENTES = "pendientes.json"


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
    pend = []
    for n, it in enumerate(interrupts, 1):
        p = it.value
        tag = etiqueta(p)
        if "outline" in p:                                   # revisión humana del guion
            ext = "md"
            chunks = values.get("chunks", {})
            (d / "guion.json").write_text(json.dumps(p["outline"], ensure_ascii=False, indent=2))
            (d / "guion.md").write_text(bg.guion_md(p["outline"], chunks, meta["paper"]))
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
        (tareas / name).write_text(
            f"# {tag}\n\nRespuesta: `{resp}`\nPaper: `{meta['paper']}` (léelo como imagen si "
            f"necesitas ver tablas, ecuaciones o figuras)\n\n---\n\n{cuerpo}\n")
        pend.append({"id": it.id, "tarea": str(tareas / name), "respuesta": str(resp),
                     "tipo": "guion" if "outline" in p else p["formato"],
                     "esquema": p.get("esquema")})
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
        try:
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
            errores.append(f"{f}: {e}")
    return ans, faltan, errores


def informar(pend: list[dict]) -> None:
    print(f"{len(pend)} tarea(s) pendiente(s):")
    for t in pend:
        print(f"- {t['tarea']}  →  {t['respuesta']}")
    print("Responde cada una y vuelve a ejecutar el mismo comando con --claude.")


def run(args) -> int:
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
                 "out_dir": args.out, "extractor": args.extractor, "human_review": args.review}
        if args.guion:
            state["outline_path"] = args.guion
        result = graph.invoke(state, cfg)
    else:
        meta = json.loads(meta_f.read_text())
        if not snap.next:
            print(f"Listo: {meta['out']}/presentacion.pdf (ver {meta['out']}/informe.md)")
            return 0
        pend = json.loads((d / PENDIENTES).read_text())
        ans, faltan, errores = leer_respuestas(pend, snap.values)
        for e in errores:
            print(f"RESPUESTA INVÁLIDA (corrígela): {e}")
        if faltan or errores:
            informar([t for t in pend if t["id"] not in ans])
            return 3
        result = graph.invoke(Command(resume=ans), cfg)

    interrupts = result.get("__interrupt__", [])
    if interrupts:
        informar(escribir_tareas(d, interrupts, meta, graph.get_state(cfg).values))
        return 3
    print(f"Listo: {meta['out']}/presentacion.pdf (ver {meta['out']}/informe.md)")
    return 0
