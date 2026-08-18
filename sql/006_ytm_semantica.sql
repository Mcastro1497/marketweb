-- ============================================================================
-- 006_ytm_semantica.sql
--
-- Desambigua prices.ytm.
--
-- EL PROBLEMA
-- Cinco escritores dejan números con TRES convenciones distintas en la misma
-- columna, sin nada que las distinga:
--
--   cerv2.py   ARS ref=CER      real (sobre CER)   TZXD6 = 3,91%
--   cerv2.py   ARS sin ref      nominal ARS        T30J7 = 27,91%
--   tamar.py   ref=Tamar        nominal ARS        TMF27 = 29,28%
--   dlk.py     ref=A3500        USD                TZV27 =  0,57%
--   tir.py     ON, HD           USD
--   patas.py   duales           nominal ARS
--
-- El mismo bono da 3,91% o 29,55% según quién lo calcule, y no hay forma de
-- saber cuál es cuál mirando la fila. Ordenar una tabla por ytm mezclando tipos
-- no significa nada, y el tab DUALES mezcla por definición.
--
-- LA SOLUCIÓN
-- Dos columnas nuevas, sin tocar el significado de `ytm`:
--
--   ytm_tipo  Declara en qué convención está `ytm`. La columna pasa a ser
--             auto-descriptiva y el front puede rotular y agrupar sin adivinar.
--             `ytm` sigue siendo la TIR en la convención de cotización del
--             instrumento, que es la que el mercado quotea y la que ya muestra
--             la pantalla. Nada de lo existente cambia de valor.
--
--   ytm_ars   TIR nominal en pesos, para TODO. Es la única comparable entre
--             clases de activo, y la que necesita un dual, donde no existe una
--             convención nativa: no podés quotear en términos reales algo que
--             quizá pague TAMAR.
--
-- Dueño de cada columna, para que no se repita el problema:
--   ytm + ytm_tipo -> el motor de cada clase (cerv2/tamar/dlk/tir), y patas.py
--                     sólo para duales, que no tienen otro dueño.
--   ytm_ars        -> patas.py y nadie más, para todos los bullet que cubre.
--                     Requiere proyectar inflación o dólar según la pata, por
--                     eso lo calcula quien tiene los escenarios.
--
-- Los bonos con cupón (Botes, Boncer semestrales) quedan fuera de ytm_ars:
-- patas.py sólo modela bullet. Se ven en NULL, que es honesto.
--
-- Idempotente.
-- ============================================================================

alter table prices add column if not exists ytm_tipo text;
alter table prices add column if not exists ytm_ars  numeric;

comment on column prices.ytm is
  'TIR en la convención de cotización del instrumento. Leer SIEMPRE junto con '
  'ytm_tipo: un 3,91% real y un 29,55% nominal son el mismo bono.';
comment on column prices.ytm_tipo is
  'Convención de ytm: real_cer (sobre CER) | usd (sobre dólar) | nominal_ars.';
comment on column prices.ytm_ars is
  'TIR nominal anual en pesos. Comparable entre clases de activo. La escribe '
  'patas.py para los bullet; NULL en los bonos con cupón.';


-- ── Backfill ────────────────────────────────────────────────────────────────
-- Se replican las mismas reglas de reparto que usan los motores hoy, así la
-- columna queda correcta de entrada sin esperar a que cada uno corra.
update prices p
   set ytm_tipo = case
         when i.instrument_type in ('ON', 'HD') then 'usd'
         when i.referencias = 'A3500'           then 'usd'
         when i.referencias = 'CER'             then 'real_cer'
         else                                        'nominal_ars'
       end
  from instruments_v2 i
 where i.symbol = p.symbol
   and p.ytm is not null;


-- ── Vista para el front ─────────────────────────────────────────────────────
-- Expone las tres lecturas por separado, para que una tabla nunca tenga que
-- decidir qué significa la columna que está ordenando.
create or replace view v_prices_ytm as
select p.symbol,
       p.ytm,
       p.ytm_tipo,
       p.ytm_ars,
       case p.ytm_tipo when 'real_cer'    then p.ytm end as ytm_real,
       case p.ytm_tipo when 'usd'         then p.ytm end as ytm_usd,
       case p.ytm_tipo when 'nominal_ars' then p.ytm end as ytm_nominal,
       case p.ytm_tipo
         when 'real_cer'    then 'TIR real (s/CER)'
         when 'usd'         then 'TIR en USD'
         when 'nominal_ars' then 'TIR nominal ($)'
       end as ytm_label,
       p.duration_y, p.paridad, p.vpv, p.ts
from prices p;


-- ── Verificación ────────────────────────────────────────────────────────────
--   select ytm_tipo, count(*) from prices where ytm is not null
--    group by 1 order by 2 desc;
--
-- Esperado: usd (ON/HD + DLK), nominal_ars (FIJA + Tamar + duales),
--           real_cer (los Boncer/Lecer).
--
-- Después correr los motores para que empiecen a declarar ytm_tipo ellos
-- mismos y patas.py pueble ytm_ars.
