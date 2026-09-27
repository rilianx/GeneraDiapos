# Informe de generación

Compilación global: OK (0 refinados globales)


## Avisos del guion (revisar las tablas usadas)

- Diapo 9: Duda del guion: ¿Figura 1 o resumen textual? No hay tabla para el perfil; usar el texto de sec10.
- Diapo 10: Duda del guion: tab2 parece la tabla adecuada; la leeré como subconjunto comparativo de variantes.
- Diapo 10: Menciona lsmear-LI, lsmear-LRI, que no aparecen en tab2 (encabezados: |Instance|lsmear|||lsmear|-MG||lsmear|-LR|| / ||time|#box|gain|time|#box|gain|time|#box|gain|): ¿es la tabla correcta? Si lo es, las cifras de esos métodos deben salir del texto
- Diapo 11: Duda del guion: tab3 es la comparación entre estrategias de búsqueda; usar solo las cifras relevantes.
- Diapo 11: Menciona FeasibleDiving, lsmear-MG, que no aparecen en tab3 (encabezados: ||minLB<br>ssum|ssr|MG|gain|Feasibl<br>ssum|eDiving<br>ssr|MG|gain| / |ex7_2_9|20|to|34|0.59|**14**|to|16|0.85|): ¿es la tabla correcta? Si lo es, las cifras de esos métodos deben salir del texto

| # | Diapositiva | Estado | Intentos | Avisos |
|---|---|---|---|---|
| 0 | \texttt{lsmear}: selección de variables con multiplicadores lagrangianos | ok | 0 | - |
| 1 | Contenido | ok | 0 | - |
| 2 | El branch-and-bound elige la variable que más promete | ok | 3 | Estilo: 3 bloques; máximo 2: combina con texto, lista o fórmula<br>Revisor: no_respaldada: «round-robin y largest-first no usan información del sistema.» (La fuente dice explícitamente que "round-robin ... does not require any information about the system"; para "largest-first" solo dice que "simply selects the variable with the largest domain" y que "is based on the assumption that intervals with large diameters have a greater impact on the function image", pero no afirma que no use información del sistema.)<br>Revisor: no_respaldada: «Una mala elección de variables puede multiplicar cajas y retrasar la convergencia.» (La fuente respalda solo la idea general de mal rendimiento: "One weakness of this strategy is that a bad initial ordering of variables can lead to disastrous performance." No menciona explícitamente multiplicación de cajas ni retraso de la convergencia.) |
| 3 | Los métodos smear tratan todas las restricciones igual | ok | 0 | Revisor: no_respaldada: «smear-based methods usan derivadas intervalares y anchos de variables.» (La fuente dice que el impacto se calcula con el «smear value» y que «a_ji is an interval overestimate of the range of the partial derivative ...»; no menciona explícitamente anchos de variables en el texto visible.)<br>Revisor: no_respaldada: «La agregación es global y no distingue restricciones por su contribución local.» (La fuente solo dice «an aggregation of this value in the whole system»; no formula explícitamente la ausencia de distinción por contribución local.)<br>Revisor: no_respaldada: «La decisión puede perder sensibilidad a la factibilidad.» (No aparece en el contexto ninguna afirmación sobre pérdida de sensibilidad a la factibilidad.)<br>Revisor: no_respaldada: «Al bisecar, el impacto se redistribuye entre subproblemas.» (El contexto explica qué hace la bisección y cómo se elige la variable, pero no dice que el impacto se redistribuya entre subproblemas.) |
| 4 | \texttt{lsmear} pondera restricciones con duales óptimos | ok | 0 | - |
| 5 | La linealización produce una sensibilidad ponderada | ok | 0 | - |
| 6 | El algoritmo separa linealización y selección | ok | 0 | - |
| 7 | Cuatro variantes aíslan el valor de cada idea | ok | 3 | Estilo: Formato (columns): usa un entorno columns con dos \column |
| 8 | El experimento usa IbexOpt y COCONUT | ok | 0 | - |
| 9 | \texttt{lsmear} supera a las estrategias clásicas | ok | 1 | - |
| 10 | \texttt{lsmear-MG} y \texttt{lsmear-LR} lideran entre variantes | ok | 0 | - |
| 11 | \texttt{lsmear-MG} también domina con otra búsqueda | ok | 3 | Estilo: Demasiados \alert (máximo 2) |
| 12 | La idea central mejora selección y simplicidad | ok | 0 | - |

## Consumo de tokens

| Modelo | Llamadas | Entrada | Salida | Total |
|---|---|---|---|---|
| gpt-5.4-mini | 41 | 123,152 | 23,561 | 146,713 |
| **Total** | 41 | 123,152 | 23,561 | 146,713 |
