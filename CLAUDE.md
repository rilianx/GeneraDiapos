# Contexto para trabajar en este repo

Pipeline de LangGraph que convierte papers en presentaciones Beamer. Ver README.md
para el flujo completo y las decisiones de diseño.

## Invariantes (no romper)

- El modelo solo genera frames sueltos. `base.tex` es del usuario y ningún nodo la
  modifica; `refine_global` solo edita el cuerpo entre `head` y `tail`.
- Toda llamada a un LLM pasa por `call_text` o `call_structured`. Así el smoke
  test puede simularlas y el proveedor se cambia en un solo lugar (`_chat`).
- Una diapo que compila nunca se descarta por estilo: queda con aviso.
  Si un refinado posterior rompe la compilación, se usa `best_frame` (la última que compiló).
- Cada compilación ocurre en su propio directorio temporal, con
  `-no-shell-escape` y timeout.
- La configuración del usuario vive en `base.tex`, `estilo.toml` y variables de
  entorno; no hardcodear modelos, paquetes ni reglas de estilo en prompts.

## Mapa del código (`beamer_graph.py`)

- Configuración y esquemas (`SlideSpec`, `Outline`, `State`, `SlideState`)
- LLM: `_chat`, `call_text`, `call_structured`
- Extracción: `extract_text`, `chunk_document`
- LaTeX: `preflight`, `compile_tex`, `parse_log`, `lint_frame`, `soft_checks`
- Estilo: `load_style`, `describe_limits`, `style_check`
- Prompts: `OUTLINE_PROMPT`, `SLIDE_PROMPT`, `REFINE_SLIDE_PROMPT`, `REFINE_GLOBAL_PROMPT`
- Base: `base_packages`, `base_files`, `base_macros`, `fill_base`, `split_base`, `standalone`
- Nodos principales: `ingest`, `outline`, `review_outline`, `fan_out`, `assemble`,
  `compile_full`, `refine_global`, `write_outputs`
- Subgrafo: `write_slide`, `compile_slide`, `route_slide`, `review_slide`, `route_review`,
  `refine_slide`, `refine_facts`, `finish_slide`

## Antes de dar un cambio por terminado

```bash
python tests/smoke_test.py      # debe imprimir OK
```

Si el cambio toca prompts o validaciones, amplía el smoke test con un caso que
lo ejercite (el LLM simulado está en `fake_text` / `fake_structured`).

## Convenciones

- Código y comentarios en español, como el resto del repo.
- Nuevas reglas de estilo: límite en `estilo.toml` + default en `STYLE_DEFAULTS`
  + comprobación en `style_check` + texto en `describe_limits`.
- Nunca incluir API keys en código, tests ni ejemplos.
