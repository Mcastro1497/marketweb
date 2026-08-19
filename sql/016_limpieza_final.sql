-- ============================================================================
-- 016_limpieza_final.sql
--
-- Retira los respaldos y las vistas que ya no tienen consumidor.
--
-- QUÉ SE VERIFICÓ ANTES
-- Cada respaldo se comparó fila por fila contra `series`: 60 filas de CER y 449
-- de TAMAR, todas presentes y con el mismo valor hasta 1e-9. Sin faltantes y sin
-- diferencias.
--
-- Y esa comparación encontró algo: el respaldo de CER tenía 27 filas que `series`
-- NO tenía, todas de fechas FUTURAS (hasta el 15-09). El BCRA publica el CER por
-- adelantado y series_sync.py cortaba en "hoy", así que las perdía. No era
-- cosmético: cerv2 mira el CER de 10 días hábiles antes del vencimiento para
-- decidir si un bono ya quedó determinístico, y para los que vencen pronto ese
-- tramo hace falta. Se corrigió el sync (DIAS_ADELANTE) y recién después se
-- confirmó la contención. Si se hubiera borrado antes de mirar, el dato se perdía
-- sin que nadie lo notara.
--
-- QUÉ NO SE TOCA
--   cer_historico   La sigue leyendo cer-historico-table.tsx del front. Deja de
--                   ser un parche de compatibilidad y pasa a ser lo que es: una
--                   vista de lectura con un nombre que significa algo.
--   tickers_legacy  Es la única fuente de identidad de las ~181 filas de prices
--                   sin instrumento. No se borra hasta resolver si prices se
--                   indexa por ticker o por bono.
--   *_test          Destinos de los uploaders del panel de admin.
--
-- Las vistas instruments_v2, instrument_flows_v2 e instrument_flows_v3 que iba a
-- retirar el 010 no existen: nunca llegaron a crearse. El 010 queda sin efecto.
--
-- Idempotente.
-- ============================================================================

-- ── Respaldos de la unificación de series (012) ─────────────────────────────
drop table if exists zz_dropped_cer_historico;
drop table if exists zz_dropped_tamar_historico;

-- ── Vista sin consumidor ────────────────────────────────────────────────────
-- tamar_historico no la lee nadie: patas.py y tamar.py leen `series` directo.
drop view if exists tamar_historico;

-- ── El nombre que sí se conserva ────────────────────────────────────────────
comment on view cer_historico is
  'Vista de lectura sobre series (serie=cer). NO es un parche temporal: es la '
  'interfaz que consume cer-historico-table.tsx. Las escrituras están revocadas '
  '(ver 013); para cargar datos, series_sync.py.';


-- ── Verificación ────────────────────────────────────────────────────────────
--   select table_name, table_type from information_schema.tables
--    where table_schema='public' order by table_type, table_name;
--
-- No debería quedar ningún zz_dropped_*, ni tamar_historico, ni ningún objeto
-- con sufijo de versión.
--
--   select count(*) from cer_historico;   -- sigue respondiendo, para el front
