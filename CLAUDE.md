# Contexto para trabajar en este repo

> **¿Te pidieron una presentación?** Usa la skill `paper2beamer` y sigue sus reglas: no
> modifiques código ni tests, y si el pipeline falla, muestra el error y detente. Lo que
> sigue es solo para cuando te piden cambiar el código del pipeline.

Pipeline de LangGraph que convierte papers en presentaciones Beamer. Ver README.md
para el flujo completo y las decisiones de diseño.

## Invariantes (no romper)

- El modelo solo genera frames sueltos. `base.tex` es del usuario y ningún nodo la
  modifica; `refine_global` solo edita el cuerpo entre `head` y `tail`.
- Toda llamada a un LLM pasa por `call_text` o `call_structured`. Así el smoke
  test puede simularlas y el proveedor se cambia en un solo lugar (`_chat`).
  Con `LLM_PROVIDER=claude` esas dos funciones hacen `interrupt()` (`pedir_a_claude`);
  `driver_claude.py` solo transporta tareas y respuestas, no decide pasos.
  En ese modo el nodo `lectura` reemplaza tablas/algoritmos/ecuaciones extraídos por un
  inventario que Claude arma viendo las páginas (validado con `validar_lectura`), y las
  tareas llevan referencias a páginas (`fuentes_claude`) en vez del texto. El driver solo
  compacta el transporte: los párrafos repetidos entre tareas van a `comun.md`
  (`compactar` / `expandir`), sin cambiar el prompt que arma el grafo.
- Una diapo que compila nunca se descarta por estilo: queda con aviso.
  Si un refinado posterior rompe la compilación, se usa `best_frame` (la última que compiló).
- Cada compilación ocurre en su propio directorio temporal, con
  `-no-shell-escape` y timeout.
- La configuración del usuario vive en `base.tex`, `estilo.toml` y variables de
  entorno; no hardcodear modelos, paquetes ni reglas de estilo en prompts.

## Mapa del código (`beamer_graph.py`)

- Configuración y esquemas (`SlideSpec`, `Outline`, `State`, `SlideState`)
- LLM: `_chat`, `call_text`, `call_structured`
- Extracción: `extract_text`, `chunk_document`, `extraer_figuras` (PNG por figura, fragmentos `fig*`)
- LaTeX: `preflight`, `compile_tex`, `parse_log`, `lint_frame`, `soft_checks`
- PowerPoint (`pptx_export.py`, `--pptx`): se deriva de los frames finales en `write_outputs`
  (`exportar_pptx`); Beamer sigue siendo la fuente de verdad. pandoc solo traduce el texto y las
  fórmulas (OMML en `mc:AlternateContent`, con fallback de imagen compilada con LaTeX).
- Diagramas: `render_diagrama` / `expandir_diagramas` (`motor_diagrama` elige: DOT → `render_diagrama`,
  PDF+PNG con `DIAGRAMA_ESTILO`; Mermaid → `render_mermaid`, PNG con `MERMAID_CONFIG`; `regla_diagramas`), se expanden en `compile_slide`, `assemble` y `exportar_pptx`; el frame guardado
  conserva el DOT (lo ven el revisor y los refinados)
- MCP (`mcp_servidor.py`, `.mcp.json`): herramientas de solo lectura (`validar_frame`,
  `dibujar_diagrama`, `ver_pagina`) que reusan las funciones del pipeline; no escriben estado ni registro
- Fuentes y pedido: `fragmentos_extra` (`--extra`, ids `x{n}…`), `bloque_instrucciones` (`--instrucciones`,
  en `OUTLINE_PROMPT` y `SLIDE_PROMPT`), `regla_secciones` (secciones libres o las de `estilo.toml`),
  `tex_a_pptx` (`--a-pptx`)
- Registro de errores: `registrar_errores` (desde `compile_slide`), `categoria_error`, `errores_previos`
  (aviso en `SLIDE_PROMPT`); archivo `ERRORES_LOG` (`BEAMER_ERRORES`)
- Estilo: `load_style`, `guia_guion` / `guia_notas` (reglas solo del guion / de las notas), `describe_limits`, `style_check`
- Notas del expositor (`--notas`): cada diapo trae su `\note{}` (`regla_notas` en `SLIDE_PROMPT`,
  `nota_check`); `sin_nota` la quita antes de los chequeos de estilo; `separar_nota` / `notas_de`
  la sacan del frame final para `notas.md` y el panel de notas del .pptx
- Prompts: `OUTLINE_PROMPT`, `SLIDE_PROMPT`, `REFINE_SLIDE_PROMPT`, `REFINE_GLOBAL_PROMPT`
- Base: `base_packages`, `base_files`, `base_macros`, `fill_base`, `split_base`, `standalone`
- Guion: `indice_tablas`, `cargar_guion` (guion revisado desde JSON), `guion_md` (vista para el PR),
  `guion_doc_md` / `guion_desde_md` (guion como documento editable, ida y vuelta en código)
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
