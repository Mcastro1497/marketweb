# Scripts reemplazados

No se borran para no perder la referencia de cómo se calculaba cada cosa. Nada de
acá debe correrse: varios apuntan a tablas viejas y sobrescribirían datos buenos.

| script | por qué salió |
|---|---|
| `precios.py` | Reemplazado por `precios2.py`. Además lee la tabla `instruments` vieja, no `instruments_v2`, así que hoy suscribiría un universo equivocado. |
| `cer.py` | Su propio reemplazo lo declara desestimado: ver el docstring de `cerv2.py`. |
| `cer_test.py` | Script de prueba de un solo bono. |
| `explora2.py`, `explora_acciones.py` | Exploración del universo de ECO, de una sola vez. |
| `updatetamar.py` | Reemplazado por `series_sync.py`, que recorre el catálogo `series_defs` y sincroniza todas las series con el mismo código. Además escribía en `tamar_historico`, que desde sql/012 es una vista de sólo lectura. |
| `cleanflows.py` | Utilitario de limpieza puntual. Borra filas de `instrument_flows_v2`, `instruments_v2` y `prices`: NO correr sin leerlo antes. |

`tamar.py` NO está acá. La pata TAMAR de `patas.py` lo reproduce y está verificado
contra sus números, pero `tamar.py` sigue siendo el dueño de las columnas TAMAR de
`prices` (`tem_ponderada`, `tamar_obs`, etc.) que consume el front. Se retira
cuando esas columnas migren a `valuations`.
