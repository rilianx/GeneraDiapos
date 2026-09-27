# Guion: lsmear: una estrategia de selección de variables

Paper: `papers/lsmear.pdf`

Revisa el orden, el tipo de cada diapo y sobre todo **qué fragmentos usa** (`sources`). Para cambiar algo edita el `.json` de este PR; al fusionarlo se generan las diapositivas.

## Tablas del paper

- `tab1`: Table 1
- `tab2`: Table 2: Average CPU time and gain for all the strategies in a subset of instances. In bold, the best CPU times and the smallest number of boxes reported on each instance
- `tab3`: Table 3: Average CPU time for the strategies in a subset of instances. In bold, the best CPU time reported on each instance

## 0. lsmear: una estrategia de selección de variables  
`title` · sección: - · fuentes: -


## 1. Contenido  
`agenda` · sección: - · fuentes: -


## 2. lsmear pondera las restricciones antes de ramificar  
`block` · sección: Introducción · fuentes: `sec2`, `sec3`, `sec7`

- Optimización global continua con branch and bound intervalar.
- Las estrategias smear clásicas tratan todas las restricciones igual.
- Las restricciones activas deberían influir más que las inactivas.
- La propuesta usa multiplicadores de Lagrange óptimos para reponderar el sistema.

## 3. El solver intervalo divide y poda cajas  
`columns` · sección: Introducción · fuentes: `sec5`, `alg1`

- Selección de caja, bisección, filtrado y acotación superior.
- La terminación depende de la brecha entre \texttt{UB} y \texttt{LB}.
- Los contractors eliminan valores inconsistentes sin perder soluciones.
- La estrategia de variables afecta directamente el tamaño del árbol.

## 4. Las heurísticas smear usan solo impacto local  
`columns` · sección: Trabajo relacionado · fuentes: `sec3`, `sec7`

- \texttt{round-robin} ignora la estructura del sistema.
- \texttt{largest-first} solo mira el ancho del intervalo.
- \texttt{smearsum}, \texttt{smearmax} y \texttt{smearsumrel} usan derivadas parciales.
- Ninguna distingue restricciones activas de inactivas.

## 5. \texttt{ViolationTransfer} es más rico, pero intrusivo  
`block` · sección: Trabajo relacionado · fuentes: `sec3`, `sec7`, `sec8`, `sec13`

- Usa la función de Lagrange de una relajación convexa.
- Selecciona variables por el ancho de imagen del Lagrangiano.
- Necesita reformular el problema y modificar el contractor.
- lsmear busca una alternativa más simple y general.

## 6. lsmear repondera el smear con duales óptimos  
`equation` · sección: Propuesta · fuentes: `sec7`, `alg2`

- Primero lineariza el problema alrededor del punto medio.
- Luego resuelve el programa lineal asociado.
- Después calcula el impacto de cada variable en un Lagrangiano ponderado.
- La variable con mayor impacto se ramifica.

## 7. La linearización de Taylor define la primera fase  
`equation` · sección: Propuesta · fuentes: `sec7`, `alg2`, `alg3`

- Se aproxima cada restricción con el término de primer orden.
- \mathbf{J} contiene sobreestimaciones intervalares de derivadas parciales.
- Se usa el punto medio de \mathbf{J}, no la derivada en el centro.
- Si el LP falla o es no acotado, cae a \texttt{smearsum}.

## 8. El impacto se mide con el Lagrangiano ponderado  
`equation` · sección: Propuesta · fuentes: `sec6`, `sec7`, `alg3`

- \mathcal{L} combina objetivo y restricciones con \lambda^\ast.
- Para cada variable, D_i agrega derivadas ponderadas por duales.
- El criterio final es |D_i| multiplicado por el ancho del intervalo.
- La mejor variable maximiza ese impacto.

## 9. El algoritmo lsmear es de dos fases  
`algorithm` · sección: Propuesta · fuentes: `alg2`

- Fase 1: linearización y solución del LP.
- Fase 2: cálculo de impactos y elección de la variable.
- Reutiliza el mismo \mathbf{J} para todos los candidatos.
- Devuelve \texttt{smearsum} si no hay solución dual válida.

## 10. Las variantes exploran tres compromisos  
`table` · sección: Propuesta · fuentes: `sec8`

- \texttt{lsmear-MG}: gradiente en el punto medio.
- \texttt{lsmear-LI}: impacto sobre el problema linealizado.
- \texttt{lsmear-LR}: relajación lineal del contractor.
- \texttt{lsmear-LRI}: impacto sobre la relajación lineal.

## 11. Los experimentos usan IbexOpt y COCONUT  
`columns` · sección: Experimentos · fuentes: `sec9`

- Implementación en \texttt{IbexOpt} con \texttt{HC4}, \texttt{ACID(HC4)} y relajación lineal.
- Comparación en 76 instancias de \texttt{COCONUT}.
- Cada estrategia se ejecuta cinco veces; se reporta el promedio central.
- \epsilon = 10^{-6}; hardware: Xeon 2.20 GHz y 8 GB RAM.

## 12. lsmear supera claramente a las heurísticas clásicas  
`table` · sección: Experimentos · fuentes: `tab2`, `sec10`, `sec11`

- Mejor perfil de rendimiento que \texttt{rr}, \texttt{lf}, \texttt{smax}, \texttt{ssum} y \texttt{ssr}.
- Gana en tiempo total frente a la mejor clásica.
- Reduce mucho el número de nodos en varios casos.
- La sobrecarga del LP es pequeña frente al filtrado.

## 13. \texttt{lsmear-MG} es la mejor variante práctica  
`table` · sección: Experimentos · fuentes: `tab2`, `tab3`, `sec11`

- Empata o mejora a \texttt{lsmear} y \texttt{lsmear-LR}.
- \texttt{lsmear-LI} y \texttt{lsmear-LRI} son las peores variantes.
- Detectar restricciones activas sin ponderarlas no basta.
- La implementación simple favorece a \texttt{lsmear-MG}.

## 14. El método es más simple que \texttt{ViolationTransfer}  
`block` · sección: Conclusiones · fuentes: `sec13`

- No requiere modificar el contractor convexo del solver.
- Mantiene la estrategia de búsqueda estándar.
- Usa pesos duales para distinguir restricciones relevantes.
- Futuro: incorporar errores de violación sin perder simplicidad.
