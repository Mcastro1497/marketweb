-- ============================================================================
-- 011_fusionar_flujos.sql
--
-- Mete las columnas proyectadas dentro de instrument_flows y elimina la segunda
-- tabla. Una sola fuente de verdad para los flujos.
--
-- POR QUÉ
-- instrument_flows_proyectados tenía las MISMAS 12 columnas que instrument_flows
-- más 3 (interes/amortizacion/total _proyectado). De sus 1.809 filas, 1.703
-- estaban duplicadas idénticas: comparé `total` en todas y no difiere ni en 1e-9.
--
-- Y ya se habían desincronizado: 106 filas existían sólo en la tabla de
-- proyectados. Mientras haya dos copias eso vuelve a pasar; con una sola es
-- imposible por construcción.
--
-- Además desaparece una rutina entera de cerv2.py, que mantenía la copia con un
-- delete+insert por símbolo. Ahora sólo hace update de 3 columnas sobre la fila
-- que ya existe.
--
-- Los ~75 símbolos ilíquidos quedan con las 3 columnas en NULL, que es
-- exactamente lo que significan: no se proyectan. El front, en vez de elegir
-- tabla, filtra `total_proyectado is not null`.
--
-- CLAVE DE MATCHEO: (symbol, fecha_pago). Verificado único en las dos tablas
-- (2.088 y 1.809 claves para 2.088 y 1.809 filas) y fecha_pago es `date` puro,
-- sin componente horario, idéntica en las 1.703 filas comunes. Los `id` NO
-- sirven: se generaron por separado y no se pisan en ninguna fila.
--
-- Idempotente.
-- ============================================================================

-- ── 1. Columnas proyectadas en la tabla base ────────────────────────────────
alter table instrument_flows add column if not exists interes_proyectado      numeric;
alter table instrument_flows add column if not exists amortizacion_proyectado numeric;
alter table instrument_flows add column if not exists total_proyectado        numeric;

comment on column instrument_flows.total_proyectado is
  'Flujo proyectado en pesos (base × ratio CER). NULL = no se proyecta este '
  'símbolo: es lo que antes significaba "no está en instrument_flows_proyectados".';


-- ── 2. Traer las proyecciones de las filas que ya existen ───────────────────
do $$
declare n_upd int; n_ins int;
begin
  if not exists (select 1 from information_schema.tables
                  where table_schema='public' and table_name='instrument_flows_proyectados'
                    and table_type='BASE TABLE') then
    raise notice 'instrument_flows_proyectados ya no existe: nada que fusionar';
    return;
  end if;

  update instrument_flows f
     set interes_proyectado      = p.interes_proyectado,
         amortizacion_proyectado = p.amortizacion_proyectado,
         total_proyectado        = p.total_proyectado
    from instrument_flows_proyectados p
   where f.symbol = p.symbol
     and f.fecha_pago = p.fecha_pago;
  get diagnostics n_upd = row_count;

  -- Las 106 filas que sólo existían en la tabla de proyectados. Se traen enteras:
  -- son flujos reales que la tabla base no tenía, no un artefacto de la copia.
  insert into instrument_flows (
    symbol, fecha_pago, interes, amortizacion, total, moneda_pago, dias, cupon,
    valor_residual, tipo,
    interes_proyectado, amortizacion_proyectado, total_proyectado
  )
  select p.symbol, p.fecha_pago, p.interes, p.amortizacion, p.total, p.moneda_pago,
         p.dias, p.cupon, p.valor_residual, p.tipo,
         p.interes_proyectado, p.amortizacion_proyectado, p.total_proyectado
    from instrument_flows_proyectados p
   where not exists (select 1 from instrument_flows f
                      where f.symbol = p.symbol and f.fecha_pago = p.fecha_pago);
  get diagnostics n_ins = row_count;

  raise notice 'proyecciones copiadas: % filas | filas nuevas insertadas: %', n_upd, n_ins;
end $$;


-- ── 3. Retirar la tabla vieja ───────────────────────────────────────────────
-- Rename y no drop: si la fusión de arriba tuviera un error, se ve recién cuando
-- alguien mire un número raro. Mientras esté renombrada, la vuelta atrás existe.
-- Cuando confirmes que el front y cerv2 andan:
--     drop table zz_dropped_instrument_flows_proyectados;
do $$
begin
  if exists (select 1 from information_schema.tables
              where table_schema='public' and table_name='instrument_flows_proyectados'
                and table_type='BASE TABLE')
     and not exists (select 1 from information_schema.tables
                      where table_schema='public'
                        and table_name='zz_dropped_instrument_flows_proyectados') then
    alter table instrument_flows_proyectados rename to zz_dropped_instrument_flows_proyectados;
  end if;
end $$;


-- ── Verificación ────────────────────────────────────────────────────────────
-- Esperado: 2.194 filas (2.088 + 106), de las cuales 1.809 con proyección.
--
--   select count(*) as total,
--          count(total_proyectado) as con_proyeccion,
--          count(*) - count(total_proyectado) as sin_proyeccion
--     from instrument_flows;
--
-- Y que no se haya perdido ninguna proyección respecto de la tabla vieja:
--
--   select count(*) from zz_dropped_instrument_flows_proyectados p
--    where not exists (
--      select 1 from instrument_flows f
--       where f.symbol = p.symbol and f.fecha_pago = p.fecha_pago
--         and f.total_proyectado is not distinct from p.total_proyectado);
--   -- tiene que dar 0
