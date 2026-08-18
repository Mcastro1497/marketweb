-- ============================================================================
-- 005_alta_tmve8.sql
--
-- Alta del BONO DEL TESORO NACIONAL EN MONEDA DUAL TAMAR / DÓLAR LINKED
-- con vencimiento 31 de enero de 2028 (TMVE8).
--
-- FUENTE: Resolución Conjunta 46/2026 (SF y SH), art. 3°, BO 31/07/2026
--         https://www.boletinoficial.gob.ar/detalleAviso/primera/345289/20260731
--
-- Verificado leyendo el prospecto:
--   · Fecha de emisión: 31 de julio de 2026. Vencimiento: 31 de enero de 2028.
--   · Moneda de DENOMINACIÓN: dólares estadounidenses. Pago en pesos.
--   · Amortización íntegra al vencimiento. VNO mínimo USD 1.
--   · Tipo de cambio inicial:   A3500 del día hábil previo a la licitación
--                               (licitación 29-07-2026 -> T-1 = 28-07-2026).
--   · Tipo de cambio aplicable: A3500 del TERCER día hábil previo al pago.
--   · Al vencimiento paga el MÁXIMO entre:
--       i)  VN en USD convertido a pesos al Tipo de Cambio APLICABLE.
--           Ajuste de capital puro: SIN spread y SIN devengamiento.
--       ii) VN en USD convertido a pesos al Tipo de Cambio INICIAL, más
--           intereses a TAMAR TEM capitalizable mensual:
--             VPV       = VNO * (1 + TAMAR TEM)^((DÍAS/360)*12)
--             TAMAR TEM = [(1 + TAMAR/(365/32))^(365/32)]^(1/12) - 1
--           La fórmula NO lleva margen: es TAMAR pura.
--
-- CORRECCIÓN respecto de lo que se había asumido antes: no hay ningún
-- "A3500 + 6,64%". Ese 6,64% salió de una nota periodística y es la TIREA de
-- corte de la licitación, no un spread contractual. Las dos patas van sin
-- margen. Por eso ambas entran con params vacíos salvo el fx_base.
--
-- UNIDADES: al estar denominado en dólares, "100 de VN" son USD 100 y el vpv
-- sale en pesos por VNO USD 100 (~150.000, no ~150). Es la misma base en la que
-- precios2.py guarda price_ars para los DLK (TZV27 = 148.800), así que la TIR y
-- la paridad quedan bien. No compares la columna vpv entre un TMVE8 y un TTS26.
--
-- PENDIENTE desde BYMA: isin, vr_vigente, vn_vigente.
--
-- Requiere 001 y 003. Idempotente.
-- ============================================================================

insert into instruments_v2 (
  symbol, instrument_type, segment, is_active,
  emisor, legislacion, jurisdiccion_pago, tipo_activo,
  emision, vencimiento,
  moneda_denom, moneda_pago,
  tipo_cupon, tasa_int, margen_ref, tasa_ref, referencias,
  convencion_int, periodicidad_int,
  lamina_min, operacion_min, valor_residual, callable,
  denominacion
) values
  ('TMVE8', 'DUAL', '24hs', true,
   'Argentina', 'Argentina', 'Argentina', 'Títulos Públicos',
   date '2026-07-31', date '2028-01-31',
   'USD', 'ARS',
   'Variable', null, 0, 'Tamar', 'Dual',
   '30/360', 'Nula',
   1, 1, 1, false,
   'GOB USD-L ARG Dual Tamar/DLK (TMVE8)')
on conflict (symbol) do nothing;


-- ── Patas ───────────────────────────────────────────────────────────────────
-- fx_base = A3500 del 28-07-2026 = 1499,8387 (traído de la API del BCRA,
-- idVariable 5 "Tipo de cambio mayorista de referencia"). Es el tipo de cambio
-- inicial que fija el prospecto y queda congelado hasta el vencimiento: la pata
-- TAMAR devenga sobre el VN convertido a ESE tipo de cambio, no al spot.
insert into instrument_legs (symbol, leg, params) values
  ('TMVE8', 'DLK',   '{"spread": 0}'::jsonb),
  ('TMVE8', 'TAMAR', '{"margen": 0, "fx_base": 1499.8387}'::jsonb)
on conflict (symbol, leg) do nothing;


-- ── Verificación ────────────────────────────────────────────────────────────
--   python patas.py --dry-run --symbols TMVE8
--
-- El breakeven de la pata DLK sale en $/USD: es el A3500 al vencimiento que
-- iguala las dos patas, y se compara directo contra un futuro de ROFEX a
-- ene-2028. Si el breakeven está por debajo del futuro, el mercado está
-- diciendo que paga la pata dólar.
--
-- La proyección de FX sale de la senda de devaluación del REM aplicada sobre el
-- spot (prices.UST, que mantiene dlk.py desde MAE). El REM de tipo de cambio
-- llega hasta dic-2027, así que enero-2028 queda extrapolado — sale contado en
-- params.meses_extrapolados. Cuando haya curva de futuros conviene reemplazar
-- esa proyección por ROFEX, que es referencia de mercado y no de encuesta.
