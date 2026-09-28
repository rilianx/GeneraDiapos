"""Tokens y tiempo por etapa de una corrida en modo Claude Code.

    python tokens_por_etapa.py --trabajo trabajo/x                 # última sesión de este proyecto
    python tokens_por_etapa.py --trabajo trabajo/x SESION.jsonl    # un transcript concreto

Cruza el transcript de Claude Code (~/.claude/projects/<proyecto>/<sesión>.jsonl: cada turno
trae su uso de tokens) con trabajo/x/tiempos.jsonl (cuándo corrió cada ronda del pipeline).
Cada turno cae en la etapa cuyas tareas estaba respondiendo; los turnos que solo escriben
texto (sin usar herramientas) son mensajes al usuario. Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

ETIQUETAS = {"lectura": "Lectura del paper", "guion": "Guion", "revision-guion": "Revisión del guion",
             "escribir-diapo": "Escribir diapos", "corregir-diapo": "Corregir diapos",
             "revisar-afirmaciones": "Revisar afirmaciones"}


def turnos(transcript: Path) -> list[dict]:
    """Un registro por petición al modelo (el transcript repite la petición por bloque)."""
    por_id: dict[str, dict] = {}
    for linea in transcript.read_text().splitlines():
        try:
            r = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if r.get("type") != "assistant" or "usage" not in r.get("message", {}):
            continue
        m = r["message"]
        rid = r.get("requestId") or m.get("id") or r.get("uuid")
        t = dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
        tipos = [c.get("type") for c in m.get("content", [])]
        # el transcript guarda output_tokens del comienzo de la respuesta, no el final: se estima
        # la salida por lo que se escribió (texto, razonamiento visible, argumentos de herramientas)
        chars = sum(len(json.dumps(c["input"], ensure_ascii=False)) if c.get("type") == "tool_use"
                    else len(c.get("text") or c.get("thinking") or "") for c in m.get("content", []))
        prev = por_id.get(rid)
        if prev is None:
            por_id[rid] = {"t": t, "u": m["usage"], "tipos": set(tipos), "chars": chars}
        else:
            prev["tipos"].update(tipos)
            prev["chars"] += chars
            prev["u"] = m["usage"]
    return sorted(por_id.values(), key=lambda x: x["t"])


CHARS_POR_TOKEN = 3.5


def tokens(tu: dict) -> dict:
    u = tu["u"]
    return {"nuevos": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
            "cache": u.get("cache_read_input_tokens", 0),
            "salida": max(u.get("output_tokens", 0), round(tu["chars"] / CHARS_POR_TOKEN))}


def etapa_de(nuevas: dict) -> str:
    claves = [k for k in ETIQUETAS if k in nuevas] or list(nuevas)
    return " + ".join(ETIQUETAS.get(k, k) for k in claves) or "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript", nargs="?", help="transcript .jsonl (por defecto, el más reciente del proyecto)")
    ap.add_argument("--trabajo", required=True, help="carpeta --claude de la corrida")
    args = ap.parse_args()
    if args.transcript:
        tr = Path(args.transcript)
    else:
        carpeta = Path.home() / ".claude" / "projects" / str(Path.cwd()).replace("/", "-")
        cands = sorted(carpeta.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not cands:
            raise SystemExit(f"No hay transcripts en {carpeta}; pasa la ruta del .jsonl")
        tr = cands[-1]
    rondas = [json.loads(l) for l in (Path(args.trabajo) / "tiempos.jsonl").read_text().splitlines() if l.strip()]
    if not rondas:
        raise SystemExit("tiempos.jsonl está vacío")

    # Intervalos de trabajo de Claude: tras la ronda k, responde sus tareas nuevas
    intervalos = [(0.0, rondas[0]["inicio"], "Arranque (skill, lanzar el pipeline)", None)]
    for k, r in enumerate(rondas):
        fin_hueco = rondas[k + 1]["inicio"] if k + 1 < len(rondas) else float("inf")
        nombre = etapa_de(r["nuevas"]) if r["nuevas"] else "Cierre (resumen final)"
        intervalos.append((r["fin"], fin_hueco, nombre, r))

    filas: dict[str, dict] = defaultdict(lambda: {"turnos": 0, "nuevos": 0, "cache": 0, "salida": 0,
                                                  "claude_s": 0.0, "pipeline_s": 0.0, "persona_s": 0.0})
    usuario = {"turnos": 0, "nuevos": 0, "cache": 0, "salida": 0}
    for a, b, nombre, r in intervalos:
        f = filas[nombre]
        if r is not None:
            hueco = (b - a) if b != float("inf") else 0.0
            f["persona_s" if "revision-guion" in r["nuevas"] else "claude_s"] += max(hueco, 0.0)
    for k, r in enumerate(rondas):                     # el pipeline de la ronda k+1 procesa lo respondido
        nombre = etapa_de(rondas[k - 1]["nuevas"]) if k else "Arranque (skill, lanzar el pipeline)"
        filas[nombre]["pipeline_s"] += r["fin"] - r["inicio"]
    # Caché vencida: un turno que no lee nada de caché tras el primero reescribe todo el contexto
    ts = turnos(tr)
    ttl = {"5m": sum(t["u"].get("cache_creation", {}).get("ephemeral_5m_input_tokens", 0) for t in ts),
           "1h": sum(t["u"].get("cache_creation", {}).get("ephemeral_1h_input_tokens", 0) for t in ts)}
    vencidas = [(ts[i]["t"] - ts[i - 1]["t"], tokens(ts[i])["nuevos"]) for i in range(1, len(ts))
                if not ts[i]["u"].get("cache_read_input_tokens") and tokens(ts[i])["nuevos"] > 5000]

    for tu in ts:
        tk = tokens(tu)
        if tu["tipos"] <= {"text", "thinking"}:        # sin herramientas: habla con el usuario
            for c in tk:
                usuario[c] += tk[c]
            usuario["turnos"] += 1
            continue
        nombre = next((n for a, b, n, _ in intervalos if a <= tu["t"] < b), "Otros")
        f = filas[nombre]
        f["turnos"] += 1
        for c in tk:
            f[c] += tk[c]

    fmt = lambda n: f"{n:,}".replace(",", ".")
    mins = lambda s: f"{s / 60:.1f}" if s else "-"
    print(f"# Tokens y tiempo por etapa\n\nTranscript: `{tr}`\n")
    print("| Etapa | Turnos | Tokens nuevos | Leídos de caché | Salida (estimada) | Claude (min) | Pipeline (min) | Persona (min) |")
    print("|---|---|---|---|---|---|---|---|")
    tot = defaultdict(float)
    for nombre, f in filas.items():
        if not (f["turnos"] or f["claude_s"] or f["pipeline_s"] or f["persona_s"]):
            continue
        print(f"| {nombre} | {f['turnos']} | {fmt(f['nuevos'])} | {fmt(f['cache'])} | {fmt(f['salida'])} | "
              f"{mins(f['claude_s'])} | {mins(f['pipeline_s'])} | {mins(f['persona_s'])} |")
        for c, v in f.items():
            tot[c] += v
    print(f"| Mensajes al usuario | {usuario['turnos']} | {fmt(usuario['nuevos'])} | {fmt(usuario['cache'])} | "
          f"{fmt(usuario['salida'])} | | | |")
    for c in ("turnos", "nuevos", "cache", "salida"):
        tot[c] += usuario[c]
    print(f"| **Total** | {int(tot['turnos'])} | {fmt(int(tot['nuevos']))} | {fmt(int(tot['cache']))} | "
          f"{fmt(int(tot['salida']))} | {mins(tot['claude_s'])} | {mins(tot['pipeline_s'])} | {mins(tot['persona_s'])} |")
    print("\nTokens nuevos = entrada sin caché + escritura en caché; «leídos de caché» se cobran mucho más "
          "barato. La salida se estima por lo escrito (~3,5 caracteres por token): el transcript no guarda "
          "el conteo final ni el razonamiento oculto, así que es un mínimo. El tiempo de Claude incluye "
          "pensar y escribir las respuestas.")
    if ttl["5m"] or ttl["1h"]:
        dur = "1 hora" if ttl["1h"] >= ttl["5m"] else "5 minutos (típico de un subagente)"
        print(f"\nCaché del prompt: {dur}.", end=" ")
        if vencidas:
            print("Se venció " + "; ".join(f"tras {g / 60:.1f} min sin actividad ({fmt(n)} tokens reescritos)"
                                           for g, n in vencidas) + ".")
        else:
            print("No se venció en ningún momento.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
