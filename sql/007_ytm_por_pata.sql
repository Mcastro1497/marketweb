-- ============================================================================
-- 007_ytm_por_pata.sql
--
-- TIR de cada pata en su convención NATIVA.
--
-- EL PROBLEMA
-- valuations.ytm es siempre nominal en pesos, porque es la única unidad que hace
-- comparables las dos patas y permite decidir cuál paga. Pero para MIRAR una
-- pata, la nominal no sirve: nadie quotea una pata dólar-linked en pesos.
--
-- Las terminales lo resuelven mostrando el mismo bono dos veces, una por pata,
-- cada una en su unidad. 1816, al 18-08-2026:
--
--   TMVE8 @TAMAR   35,60% TEA        TXMD8 @TAMAR   38,16% TEA
--   TMVE8 @USD-L    6,26% TEA        TXMD8 @CER      6,29% TEA
--
-- Un 6,26% en dólares y un 35,60% en pesos son el mismo bono y el mismo precio;
-- lo que cambia es contra qué curva lo estás comparando.
--
-- LA SOLUCIÓN
-- Dos columnas más en valuations, análogas a lo que 006 hizo en prices:
--
--   ytm_nativa  La TIR en la unidad propia de la pata.
--   ytm_conv    En qué unidad está: nominal_ars | real_cer | usd.
--
-- El cálculo es siempre (base / precio)^(365/días) - 1; lo que cambia es la base:
--   nominal_ars -> vpv                       pago nominal en pesos
--   real_cer    -> vt * (1+tem)^meses_rest   pago deflactado por CER
--   usd         -> 100*fx * (1+spr)^m_rest   pago en dólares, al spot de hoy
--
-- En las patas FIJA y TAMAR la nativa coincide con la nominal: ya están en pesos.
--
-- Requiere 001. Idempotente.
-- ============================================================================

alter table valuations add column if not exists ytm_nativa numeric;
alter table valuations add column if not exists ytm_conv   text;

comment on column valuations.ytm is
  'TIR NOMINAL en pesos. Es la comparable entre patas y la que decide is_winner. '
  'Para mostrar una pata sola, usar ytm_nativa.';
comment on column valuations.ytm_nativa is
  'TIR en la convención propia de la pata (ver ytm_conv). Es la que se compara '
  'contra la curva de su clase de activo.';
comment on column valuations.ytm_conv is
  'Unidad de ytm_nativa: nominal_ars | real_cer | usd.';


-- v_duales pasa a exponer las dos TIR por pata, más el margen de la pata
-- ganadora sobre la alternativa, que es lo que mide cuánta opcionalidad queda.
--
-- Va DROP y no CREATE OR REPLACE: el replace sólo admite agregar columnas al
-- final de la lista, y acá se intercala `ventaja` antes de `patas`. Falla con
-- "cannot change name of view column". Nada depende de esta vista salvo el
-- front, que la lee por nombre de columna, así que dropearla es inocuo.
drop view if exists v_duales;

create view v_duales as
select v.symbol,
       v.scenario,
       (array_agg(v.leg order by v.vpv desc nulls last))[1] as ganadora,
       max(v.vpv)                                           as vpv_max,
       -- Distancia relativa entre las patas. Cerca de 0 = la opción está viva;
       -- muy grande = una pata domina y el dual es un mono-pata disfrazado.
       case when min(v.vpv) > 0 then max(v.vpv) / min(v.vpv) - 1 end as ventaja,
       jsonb_object_agg(v.leg, jsonb_build_object(
         'vpv',        v.vpv,
         'vt',         v.vt,
         'tem',        v.tem,
         'driver',     v.driver,
         'ytm',        v.ytm,
         'ytm_nativa', v.ytm_nativa,
         'ytm_conv',   v.ytm_conv,
         'breakeven',  v.breakeven,
         'is_winner',  v.is_winner,
         'params',     v.params
       ))                                                   as patas,
       max(v.ts)                                            as ts
from valuations v
where v.symbol in (select symbol from instrument_legs group by symbol having count(*) > 1)
group by v.symbol, v.scenario;


-- ── Verificación ────────────────────────────────────────────────────────────
--   python patas.py
--
-- Esperado (contrastable contra 1816):
--   TXMD8  TAMAR  ~38%  nominal_ars     CER   ~6,3%  real_cer
--   TMVE8  TAMAR  ~35%  nominal_ars     DLK   ~6,3%  usd
--   TTS26  TAMAR  ~17%  nominal_ars     FIJA  ~17%   nominal_ars (misma unidad)
