# Informe de generación

Compilación global: OK (0 refinados globales)


| # | Diapositiva | Estado | Intentos | Avisos |
|---|---|---|---|---|
| 0 | lsmear mejora la selección en B\&B intervalar | ok | 0 | - |
| 1 | El problema es elegir bien la variable a bisecar | ok | 1 | - |
| 2 | Las heurísticas clásicas ignoran la importancia relativa | ok | 3 | Estilo: 4 bloques; máximo 2: combina con texto, lista o fórmula<br>Revisor: no_respaldada: «Título: «Heurísticas clásicas: selección de variables»» (El contexto habla de estrategias de branching/selección de variables, pero no presenta esa clasificación ni usa la etiqueta «heurísticas clásicas».) |
| 3 | El trabajo relacionado apunta a ponderar restricciones | ok | 1 | - |
| 4 | lsmear pondera la Lagrangiana con duales lineales | ok | 1 | - |
| 5 | La selección usa un smear de la Lagrangiana | ok | 2 | - |
| 6 | El algoritmo separa linealización y decisión | ok | 0 | - |
| 7 | Los experimentos usan IbexOpt y COCONUT | ok | 0 | - |
| 8 | lsmear supera a las heurísticas clásicas | failed | 3 | Compilación: Desborde: contenido demasiado ancho por 13.32317pt (línea 33: `\end{frame}`)<br>Revisor: contradicha: «En la tabla, lsmear tiene ganancia 1.07» (La tabla muestra 1.07 en la columna gain para lsmear-MG, no para lsmear. Para lsmear, la ganancia es 0.91 en la fila total, y en la figura/texto la ganancia media respecto a ssum es 1.30.)<br>Revisor: no_respaldada: «En la tabla, ssum tiene ganancia 1.30» (En el contexto no aparece una columna o fila para ssum con ese total; 1.30 se reporta como ganancia media relativa de lsmear respecto de ssum, no como "gain" tabulado para ssum.)<br>Revisor: no_respaldada: «En la tabla, ssr tiene tiempo total 20 429 s» (20,429 aparece en la última fila para la tercera variante de lsmear (lsmear/-LR), no para ssr.)<br>Revisor: no_respaldada: «En la tabla, ssr tiene ganancia 0.94» (0.94 aparece en la última fila para la tercera variante de lsmear (lsmear/-LR); no hay tabla de ssr en el contexto.) |
| 9 | \texttt{lsmear-MG} y \texttt{lsmear-LR} son las mejores variantes | ok | 3 | Estilo: Demasiadas negritas (máximo 3)<br>Revisor: contradicha: «lsmear-LI empeora al usar la Lagrangiana lineal» (La fuente dice que lsmear-LI y lsmear-LRI "report the worst results" porque usan la Lagrangiana de la relajación lineal; no solo lsmear-LI sino ambas son las peores, y la diapositiva lo presenta como una limitación específica de lsmear-LI/LRI sin indicar que sea una conclusión general del paper.) |
| 10 | La estrategia de búsqueda también importa | failed | 3 | Compilación: Error: Misplaced \omit. (línea 23: `\end{frame}`) contexto: \multispan ->\omit \@multispan<br>Estilo: Demasiados \alert (máximo 2) |
| 11 | lsmear reduce el árbol sin costo algorítmico alto | ok | 1 | - |
| 12 | El trabajo futuro incorporará violación de restricciones | ok | 1 | - |

## Consumo de tokens

| Modelo | Llamadas | Entrada | Salida | Total |
|---|---|---|---|---|
| gpt-5.4-mini | 50 | 161,970 | 21,876 | 183,846 |
| **Total** | 50 | 161,970 | 21,876 | 183,846 |
