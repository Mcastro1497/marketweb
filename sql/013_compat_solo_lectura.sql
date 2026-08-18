-- ============================================================================
-- 013_compat_solo_lectura.sql
--
-- Bloquea las escrituras sobre las vistas de compatibilidad.
--
-- QUÉ PASÓ
-- En 012 escribí que cer_historico y tamar_historico eran "de sólo lectura
-- porque renombran columnas y filtran por serie". Es falso: en Postgres una
-- vista sobre UNA sola tabla, con WHERE y con columnas renombradas, sigue siendo
-- auto-actualizable. Renombrar y filtrar no la vuelve read-only.
--
-- Consecuencia real: un `delete from cer_historico` se propagó a
-- `series where serie='cer'` y borró la serie entera, 1.101 filas. Sin error, sin
-- rastro: la vista simplemente pasó a devolver 0 filas y la TIR real de los
-- CER/TAMAR salió ~275 bps abajo. Se recuperó volviendo a bajarla del BCRA.
--
-- LA SOLUCIÓN
-- REVOKE de insert/update/delete. Se elige revoke y no una regla DO INSTEAD
-- NOTHING a propósito: la regla se tragaría la escritura en silencio, que es
-- exactamente el modo de falla que causó esto. El revoke tira error.
--
-- La única forma correcta de escribir estas series es contra `series`, con
-- series_sync.py.
--
-- Idempotente.
-- ============================================================================

revoke insert, update, delete, truncate on cer_historico   from public, anon, authenticated, service_role;
revoke insert, update, delete, truncate on tamar_historico from public, anon, authenticated, service_role;

-- Las de 009 corren el mismo riesgo: son vistas de una tabla, plenamente
-- escribibles. Ahí no molesta —los escritores legítimos pasaron a los nombres
-- nuevos— pero conviene que nadie escriba por la puerta vieja sin darse cuenta.
do $$
declare v text;
begin
  foreach v in array array['instruments_v2','instrument_flows_v2','instrument_flows_v3'] loop
    if exists (select 1 from information_schema.views
                where table_schema='public' and table_name=v) then
      execute format('revoke insert, update, delete, truncate on public.%I from public, anon, authenticated, service_role', v);
      raise notice 'escrituras bloqueadas en la vista %', v;
    end if;
  end loop;
end $$;

comment on view cer_historico is
  'COMPAT de sólo lectura -> series (serie=cer). Escrituras revocadas: un delete '
  'sobre esta vista llegaba a series y borraba la serie. Escribir con series_sync.py.';
comment on view tamar_historico is
  'COMPAT de sólo lectura -> series (serie=tamar_tna). Escrituras revocadas. '
  'Escribir con series_sync.py.';


-- ── Verificación ────────────────────────────────────────────────────────────
--   select count(*) from cer_historico;          -- lee bien
--   delete from cer_historico where false;       -- tiene que dar permission denied
--
-- Y que la serie esté completa:
--   select serie, count(*), min(fecha), max(fecha) from series group by 1 order by 1;
--   -> a3500 727 | cer 1101 | ipc_mensual 36 | tamar_tna 449
