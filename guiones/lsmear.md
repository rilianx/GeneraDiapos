# Guion: lsmear: selección de variables para branch and bound con intervalos

Paper: `papers/lsmear.pdf`

Revisa el orden, el tipo de cada diapo y sobre todo **qué fragmentos usa** (`sources`). Para cambiar algo edita el `.json` de este PR; al fusionarlo se generan las diapositivas.

## Tablas del paper

- `tab1`: Table 1
- `tab2`: Table 2: Average CPU time and gain for all the strategies in a subset of instances. In bold, the best CPU times and the smallest number of boxes reported on each instance
- `tab3`: Table 3: Average CPU time for the strategies in a subset of instances. In bold, the best CPU time reported on each instance

## 0. lsmear mejora el branching con pesos duales  
`title` · fuentes: -

- Presentar el problema de selección de variables en solvers intervalares.
- Mostrar la idea central: ponderar restricciones con multiplicadores lagrangianos óptimos.
- Comparar lsmear con \\texttt{smearsum} y \\texttt{ViolationTransfer}.
- Resumir el impacto experimental sobre instancias de referencia.

## 1. El branching domina el costo del solver  
`block` · fuentes: `sec3`, `sec5`

- En NCOPs, el solver alterna bisección, filtrado y bound tightening.
- Elegir bien la variable reduce el árbol de búsqueda.
- Una mala regla de branching dispara el número de cajas.
- El objetivo es acercarse rápido a una \\epsilon-optimalidad verificable.

## 2. Las heurísticas clásicas ignoran la importancia de cada restricción  
`columns` · fuentes: `sec3`, `sec7`

- \\texttt{round-robin} evita sesgos, pero depende del orden inicial.
- \\texttt{largest-first} usa solo el ancho de dominios.
- Las heurísticas smear combinan derivadas e intervalos por restricción.
- \\alert{Limitación}: todas las restricciones pesan igual, incluso las inactivas.

## 3. \\texttt{ViolationTransfer} motivó el uso de duales  
`columns` · fuentes: `sec3`, `sec7`, `sec8`, `sec13`

- Usa la función Lagrangiana de una relajación del problema.
- Mide el efecto de cada variable sobre la anchura de la Lagrangiana.
- Requiere reformular el problema y modificar el contratador.
- Propone una referencia útil, pero es más intrusiva que lsmear.

## 4. lsmear pesa las restricciones antes del smear  
`block` · fuentes: `sec7`, `sec6`, `alg2`

- Aproxima cada restricción con una linealización de Taylor en el centro de la caja.
- Resuelve un programa lineal para obtener \\lambda^\*.
- Reemplaza los multiplicadores de la Lagrangiana por esos valores duales.
- Selecciona la variable con mayor impacto ponderado en la Lagrangiana.

## 5. La función de selección tiene dos fases  
`algorithm` · fuentes: `alg2`, `alg3`, `sec7`

- Fase 1: linealizar el sistema y resolver el programa lineal.
- Fase 2: calcular, para cada variable, el impacto \\(|D_i|\\mathrm{wid}(x_i)\\).
- Si el LP falla o es no acotado, caer a \\texttt{smearsum}.
- \\textbf{Ventaja}: la linealización es independiente del contratador convexo.

## 6. El solvers experimental usa \\texttt{IbexOpt}  
`table` · fuentes: `sec9`

- Implementación en \\texttt{IbexOpt} con \\texttt{HC4}, \\texttt{ACID(HC4)} y relajación lineal.
- Búsqueda por \\texttt{FeasibleDiving}; comparación adicional con \\texttt{minLB}.
- Precisión fijada en 10^{-6}.
- 76 instancias de COCONUT; cinco corridas por instancia; se promedian tres.

## 7. lsmear domina a las estrategias clásicas  
`columns` · fuentes: `sec10`

- Comparar \\texttt{lsmear} con \\texttt{rr}, \\texttt{lf}, \\texttt{smax}, \\texttt{ssum} y \\texttt{ssr}.
- En las figuras, lsmear concentra más instancias cerca del óptimo temporal.
- La ganancia media relativa frente a \\texttt{ssum} es 1.30.
- \\alert{Resultado}: lsmear resuelve casi 90\% de las instancias en menos del doble del mejor tiempo.

## 8. En la tabla, lsmear reduce tiempo y nodos  
`table` · fuentes: `tab2`

- hs087: 0.6 s con 65 cajas; empate con \\texttt{lsmear-MG}.
- bearing: 15 s y 1713 cajas; mejor tiempo entre las tres variantes.
- harker: 1071 s y 50,555 cajas; mejora clara sobre \\texttt{lsmear-LR}.
- ex8\_4\_5: \\texttt{lsmear-LR} gana en tiempo, pero no en robustez.

## 9. \\texttt{lsmear-MG} es la mejor variante práctica  
`block` · fuentes: `sec8`, `sec11`

- \\texttt{lsmear-MG} usa el gradiente en el punto medio para linealizar.
- \\texttt{lsmear-LI} y \\texttt{lsmear-LRI} empeoran al ponderar una relajación lineal.
- \\texttt{ssum-active} apenas mejora sobre \\texttt{smearsum}; detectar actividad no basta.
- \\textbf{Preferencia}: \\texttt{lsmear-MG} equilibra rendimiento y simplicidad.

## 10. El esquema de búsqueda influye menos que el branching  
`table` · fuentes: `sec12`, `tab3`

- \\texttt{FeasibleDiving} supera a \\texttt{minLB} con cualquier estrategia de selección.
- \\texttt{lsmear-MG} vence a \\texttt{ssum} y \\texttt{ssr} en ambos esquemas.
- Con \\texttt{minLB}, lsmear-MG supera a \\texttt{ssum} con ganancia media 1.63.
- Con \\texttt{FeasibleDiving}, la ganancia media frente a \\texttt{ssum} sigue siendo 1.43.

## 11. lsmear es simple y deja trabajo abierto  
`block` · fuentes: `sec13`

- La idea clave es ponderar restricciones con información dual barata.
- La mejora es grande sin modificar fuertemente el solver.
- Futuro: incorporar errores de violación como en \\texttt{ViolationTransfer}.
- También interesa decidir cuándo una variable lineal no merece bisecarse.
