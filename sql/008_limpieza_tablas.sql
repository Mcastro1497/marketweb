-- ============================================================================
-- 008_limpieza_tablas.sql
--
-- Retira instrument_flows, la única tabla del esquema sin ningún consumidor.
--
-- RELEVAMIENTO (grep sobre marketweb + Market-Datita, excluyendo deprecated/):
--
--   instrument_flows        1.581 filas   CERO consumidores   -> se retira acá
--   instrument_flows_v2     2.088 filas   front (4) + cerv2 + dlk
--   instrument_flows_v3     1.809 filas   front (2) + cerv2
--   instrument_flows_test   5.472 filas   admin/page.tsx:105  -> NO TOCAR
--   instruments_test            0 filas   admin/page.tsx:119  -> NO TOCAR
--
-- Las dos tablas *_test parecen muertas y no lo están: son los destinos de los
-- uploaders de prueba del panel de admin. instruments_test está vacía porque
-- todavía no se subió nada, no porque esté en desuso.
--
-- v2 y v3 NO son dos generaciones de lo mismo: v2 tiene todos los flujos y v3
-- sólo los soberanos ARS más las ONs que operan, con las columnas proyectadas
-- por CER. La diferencia de 75 símbolos es un filtro deliberado, no un faltante.
-- (El docstring de cerv2.py dice "réplica de v2" y conviene corregirlo, porque
-- invita a leerlo como un bug.)
--
-- SE RENOMBRA EN VEZ DE DROPEAR. Son 1.581 filas que no generé yo y de las que
-- no puedo descartar que sean un archivo histórico. El rename las saca del
-- esquema de trabajo y es reversible con una línea. Cuando confirmes que no las
-- necesitás:
--
--     drop table zz_dropped_instrument_flows;
--
-- y para volver atrás:
--
--     alter table zz_dropped_instrument_flows rename to instrument_flows;
--
-- Idempotente.
-- ============================================================================

do $$
begin
  if exists (select 1 from information_schema.tables
              where table_schema = 'public' and table_name = 'instrument_flows')
     and not exists (select 1 from information_schema.tables
                      where table_schema = 'public' and table_name = 'zz_dropped_instrument_flows')
  then
    alter table public.instrument_flows rename to zz_dropped_instrument_flows;
    raise notice 'instrument_flows -> zz_dropped_instrument_flows';
  else
    raise notice 'nada que hacer: instrument_flows ya no existe o ya fue renombrada';
  end if;
end $$;

comment on table zz_dropped_instrument_flows is
  'RETIRADA el 2026-08-18 por 008_limpieza_tablas.sql: no tenía ningún consumidor '
  'en marketweb ni en Market-Datita. Se conserva por si fuera archivo histórico. '
  'Dropear cuando se confirme que no hace falta.';


-- ── Verificación ────────────────────────────────────────────────────────────
--   select table_name from information_schema.tables
--    where table_schema='public' order by 1;
--
-- Esperado: ya no aparece instrument_flows, sí zz_dropped_instrument_flows.
-- El front y los motores no deberían notar nada.
