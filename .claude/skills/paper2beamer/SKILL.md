---
name: paper2beamer
description: Genera una presentación LaTeX Beamer a partir de un paper (.pdf, .tex o .md) con el pipeline de este repo en modo Claude Code, sin API. Usar cuando se pida hacer o preparar diapositivas o una presentación de un paper.
---

El pipeline (`beamer_graph.py`) decide los pasos y valida; tú solo respondes las
tareas que deja. Todo lo que necesitas para cada una está en su archivo.

```bash
python beamer_graph.py papers/<nombre>.pdf --claude trabajo/<nombre> --out presentaciones/<nombre> --review
```

Luego, hasta que diga «Listo»:
1. Lee cada archivo de `trabajo/<nombre>/tareas/` y escribe la respuesta donde indica.
2. `python beamer_graph.py --claude trabajo/<nombre>`

Si una respuesta no valida, el comando lo dice: corrige ese archivo y vuelve a ejecutarlo.
Al terminar, muestra `presentaciones/<nombre>/presentacion.pdf` y los avisos de `informe.md`.
