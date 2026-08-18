-- ============================================================================
-- 004_alta_duales_cer.sql
--
-- Alta de los BONOS DEL TESORO NACIONAL EN PESOS DUAL CER/TAMAR.
--
-- FUENTE: Resolución Conjunta 32/2026 (SF y SH), Boletín Oficial 11/06/2026
--         https://www.boletinoficial.gob.ar/detalleAviso/primera/343013/20260611
--         Artículos 3° (2028), 4° (2029) y 5° (2030).
--
-- Verificado leyendo el prospecto, para los tres:
--   · Fecha de emisión (T): 12 de junio de 2026. Amortización íntegra al vto.
--   · Denominación y pago en pesos. VNO mínimo $1. Ley argentina.
--   · Al vencimiento paga el MÁXIMO entre:
--       i)  capital ajustado por CER entre los 10 días hábiles anteriores a la
--           emisión y los 10 días hábiles anteriores al vencimiento.
--           SIN interés adicional: es ajuste de capital puro.
--       ii) TAMAR TEM capitalizable mensual MÁS UN MARGEN DEL 3%, con
--             VPV       = VNO * (1 + TAMAR TEM)^((DÍAS/360)*12)
--             TAMAR TEM = [(1 + (TAMAR + 3%)/(365/32))^(365/32)]^(1/12) - 1
--           El margen va DENTRO de la fórmula, sumado a la TNA antes de
--           convertir a TEM. La TAMAR es el promedio aritmético simple de las
--           TNA publicadas por el BCRA en la misma ventana de 10 días hábiles.
--
-- TXMJ8 y TXMJ9 salen de otras dos resoluciones, con la MISMA estructura y el
-- mismo margen del 3%, verificado leyendo cada prospecto:
--   TXMJ8  Res. Conj. 25/2026 art. 1° (BO 15/05/2026)  emisión 15-05-2026
--   TXMJ9  Res. Conj. 23/2026 art. 3° (BO 29/04/2026)  emisión 30-04-2026
--          (reabierto el 13-05-2026 por Res. Conj. 25/2026 art. 5°)
--
-- Contraste con el comunicado de resultados de la licitación del 13-05-2026,
-- útil para validar la pata CER: TXMJ8 cortó a TIREA sobre CER 4,00% con precio
-- $920 por VNO $1.000; TXMJ9 a 6,19% con precio $842.
--
-- PENDIENTE desde BYMA: isin, vr_vigente, vn_vigente (se dejan en NULL).
--
-- Requiere 001 y 003. Idempotente.
-- ============================================================================


-- ── 1. Instrumentos ─────────────────────────────────────────────────────────
insert into instruments_v2 (
  symbol, instrument_type, segment, is_active,
  emisor, legislacion, jurisdiccion_pago, tipo_activo,
  emision, vencimiento,
  moneda_denom, moneda_pago,
  tipo_cupon, tasa_int, margen_ref, tasa_ref, referencias, cer_emision,
  convencion_int, periodicidad_int,
  lamina_min, operacion_min, valor_residual, callable,
  denominacion
) values
  ('TXMD8', 'DUAL', '24hs', true,
   'Argentina', 'Argentina', 'Argentina', 'Títulos Públicos',
   date '2026-06-12', date '2028-12-15',
   'ARS', 'ARS', 'Variable', null, 0.03, 'Tamar', 'Dual', 779.88639312623,
   '30/360', 'Nula', 1, 1, 1, false,
   'GOB ARS ARG Dual CER/Tamar (TXMD8)'),

  ('TXMD9', 'DUAL', '24hs', true,
   'Argentina', 'Argentina', 'Argentina', 'Títulos Públicos',
   date '2026-06-12', date '2029-12-14',
   'ARS', 'ARS', 'Variable', null, 0.03, 'Tamar', 'Dual', 779.88639312623,
   '30/360', 'Nula', 1, 1, 1, false,
   'GOB ARS ARG Dual CER/Tamar (TXMD9)'),

  ('TXMJ0', 'DUAL', '24hs', true,
   'Argentina', 'Argentina', 'Argentina', 'Títulos Públicos',
   date '2026-06-12', date '2030-06-28',
   'ARS', 'ARS', 'Variable', null, 0.03, 'Tamar', 'Dual', 779.88639312623,
   '30/360', 'Nula', 1, 1, 1, false,
   'GOB ARS ARG Dual CER/Tamar (TXMJ0)'),

  -- Res. Conjunta 25/2026 art. 1° (BO 15/05/2026), licitación del 13-05-2026.
  ('TXMJ8', 'DUAL', '24hs', true,
   'Argentina', 'Argentina', 'Argentina', 'Títulos Públicos',
   date '2026-05-15', date '2028-06-30',
   'ARS', 'ARS', 'Variable', null, 0.03, 'Tamar', 'Dual', 758.11795391868,
   '30/360', 'Nula', 1, 1, 1, false,
   'GOB ARS ARG Dual CER/Tamar (TXMJ8)'),

  -- Res. Conjunta 23/2026 art. 3° (BO 29/04/2026), licitación del 28-04-2026.
  -- Reabierto en la licitación del 13-05-2026 (Res. Conj. 25/2026 art. 5°).
  ('TXMJ9', 'DUAL', '24hs', true,
   'Argentina', 'Argentina', 'Argentina', 'Títulos Públicos',
   date '2026-04-30', date '2029-06-29',
   'ARS', 'ARS', 'Variable', null, 0.03, 'Tamar', 'Dual', 746.38092265237,
   '30/360', 'Nula', 1, 1, 1, false,
   'GOB ARS ARG Dual CER/Tamar (TXMJ9)')
on conflict (symbol) do nothing;

-- cer_emision = CER del 29-05-2026 (10 días hábiles antes de la emisión del
-- 12-06-2026), traído de la API del BCRA (idVariable 30). Es imprescindible
-- cargarlo: cer_historico guarda sólo los últimos 60 días (CER_SYNC_LIMIT), así
-- que la serie local nunca va a tener el coeficiente base de un bono viejo.


-- ── 2. Patas ────────────────────────────────────────────────────────────────
-- La pata CER va con params vacío: el motor toma el CER base de la serie en
-- 10 días hábiles antes de la emisión (29-05-2026), que ya está publicado.
-- Si se quisiera fijar el coeficiente exacto del prospecto, se carga como
-- {"cer_base": <valor>} y pisa al calculado.
insert into instrument_legs (symbol, leg, params) values
  ('TXMD8', 'CER',   '{}'::jsonb),
  ('TXMD8', 'TAMAR', '{"margen": 0.03}'::jsonb),
  ('TXMD9', 'CER',   '{}'::jsonb),
  ('TXMD9', 'TAMAR', '{"margen": 0.03}'::jsonb),
  ('TXMJ0', 'CER',   '{}'::jsonb),
  ('TXMJ0', 'TAMAR', '{"margen": 0.03}'::jsonb),
  ('TXMJ8', 'CER',   '{}'::jsonb),
  ('TXMJ8', 'TAMAR', '{"margen": 0.03}'::jsonb),
  ('TXMJ9', 'CER',   '{}'::jsonb),
  ('TXMJ9', 'TAMAR', '{"margen": 0.03}'::jsonb)
on conflict (symbol, leg) do nothing;


-- ── Verificación ────────────────────────────────────────────────────────────
--   select * from v_instrument_kind where n_patas > 1 order by symbol;
--     -> TTD26/TTS26                        DUAL:FIJA/TAMAR
--        TXMD8/TXMD9/TXMJ0/TXMJ8/TXMJ9      DUAL:CER/TAMAR
--
--   python patas.py --dry-run --symbols TXMJ8,TXMD8,TXMJ9,TXMD9,TXMJ0
--
-- Esperado al 17-08-2026 (REM jul-2026 mediana, TAMAR últimos-5 en 23,10%):
--   bono    vto          VPV CER   VPV TAMAR   paga    BE infl mens / anual
--   TXMJ8   2028-06-30    149.87      172.81   TAMAR      2.131%   28.80%
--   TXMD8   2028-12-15    154.79      190.81   TAMAR      2.174%   29.44%
--   TXMJ9   2029-06-29    173.68      225.89   TAMAR      2.131%   28.80%
--   TXMD9   2029-12-14    176.60      246.81   TAMAR      2.174%   29.44%
--   TXMJ0   2030-06-28    189.63      283.63   TAMAR      2.174%   29.44%
--
-- A diferencia de los TT*26, acá la opción está VIVA: vencen 2028-2030 y el
-- breakeven de inflación cae en un rango alcanzable, así que el número dice
-- algo. Ojo con leerlo sin el supuesto de TAMAR al lado: el breakeven de
-- inflación se despeja fijando TAMAR en la convención de últimos-5, y se mueve
-- si esa convención cambia.


-- ── Nota sobre prices.ytm ───────────────────────────────────────────────────
-- cerv2.py escribe en prices.ytm la TIR REAL (sobre CER) de los Boncer/Lecer.
-- patas.py escribe TIR NOMINAL en pesos, que es lo único comparable contra una
-- pata TAMAR. Son números distintos en la misma columna, según quién escriba.
--
-- Por eso patas.py sólo toca el headline de bonos multi-pata: los CER de una
-- pata siguen siendo de cerv2 y su TIR real queda intacta. NO correr patas.py
-- con --headline-all hasta resolver esa ambigüedad de semántica.
-- La TIR real de cada pata CER queda igual disponible en valuations.params.ytm_real.
