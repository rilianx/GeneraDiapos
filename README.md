# paper2beamer

Genera presentaciones LaTeX Beamer a partir de papers científicos con un pipeline
de LangGraph. El modelo escribe **una diapositiva a la vez**, cada una se compila y
valida por separado, y solo las que pasan se ensamblan en tu plantilla.

**Flujo en GitHub** (con revisión humana del guion por defecto):

```mermaid
flowchart LR
    A([Subes un paper<br/>a papers/ en main]) --> B{"REVISAR_GUION<br/>(por defecto true)"}
    B -->|true| C["Workflow: --solo-guion<br/>1 llamada LLM"]
    C --> D[/"PR «Guion para revisar»<br/>guiones/nombre.json + .md"/]
    D --> E[/"Persona revisa fuentes y tipos<br/>edita el .json si hace falta"/]
    E --> F["Fusiona el PR<br/>guiones/nombre.json llega a main"]
    F --> G["Workflow: --guion<br/>grafo sin generar guion"]
    B -->|false| H["Workflow: completo<br/>grafo con guion del LLM"]
    G --> I[/"PR «Presentaciones generadas»<br/>presentaciones/nombre/"/]
    H --> I
    I --> J[/"Persona revisa informe.md<br/>y fusiona"/]
    classDef human fill:#e6f4ea,stroke:#2f855a,color:#000
    classDef wf fill:#e3eefc,stroke:#2b6cb0,color:#000
    class D,E,F,I,J human
    class C,G,H wf
```

**Grafo de estados** (LangGraph; en naranjo los nodos que llaman al LLM, en verde la
intervención humana):

```mermaid
flowchart TD
    start([paper: .pdf / .tex / .md]) --> ingest

    subgraph PRINCIPAL["Grafo principal (State)"]
        ingest["<b>ingest</b><br/>preflight de base.tex<br/>extracción + troceado<br/>sec* · eq* · tab* · alg*<br/>tablas: título, unión, dañadas"]
        outline["<b>outline</b> · LLM guion<br/>razonamiento medium<br/>portada + agenda + secciones<br/>ajustar_kinds + validate_outline<br/>hasta 3 intentos con feedback"]
        cargar["<b>cargar_guion</b><br/>guion revisado (--guion)<br/>sin LLM, misma validación"]
        review_outline{"<b>review_outline</b><br/>¿--review?"}
        pausa[/"pausa: la persona edita<br/>outline_borrador.json"/]
        fan_out[["<b>fan_out</b><br/>Send × N diapositivas<br/>en paralelo"]]
        assemble["<b>assemble</b><br/>frames dentro de base.tex<br/>\section{} por sección<br/>fallida → marcador pendiente"]
        compile_full{"<b>compile_full</b><br/>pdflatex × 2"}
        refine_global["<b>refine_global</b> · LLM<br/>solo el cuerpo, no la base"]
        write_outputs(["<b>write_outputs</b><br/>presentacion.tex / .pdf<br/>outline.json · informe.md<br/>(avisos + tokens)"])

        ingest -->|"sin outline_path"| outline
        ingest -->|"outline_path"| cargar
        outline --> review_outline
        cargar --> review_outline
        review_outline -->|no| fan_out
        review_outline -->|sí| pausa --> fan_out
        assemble --> compile_full
        compile_full -->|"errores y global_attempts < 2"| refine_global --> compile_full
        compile_full -->|"ok, o intentos agotados"| write_outputs
    end

    fan_out --> write_slide

    subgraph SLIDE["Subgrafo por diapositiva (SlideState)"]
        write_slide["<b>write_slide</b> · LLM<br/>plantilla del kind<br/>portada y agenda: sin LLM"]
        compile_slide{"<b>compile_slide</b><br/>lint · pdflatex aislado<br/>kind_check · style_check<br/>si compila → best_frame"}
        refine_slide["<b>refine_slide</b> · LLM<br/>errores + estilo + afirmaciones<br/>attempts + 1"]
        review_slide{"<b>review_slide</b> · LLM revisor<br/>afirmaciones vs fuentes<br/>+ índice de tablas<br/>reviews + 1"}
        refine_facts["<b>refine_facts</b> · LLM<br/>corrige afirmaciones<br/>no gasta attempts"]
        finish_slide(["<b>finish_slide</b><br/>si falla y hay best_frame → la usa<br/>avisos: compilación, estilo,<br/>revisor, cifras"])

        write_slide --> compile_slide
        compile_slide -->|"errores o estilo<br/>y attempts < 3"| refine_slide --> compile_slide
        compile_slide -->|"compila y reviews < 2<br/>(no portada)"| review_slide
        compile_slide -->|"no compila tras 3,<br/>portada o reviews = 2"| finish_slide
        review_slide -->|"afirmaciones sin respaldo<br/>y reviews < 2"| refine_facts --> compile_slide
        review_slide -->|"todo respaldado,<br/>o ya se verificó"| finish_slide
    end

    finish_slide --> assemble

    classDef llm fill:#fde7c8,stroke:#c77700,color:#000
    classDef code fill:#e3eefc,stroke:#2b6cb0,color:#000
    classDef human fill:#e6f4ea,stroke:#2f855a,color:#000
    class outline,refine_global,write_slide,refine_slide,review_slide,refine_facts llm
    class ingest,cargar,fan_out,assemble,compile_full,compile_slide,finish_slide,write_outputs,review_outline code
    class pausa human
```

## Estructura

```
├─ beamer_graph.py           # pipeline completo
├─ base.tex                  # TU plantilla: preámbulo, tema, macros, %%SLIDES%%
├─ estilo.toml               # TUS reglas de estilo: guía + límites medibles
├─ requirements.txt
├─ tests/smoke_test.py       # prueba con LLM simulado (no gasta API)
├─ papers/                   # entradas: .pdf, .tex o .md
├─ presentaciones/           # salidas (las crea el workflow)
├─ ejemplos/lsmear/          # presentación de referencia hecha a mano
├─ CLAUDE.md                 # contexto para trabajar en el repo con Claude
└─ .github/workflows/
   ├─ tests.yml              # smoke test en cada push/PR
   └─ generar.yml            # genera presentaciones y abre un PR
```

## Pasos para dejarlo funcionando

### 1. Local

Requisitos: Python 3.11 o superior (usa `tomllib`) y TeX Live con `beamer`,
`metropolis`, `algorithm2e`, `booktabs` y `tikz`.

```bash
# Debian/Ubuntu
sudo apt-get install texlive-latex-recommended texlive-latex-extra \
  texlive-pictures texlive-science texlive-fonts-recommended
# macOS: MacTeX. Windows: MiKTeX o TeX Live.

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python tests/smoke_test.py          # debe terminar en "OK"
```

### 2. Primera generación local

```bash
export OPENAI_API_KEY="tu-key"      # Windows: set OPENAI_API_KEY=tu-key
python beamer_graph.py papers/mi_paper.pdf --out salida --review
```

Con `--review` el pipeline se detiene tras el guion, lo guarda en
`salida/outline_borrador.json` y espera a que lo edites y pulses Enter.

### 3. GitHub

1. Sube el contenido del zip a tu repo (rama `main`).
2. **Settings → Secrets and variables → Actions → Secrets**: crea
   `OPENAI_API_KEY` (o `ANTHROPIC_API_KEY` si usas Claude).
3. **Settings → Secrets and variables → Actions → Variables** (opcional):
   - `LLM_PROVIDER`: `openai` (por defecto) o `anthropic`
   - `LLM_MODEL_OUTLINE`: tu modelo más capaz
   - `LLM_MODEL_SLIDES`: uno más rápido y barato
4. **Settings → Actions → General → Workflow permissions**: marca
   *Read and write permissions* y *Allow GitHub Actions to create and approve pull
   requests*. Sin esto `generar.yml` no puede abrir el PR.
5. Comprueba que el workflow `tests` pase en verde.

### 4. Uso diario en GitHub

Por defecto hay **revisión humana del guion** (el paso más barato de corregir):

1. Sube un paper a `papers/` en `main` → llega un PR **"Guion para revisar"** con
   `guiones/<nombre>.json` y una vista legible `guiones/<nombre>.md`.
2. Empieza por la sección **⚠ Revisar primero** del `.md`: dudas que el propio modelo
   declaró (campo `aviso`) y diapos que mencionan métodos que no aparecen en las tablas
   que citan, o que citan una tabla dañada. Luego revisa el orden, el tipo de cada diapo
   y qué fragmentos usa (`sources`; las tablas del paper están listadas al inicio). Si
   algo está mal, edita el `.json` en el mismo PR.
3. Fusiona el PR → se generan las diapositivas desde ese guion y llega un segundo PR
   con `presentaciones/<nombre>/` (`presentacion.tex`, `.pdf`, `outline.json`,
   `informe.md`). El PDF también queda en los artefactos del run.
4. Revisa `informe.md` antes de aprobar: estado de cada diapo, intentos, avisos del
   revisor de afirmaciones, de estilo y cifras a verificar.

Para generar todo de una vez: variable del repo `REVISAR_GUION=false`, o
**Actions → generar presentacion → Run workflow** con modo `completo`. Desde ahí
también puedes lanzar solo el guion (modo `guion`) o las diapos desde un guion ya
revisado (campo `guion`).

En local lo equivalente es `--solo-guion guiones/x.json` y luego `--guion guiones/x.json`
(o `--review`, que pausa en la terminal).

## Modo Claude Code (sin API)

El mismo grafo puede correr **dentro de Claude Code**, con tu suscripción y sin API
key: con `--claude DIR` (proveedor `claude`), cada llamada al modelo pausa el grafo
(`interrupt` de LangGraph), el estado queda en `DIR/estado.sqlite` y la tarea se
escribe en `DIR/tareas/`. Claude la responde en `DIR/respuestas/` y, al volver a
ejecutar el comando, el grafo valida la respuesta y continúa donde iba. Las
validaciones, reintentos, revisor, secciones e informe son exactamente los del
pipeline; solo cambia quién responde.

```bash
python beamer_graph.py papers/x.pdf --claude trabajo/x --out presentaciones/x --review
python beamer_graph.py --claude trabajo/x      # repetir hasta que diga "Listo"
```

En Claude Code basta con pedir "haz la presentación de papers/x.pdf": la skill
`.claude/skills/paper2beamer/` es mínima: solo explica el ciclo, porque lo que hay que
hacer en cada paso (leer el paper como imagen, escribir una diapo, revisarla, esperar
tu aprobación del guion) viaja en el texto de cada tarea, generado por el grafo.
`validar.py`, en la misma carpeta, aplica los chequeos a un `.tex` editado a mano.

## Configuración

| Qué | Dónde | Notas |
|---|---|---|
| Proveedor y modelos | variables `LLM_PROVIDER`, `LLM_MODEL_OUTLINE`, `LLM_MODEL_SLIDES` | por defecto OpenAI; los nombres por defecto pueden quedar obsoletos, usa los de tu cuenta |
| Razonamiento del guion | variable `LLM_REASONING_OUTLINE` | `low`, `medium` (por defecto) o `high`; solo OpenAI. `LLM_REASONING_SLIDES` hace lo mismo para las diapos (por defecto, sin razonamiento extra) |
| Revisor de afirmaciones | variables `LLM_MODEL_REVIEW` (por defecto el de diapos) y `LLM_REASONING_REVIEW` (por defecto `medium`) | cada diapo que compila se verifica contra sus fragmentos; lo no respaldado se corrige una vez (presupuesto propio, `MAX_REVIEWS`) y, si persiste, queda en `informe.md` |
| Preámbulo, tema, macros | `base.tex` | debe tener exactamente un `%%SLIDES%%` |
| Título/autores | `<<TITLE>>`, `<<AUTHORS>>`, `<<VENUE>>` en `base.tex` | se rellenan desde el guion; si los escribes a mano se respetan |
| Figuras | automático (PDF) | al leer el paper se recortan sus figuras en `<salida>/figuras/figN.png` (desde el pie «Fig. N»/«Figure N»); el guion las cita como `fig*` y el tipo `figure` las inserta con `\includegraphics` |
| Secciones y agenda | `[estructura]` en `estilo.toml` | `secciones` (orden de la presentación) y `agenda` (diapo con `\tableofcontents`); la portada siempre va primero |
| Reglas de estilo | `estilo.toml` | `[guia]` va al prompt; `[limites]` se verifica en código |
| Reintentos, tolerancias, nº de diapos | constantes al inicio de `beamer_graph.py` | `MAX_SLIDE_ATTEMPTS`, `OVERFULL_TOLERANCE_PT`, `N_SLIDES`… |
| Extracción de PDF | `--extractor marker` | mejor con ecuaciones; requiere `pip install marker-pdf` |
| Paralelismo | `--concurrency` | bájalo si chocas con límites de tasa de la API |

Opciones de la CLI: `python beamer_graph.py --help`.

## Cómo funciona (notas de diseño)

1. **La base es tuya y el modelo no la toca.** El modelo solo devuelve un
   `\begin{frame}…\end{frame}`. El lint rechaza `\usepackage`, `\newcommand`,
   `\documentclass`, `\input`, `\write18`. Los paquetes y macros de tu base se
   detectan solos y se le informan al modelo en cada diapo.
2. **Validación al inicio.** Antes de gastar llamadas se comprueba que la base
   tenga el marcador, que sus paquetes existan (`kpsewhich`) y que compile vacía.
3. **Recuperación estructural, no RAG.** El paper se trocea por secciones, y las
   ecuaciones, tablas y algoritmos quedan como fragmentos propios (`eq1`, `tab2`,
   `alg1`). El guion (que sí ve el paper completo) asigna a cada diapo los IDs que
   necesita, y cada diapo recibe solo esos, junto con su sección, su aviso del guion y
   la lista de títulos de todo el guion (para no repetir a las vecinas). Es trazable y recupera bien tablas y
   fórmulas, que la búsqueda semántica recupera mal. RAG tendría sentido con
   varios papers o documentos muy largos.
4. **Cada diapo se compila sola**, con la cabecera real de la base, en su propio
   directorio temporal (seguro en paralelo). Los errores quedan localizados y el
   refinado trabaja con un frame pequeño.
5. **Beamer reporta los errores en `\end{frame}`** porque lee el frame como
   argumento; el parser del log añade el token culpable como contexto.
6. **Notación compartida.** Las diapos se escriben en paralelo sin verse entre sí;
   el guion define un glosario que reciben todas para no inventar símbolos distintos.
7. **Estilo en tres capas:** la guía va al guion y a cada diapo; los límites se
   validan en el guion (nº de puntos) y en cada frame; lo visual queda pendiente
   (ver hoja de ruta).
8. **El estilo nunca descarta una diapo que compila.** Si tras los intentos sigue
   incumpliendo estilo, se conserva con aviso. Solo una diapo que no compila se
   reemplaza por un marcador "pendiente de revisión".
9. **Chequeo de cifras.** Avisa si un número de la diapo no aparece en sus
   fragmentos fuente. No bloquea; queda en el informe.
10. **Seguridad al compilar código generado:** `-no-shell-escape`, timeout y
    directorio aislado.
11. **Modelo según el nodo:** el más capaz para el guion (la decisión que más
    pesa), uno rápido para las N diapos y los refinados.
12. **La portada es determinista** (`\titlepage`), sin LLM.

## Limitaciones conocidas

- La extracción con `pymupdf4llm` degrada ecuaciones; para papers con mucha
  matemática usa `--extractor marker` o, si existe, la fuente `.tex` (arXiv).
- La detección de tablas y algoritmos en Markdown es heurística.
- El conteo de palabras del estilo aproxima: ignora comandos y cuenta cada
  fórmula como una palabra.
- El chequeo de cifras puede dar falsos positivos (números formateados distinto
  que en la fuente).
- No usa figuras del paper, solo texto.
- `MemorySaver` no persiste entre ejecuciones: si el proceso se corta, se
  empieza de nuevo.

## Hoja de ruta

- [ ] Revisión visual: renderizar cada página a PNG y pasarla a un modelo
      multimodal con una rúbrica corta (saturación, legibilidad, equilibrio).
- [ ] Figuras: extraer imágenes del PDF como fragmentos `fig*` y permitir
      `\includegraphics`.
- [ ] Persistencia con `SqliteSaver` para reanudar sin repetir llamadas.
- [ ] Trazas con LangSmith.
- [ ] Conjunto de evaluación (5–10 papers): tasa de compilación al primer
      intento, refinados promedio, avisos de cifras.
- [ ] Notas del presentador con `\note{}`.
- [ ] Nodo de coherencia narrativa que lea todos los títulos en orden.
- [x] Revisor LLM de afirmaciones contra la fuente (`review_slide`).
- [ ] Juez LLM para reglas no medibles (títulos que afirman, una idea por diapo).

## Costes y cuentas

- Las suscripciones de chat (ChatGPT, Claude) **no incluyen** la API; la key se
  crea y factura aparte (platform.openai.com o la Claude Console).
- Nunca pongas la key en el código ni en un commit: usa variables de entorno en
  local y *secrets* en GitHub.
- Coste aproximado por presentación: 1 llamada de guion + N diapos + refinados
  (máximo 3 por diapo) + hasta 2 refinados globales. `informe.md` muestra los
  intentos reales.
