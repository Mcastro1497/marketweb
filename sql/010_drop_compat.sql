-- ============================================================================
-- 010_drop_compat.sql
--
-- Retira las vistas de compatibilidad que dejó 009.
--
-- CORRER SÓLO cuando hayas verificado que todo anda con los nombres nuevos:
--   · las 6 páginas del front (ons, cartera, duales, soberanos, soberanos-ars, dlk)
--   · un ciclo completo de los motores (cerv2, dlk, tir, tamar, patas, precios2)
--   · lo que tengas fuera de estos dos repos y yo no puedo ver: queries guardadas
--     en el SQL editor, n8n, Metabase, notebooks, planillas contra la API
--
-- Ese último punto es justamente el motivo de que las vistas existan. Mientras
-- estén, algo que use un nombre viejo sigue funcionando en silencio; después de
-- esto, falla. Y si no estás seguro, no hay ningún apuro: son vistas, no copias,
-- así que no ocupan espacio ni se desincronizan.
--
-- Idempotente.
-- ============================================================================

drop view if exists instrument_flows_v2;
drop view if exists instrument_flows_v3;
drop view if exists instruments_v2;

-- Verificación: no debería quedar ningún objeto con sufijo de versión.
--   select table_name, table_type from information_schema.tables
--    where table_schema = 'public' and table_name ~ '_v[0-9]+$'
--    order by 1;
