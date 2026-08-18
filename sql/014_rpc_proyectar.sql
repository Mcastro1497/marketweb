-- ============================================================================
-- 014_rpc_proyectar.sql
--
-- Proyección CER de los flujos, en una sola sentencia.
--
-- EL PROBLEMA
-- project_once() de cerv2.py hacía un UPDATE por fila: ~1.800 llamadas HTTP por
-- ciclo, una por flujo. Intenté batchearlo con upsert y falla, porque el upsert
-- de PostgREST es INSERT ... ON CONFLICT: cuando manda sólo {id, proyectados} y
-- no encuentra el conflicto, intenta INSERTAR una fila con symbol NULL y choca
-- contra el NOT NULL.
--
-- Pero el batching por HTTP era la solución equivocada de entrada. La operación
-- es "multiplicá tres columnas por un coeficiente que depende del símbolo": eso
-- es una sentencia de SQL, no 1.800 requests. Un solo UPDATE hace todo.
--
-- El coeficiente llega como jsonb {símbolo: ratio}, que lo calcula cerv2 con la
-- lógica de CER t-10 y de bono fijo.
--
-- NULL se propaga solo: si un flujo tiene interes NULL, interes_proyectado queda
-- NULL. Es el mismo comportamiento que tenía el bucle en Python.
--
-- Idempotente.
-- ============================================================================

create or replace function proyectar_flujos(ratios jsonb)
returns integer
language plpgsql
as $$
declare
  n integer;
begin
  update instrument_flows f
     set interes_proyectado      = f.interes      * (ratios ->> f.symbol)::numeric,
         amortizacion_proyectado = f.amortizacion * (ratios ->> f.symbol)::numeric,
         total_proyectado        = f.total        * (ratios ->> f.symbol)::numeric
   where ratios ? f.symbol;
  get diagnostics n = row_count;
  return n;
end;
$$;

comment on function proyectar_flujos(jsonb) is
  'Aplica el ratio CER a las columnas proyectadas de instrument_flows. Recibe '
  '{símbolo: ratio}. Reemplaza el bucle de ~1.800 UPDATE por fila de cerv2.py.';


-- ── Verificación ────────────────────────────────────────────────────────────
--   select proyectar_flujos('{"TZXD6": 1.0}'::jsonb);
--   -- devuelve la cantidad de flujos de TZXD6 (los deja igual: ratio 1)
--
-- Y después de correr cerv2.py --once:
--   select count(*) filter (where total_proyectado is not null) as con,
--          count(*) filter (where total_proyectado is null)     as sin
--     from instrument_flows;
--   -> con 1809, sin 385
