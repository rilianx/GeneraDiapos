---
name: paper2beamer
description: Genera una presentación LaTeX Beamer a partir de un paper (.pdf, .tex o .md) con el pipeline de este repo en modo Claude Code, sin API. Usar cuando se pida hacer o preparar diapositivas o una presentación de un paper.
allowed-tools: Bash(python beamer_graph.py:*), Bash(.venv/bin/python beamer_graph.py:*), Read, Write, mcp__paper2beamer__validar_frame, mcp__paper2beamer__dibujar_diagrama, mcp__paper2beamer__ver_pagina
---

El pipeline (`beamer_graph.py`) decide los pasos y valida; tú solo respondes las
tareas que deja. Todo lo que necesitas para cada una está en su archivo.
Usa `.venv/bin/python` si existe (dependencias en un entorno virtual); si no, `python`.

```bash
python beamer_graph.py papers/<nombre>.pdf --claude trabajo/<nombre> --out presentaciones/<nombre> --review
```

En ese primer comando, según lo que pida el usuario:
- PowerPoint (.pptx): `--pptx`.
- Otros archivos (otro paper, notas): `--extra ARCHIVO`, uno por archivo.
- Indicaciones para la presentación (público, duración, énfasis, idioma, secciones):
  `--instrucciones "…"` con sus palabras, o `--instrucciones archivo` si las dio en un archivo.

Ese primer comando empieza de cero (borra lo generado antes en esas carpetas). Para retomar
un trabajo interrumpido, usa solo el comando de abajo, sin el paper.

Luego, hasta que diga «Listo»:
1. Lee cada archivo de `trabajo/<nombre>/tareas/` (y una vez `trabajo/<nombre>/comun.md`,
   con los bloques que citan) y escribe la respuesta donde indica.
2. `python beamer_graph.py --claude trabajo/<nombre>`

Si están las herramientas `paper2beamer` (MCP), antes de guardar una diapositiva pásala por
`validar_frame` (con `trabajo` = la carpeta de arriba) y corrige lo que diga; en un diagrama
puedes probar el DOT con `dibujar_diagrama`, y `ver_pagina` muestra una página del paper.
Así el pipeline no gasta rondas en refinar.

Si una respuesta no valida, el comando lo dice: corrige ese archivo y vuelve a ejecutarlo.
Al terminar, muestra `presentaciones/<nombre>/presentacion.pdf` y los avisos de `informe.md`.

Reglas (el pipeline ya valida y reintenta; tu trabajo es solo responder):
- No modifiques código ni configuración (`*.py`, `base.tex`, `estilo.toml`, tests) ni
  instales nada. No leas el README ni el código: no hace falta.
- Si el comando falla con algo que no sea «RESPUESTA INVÁLIDA», muestra el error al
  usuario y detente; no intentes repararlo.
  Si el error es un módulo o comando que falta, sugiere `python verificar.py`.
- Responde todas las tareas de la ronda y recién entonces vuelve a ejecutar el comando.
- Sin comentarios entre rondas: habla con el usuario solo para el guion (revisión) y al
  final (ruta del PDF y avisos, en pocas líneas).
