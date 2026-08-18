-- ============================================================================
-- 002_alta_duales.sql
--
-- Alta de los BONOS DEL TESORO NACIONAL EN PESOS A TASA DUAL vivos: TTS26 y
-- TTD26. Primer dual real en la base.
--
-- FUENTE: Resolución Conjunta 4/2025 (SF y SH, 24/01/2025), Boletín Oficial
--         https://www.boletinoficial.gob.ar/pdf/aviso/primera/320147/20250422
--
-- Del prospecto, verificado contra el texto:
--   · Emisión 29-ene-2025 para los cuatro duales de la canasta de conversión.
--   · Amortización íntegra al vencimiento (bullet). Intereses al vencimiento.
--   · Paga el MÁXIMO entre:
--       i)  tasa fija TEM capitalizable mensual  -> TTS26 2,17% / TTD26 2,14%
--       ii) TAMAR TEM capitalizable mensual, SIN margen
--     con VPV = VNO * (1 + tasa)^((DÍAS/360)*12) y DÍAS en 30/360.
--   · La TAMAR es el promedio aritmético simple de las TNA publicadas por el
--     BCRA desde 10 días hábiles antes de la emisión hasta 10 días hábiles
--     antes del vencimiento.
--   · Denominación mínima VNO $1. Ley argentina. Pesos.
--
-- TTM26 (16-mar-2026) y TTJ26 (30-jun-2026) ya vencieron y no se dan de alta.
-- Si los querés para backtesting, misma estructura con is_active=false y
-- tasa_int 0.0225 y 0.0219 respectivamente.
--
-- PENDIENTE de completar desde BYMA (se dejan en NULL a propósito, no se
-- inventan): isin, vr_vigente, vn_vigente.
--
-- Requiere 001_patas_duales.sql. Idempotente.
-- ============================================================================


-- ── 1. Instrumentos ─────────────────────────────────────────────────────────
-- instrument_type='DUAL' es informativo: la categoría real la deriva
-- v_instrument_kind de las patas, no de esta columna.
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
  ('TTS26', 'DUAL', '24hs', true,
   'Argentina', 'Argentina', 'Argentina', 'Títulos Públicos',
   date '2025-01-29', date '2026-09-15',
   'ARS', 'ARS',
   'Variable', 0.0217, 0, 'Tamar', 'Dual',
   '30/360', 'Nula',
   1, 1, 1, false,
   'GOB ARS ARG Dual (TTS26)'),

  ('TTD26', 'DUAL', '24hs', true,
   'Argentina', 'Argentina', 'Argentina', 'Títulos Públicos',
   date '2025-01-29', date '2026-12-15',
   'ARS', 'ARS',
   'Variable', 0.0214, 0, 'Tamar', 'Dual',
   '30/360', 'Nula',
   1, 1, 1, false,
   'GOB ARS ARG Dual (TTD26)')
on conflict (symbol) do nothing;


-- ── 2. Patas ────────────────────────────────────────────────────────────────
-- Acá está todo lo que hace que el bono sea un dual. Nada de código.
insert into instrument_legs (symbol, leg, params) values
  ('TTS26', 'FIJA',  '{"tem": 0.0217}'::jsonb),
  ('TTS26', 'TAMAR', '{"margen": 0}'::jsonb),
  ('TTD26', 'FIJA',  '{"tem": 0.0214}'::jsonb),
  ('TTD26', 'TAMAR', '{"margen": 0}'::jsonb)
on conflict (symbol, leg) do nothing;


-- ── Verificación ────────────────────────────────────────────────────────────
--   select * from v_instrument_kind where n_patas > 1;
--     -> TTS26  2  DUAL:FIJA/TAMAR
--        TTD26  2  DUAL:FIJA/TAMAR
--
-- Después correr:  python patas.py --dry-run --symbols TTS26,TTD26
-- Esperado al 16-ago-2026, con TAMAR proyectada en ~23,1% TNA:
--
--   TTS26   FIJA  TEM 2.1700%  VPV 152.10
--           TAMAR TEM 2.7175%  VPV 168.83  BE -52.00%   ◄ paga
--   TTD26   FIJA  TEM 2.1400%  VPV 161.14
--           TAMAR TEM 2.6092%  VPV 178.67  BE  -3.47%   ◄ paga
--
-- El breakeven negativo dice que la pata fija es inalcanzable: con el 91% y el
-- 79% de la ventana de TAMAR ya observada, la TAMAR promedio del tramo que
-- falta tendría que ser negativa para que la fija gane. La opción está muerta
-- y los duales hoy son floaters TAMAR puros.


-- ── Notas operativas ────────────────────────────────────────────────────────
-- 1. precios2.py carga los símbolos desde instruments_v2 filtrando is_active,
--    así que con esta alta se suscribe solo y empieza a poblar prices. Hasta
--    que llegue el primer precio, patas.py calcula VPV/TEM/breakeven igual y
--    deja ytm/paridad en NULL.
--
-- 2. Estos bonos NO van a aparecer todavía en el dashboard de soberanos ARS:
--    esa página arma las filas desde instrument_flows_v3 y estos símbolos no
--    tienen flujos cargados. Es lo que queremos por ahora.
--
-- 3. Cuando cargues los flujos, ojo: categoriaArs() en soberanos-ars/page.tsx
--    manda a 'FIJA' todo lo que no sea CER/Tamar/A3500, así que con
--    referencias='Dual' los duales caerían en el tab FIJA. Se arregla cuando el
--    front pase a leer v_instrument_kind (paso 5 del plan).
