---
name: paper2beamer
description: Genera una presentación LaTeX Beamer en español a partir de un paper científico (.pdf, .tex o .md) usando la plantilla base.tex y las reglas de estilo.toml de este repo, con guion revisable, validación automática (validar.py) y verificación de cada cifra contra el paper. Usar cuando se pida hacer, preparar o corregir diapositivas o una presentación de un paper.
---

# paper2beamer (con Claude)

Tú haces lo que en el pipeline hacen varios nodos con un modelo barato: leer el paper
entero, planificar, escribir y verificar. Lo determinista lo hace `validar.py`, que
reutiliza los chequeos de `beamer_graph.py`.

Archivos del usuario (no los modifiques salvo que lo pida):
- `base.tex`: preámbulo, tema, macros y marcadores `<<TITLE>>`, `<<AUTHORS>>`, `<<VENUE>>`, `%%SLIDES%%`.
- `estilo.toml`: `[guia]` (criterios de redacción), `[limites]` (reglas medibles),
  `[estructura]` (secciones en orden y si va agenda). Léelo completo antes de empezar.

## 1. Leer el paper completo

- Si hay fuente `.tex`, úsala: tablas y ecuaciones llegan exactas.
- Si es PDF, léelo con la herramienta Read **por páginas** (`pages`, máximo 20 por
  llamada) para ver tablas, ecuaciones y figuras como imagen. No confíes en una
  extracción a texto para las tablas: las rotadas o partidas entre páginas se rompen.
- Arma un **inventario de tablas**: número, título, qué métodos/filas/columnas tiene y
  qué mide cada columna. Muchos títulos engañan ("all the strategies" en una tabla que
  solo tiene variantes): decide por los encabezados, no por el título.
- Anota también dónde están en el texto las cifras clave (ganancias, totales) que no
  están en tablas.

## 2. Guion (revisión humana)

Escribe el guion como tabla en la conversación y **espera la aprobación del usuario**
antes de escribir diapositivas (sáltate la espera solo si el usuario lo pide):

| # | Sección | Tipo | Título (afirmación) | Contenido | Fuente exacta |
|---|---|---|---|---|---|

- Diapo 0 portada, diapo 1 agenda (si `[estructura].agenda`), luego contenido.
- Cada diapo de contenido en una sección de `[estructura].secciones`, en ese orden.
- Tipos: `bullets`, `columns`, `block`, `equation`, `table`, `algorithm`. Varíalos; como
  máximo `max_fraccion_vinetas` de diapos solo con viñetas.
- **Fuente exacta**: "Tabla 2 (p. 12), fila total" o "§5.1, párrafo 3", no "sec10".
- Si dudas qué tabla o pasaje corresponde, márcalo con ⚠ y nombra las candidatas.

## 3. Escribir la presentación

- Copia `base.tex` tal cual; rellena solo `<<TITLE>>`, `<<AUTHORS>>`, `<<VENUE>>` y
  reemplaza `%%SLIDES%%` por las diapositivas. No agregues paquetes ni macros: usa las
  de la base.
- Portada `\begin{frame}[plain]\titlepage\end{frame}`, agenda
  `\begin{frame}{Contenido}\tableofcontents\end{frame}` y `\section{...}` antes de la
  primera diapo de cada sección.
- Formatos: `columns` (dos `\column`), `block`/`alertblock`/`exampleblock`, ecuaciones
  en display con una línea que diga qué significan, tablas `booktabs`, pseudocódigo
  con `algorithm2e` (`\begin{algorithm}[H]`).
- Sigue `[guia]` y respeta `[limites]` (viñetas, palabras, bloques, `\alert`, tamaños).

Reglas de contenido (las que más fallaron en el pipeline):
- Toda cifra sale del paper con su **atribución exacta**: qué método, qué métrica, qué
  configuración. "Ganancia de lsmear-MG frente a lsmear" no es "ganancia de lsmear-MG".
- En una tabla, solo filas y columnas con datos en la fuente. Si un método no tiene
  datos, no lo pongas (nada de filas o columnas con `--`) o usa las cifras del texto.
- No copies filas ni crees columnas agregadas (promedios, mejor/peor) que el paper no da.
- Trabajo futuro va como futuro; no inventes extensiones ni comparaciones que el paper
  no hace (p. ej. "supera a X" si solo supera a una variante parecida a X).

## 4. Validar y corregir

```bash
python .claude/skills/paper2beamer/validar.py presentaciones/<nombre>/presentacion.tex \
    --paper <paper>
```

- Corrige todos los `ERROR` y vuelve a validar hasta 0 errores.
- Cada `aviso: Cifra N no aparece en las fuentes` se verifica a mano contra el paper
  (puede ser un formato distinto, p. ej. 21,035 vs 21035, o un error real).
- Los avisos de estilo se corrigen salvo que corregirlos empeore la diapo; dilo.

## 5. Verificación final de afirmaciones

Relee cada diapo contra el paper (las páginas, no tu memoria) y verifica cada
afirmación y cifra. Corrige lo que no se sostenga.

## 6. Entrega

En `presentaciones/<nombre>/`: `presentacion.tex`, `presentacion.pdf` (lo deja
`validar.py`) e `informe.md` con:
- el guion final,
- la tabla de verificación: diapo · afirmación o cifra · fuente (página/tabla) · estado,
- lo que quedó pendiente o dudoso, y los avisos de `validar.py` que no corregiste.

Muestra el PDF al usuario. No hagas commit ni push salvo que lo pida.
