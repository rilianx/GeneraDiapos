---
name: paper2beamer
description: Genera una presentación LaTeX Beamer en español a partir de un paper científico (.pdf, .tex o .md) con el pipeline de este repo en modo Claude Code (sin API): el grafo de beamer_graph.py decide los pasos y valida; Claude responde las tareas que el grafo deja en archivos. Usar cuando se pida hacer, preparar o corregir diapositivas o una presentación de un paper.
---

# paper2beamer en modo Claude Code

El control es el grafo de `beamer_graph.py`, el mismo del pipeline de GitHub: qué se
pide, en qué orden, qué se valida, cuántos reintentos, el revisor, `best_frame`, las
secciones y el informe. **Tú solo respondes las tareas que el grafo deja**; no decides
pasos ni te saltas validaciones. Ver `driver_claude.py`.

## Ciclo

```bash
# empezar (una vez); nombre = nombre del paper sin extensión
python beamer_graph.py papers/<nombre>.pdf --claude trabajo/<nombre> \
    --out presentaciones/<nombre> --review
# después de responder las tareas, repetir hasta que diga "Listo"
python beamer_graph.py --claude trabajo/<nombre>
```

- Salida 3: hay tareas. Cada una está en `trabajo/<nombre>/tareas/NN_<tipo>_<id>.md` y
  dice en qué archivo de `trabajo/<nombre>/respuestas/` va la respuesta.
- Salida 0: listo; el resultado está en `presentaciones/<nombre>/` (`presentacion.pdf`,
  `informe.md`, `outline.json`).
- Si una respuesta no valida (JSON que no cumple el esquema, guion inválido), el driver
  lo dice y vuelve a pedirla: corrige solo ese archivo.
- No edites `estado.sqlite` ni `pendientes.json`. Si hay varias tareas, respóndelas
  todas antes de volver a ejecutar.

## Cómo responder cada tipo de tarea

- **lectura** (siempre la primera): lee el paper **completo como imagen** (Read con
  `pages`, de a 20) y devuelve solo el inventario pedido: tablas (número, página,
  encabezados y métodos tal como están escritos, qué mide cada celda), algoritmos,
  ecuaciones clave y páginas de cada sección. No transcribas. El grafo verifica contra
  la capa de texto del PDF que esas páginas, tablas, encabezados y métodos existan.
  Desde ahí las tareas solo traen referencias ("Table 2 (pág. 14)", "§5.2 (págs. 13-14)"):
  apóyate en lo que viste y vuelve a mirar la página si ya no la tienes presente (por
  ejemplo, si la sesión se resumió o retomaste otro día).
- **guion**: JSON con el esquema indicado. Las fuentes son los ids del inventario
  (secciones, tablas, algoritmos, ecuaciones). Decide qué tabla corresponde por sus
  encabezados, no por el título. Si dudas, usa el campo `aviso`.
- **revision-guion**: muestra `trabajo/<nombre>/guion.md` al usuario y **espera su
  aprobación**. Escribe el guion final (con los cambios que pida) en la respuesta.
- **escribir-diapo / corregir-diapo**: solo el `\begin{frame}...\end{frame}`, siguiendo
  el prompt al pie de la letra (plantilla del tipo, límites, macros disponibles).
  Toda cifra con su atribución exacta (método, métrica, configuración); verifícala en
  la página del paper si viene de una tabla.
- **revisar-afirmaciones**: JSON `Review`. Sé exigente con cifras y atribuciones, pero no
  marques paráfrasis fieles ni rótulos. Para `contradicha` cita el pasaje.
- **corregir-presentacion**: solo el cuerpo pedido.

Al terminar, muestra al usuario `presentaciones/<nombre>/presentacion.pdf` y los avisos
de `informe.md`. No hagas commit ni push salvo que lo pida.

## Validar una presentación editada a mano

```bash
python .claude/skills/paper2beamer/validar.py presentaciones/<nombre>/presentacion.tex --paper <paper>
```

Aplica los mismos chequeos (compilación por diapo, estilo, tablas, estructura, preámbulo,
cifras) a un `.tex` fuera del grafo.
