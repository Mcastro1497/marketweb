-- ============================================================================
-- 001_patas_duales.sql
--
-- Modelo de "patas" para valuación de bonos bullet, incluidos los duales.
--
-- Idea: un bono no tiene "un tipo", tiene patas. Un TAMAR común es un bono de
-- UNA pata; un dual es un bono de DOS (o N). Al vencimiento paga el máximo
-- entre las patas. Dar de alta un dual nuevo = insertar filas en
-- instrument_legs, sin tocar código.
--
-- ALCANCE: sólo instrumentos BULLET (periodicidad_int = 'Nula'). Un bono con
-- cupones no tiene un único "valor de pago al vencimiento" comparable entre
-- patas, así que TO26/TY30P/TX26/TX28/TX31/DICP/PARP/CUAP quedan fuera y los
-- siguen valuando tir.py y cerv2.py por flujos. Todos los duales emitidos son
-- bullet por construcción ("amortización íntegra al vencimiento").
--
-- Correr en el SQL editor de Supabase. Es idempotente.
-- ============================================================================


-- ── 1. Tipos de pata ────────────────────────────────────────────────────────
-- Catálogo, no un CHECK: sumar un tipo nuevo tiene que ser un INSERT, no un
-- ALTER TABLE. 'driver' es el nombre de la variable que maneja la pata; es la
-- que se despeja para calcular el breakeven contra otra pata.
create table if not exists leg_types (
  leg         text primary key,
  descripcion text,
  driver      text
);

insert into leg_types (leg, descripcion, driver) values
  ('FIJA',  'Tasa fija efectiva mensual capitalizable',        null),
  ('TAMAR', 'TAMAR promedio de la ventana + margen',           'tamar_tna'),
  ('CER',   'Ajuste por CER, zero-coupon',                     'infl_mens'),
  ('DLK',   'Ajuste por A3500 + spread',                       'fx_vto')
on conflict (leg) do nothing;


-- ── 2. Escenarios ───────────────────────────────────────────────────────────
-- Los supuestos de proyección viven acá, no repartidos como constantes en cada
-- script (hoy N_PROY=5 está hardcodeado en tamar.py). Si comparás una pata
-- TAMAR proyectada con un criterio contra una pata DLK proyectada con otro, el
-- max() entre ellas no significa nada.
--
-- Claves de 'supuestos' (todas opcionales; si falta una, el motor proyecta con
-- su default y lo deja asentado en valuations.params):
--   tamar_tna  numeric  TNA TAMAR asumida para el tramo NO observado
--   infl_mens  numeric  inflación mensual asumida (pata CER)
--   fx_vto     numeric  A3500 asumido al vencimiento (pata DLK)
create table if not exists scenarios (
  id        text primary key,
  nombre    text,
  supuestos jsonb not null default '{}'::jsonb,
  fuente    text,
  ts        timestamptz not null default now()
);

insert into scenarios (id, nombre, supuestos, fuente) values
  ('base', 'Base', '{}'::jsonb, 'proyección default de cada motor')
on conflict (id) do nothing;


-- ── 3. Patas de cada instrumento (INPUT) ────────────────────────────────────
-- Claves esperadas en 'params' según la pata:
--   FIJA  -> {"tem": 0.0217}                TEM fija capitalizable
--   TAMAR -> {"margen": 0.065}              margen sobre TAMAR, TNA decimal (0 en duales)
--   CER   -> {}                             usa instruments_v2.cer_emision
--   DLK   -> {"spread": 0.0664}             spread sobre A3500
create table if not exists instrument_legs (
  symbol text not null references instruments_v2(symbol) on delete cascade,
  leg    text not null references leg_types(leg),
  params jsonb not null default '{}'::jsonb,
  primary key (symbol, leg)
);

create index if not exists instrument_legs_leg_idx on instrument_legs (leg);


-- ── 4. Valuaciones (OUTPUT) ─────────────────────────────────────────────────
-- Una fila por (símbolo, pata, escenario).
--
-- CONTRATO: vpv está SIEMPRE en pesos, base 100 de valor nominal, al
-- vencimiento. Es lo único que hace comparables a las patas entre sí; si un
-- motor no puede respetarlo, no entra en este modelo.
create table if not exists valuations (
  symbol     text not null,
  leg        text not null,
  scenario   text not null default 'base' references scenarios(id) on delete cascade,

  vpv        numeric,   -- valor de pago al vencimiento, ARS base 100
  vt         numeric,   -- valor técnico devengado a hoy, ARS base 100
  tem        numeric,   -- TEM implícita de la pata (decimal)
  driver     numeric,   -- valor de la variable que maneja la pata
  ytm        numeric,   -- TIR anual efectiva SI esta pata fuera la que paga
  duration_y numeric,
  breakeven  numeric,   -- valor del driver que iguala esta pata con la ganadora
  is_winner  boolean not null default false,
  params     jsonb not null default '{}'::jsonb,  -- desglose libre del motor
  ts         timestamptz not null default now(),

  primary key (symbol, leg, scenario),
  foreign key (symbol, leg) references instrument_legs (symbol, leg) on delete cascade
);

create index if not exists valuations_winner_idx on valuations (symbol, scenario) where is_winner;


-- ── 5. Vistas para el front ─────────────────────────────────────────────────
-- Categoría derivada de las patas, no de instruments_v2.referencias. Un dual
-- nuevo aparece solo en el tab que corresponde, sin deploy.
create or replace view v_instrument_kind as
select symbol,
       count(*)::int as n_patas,
       case when count(*) > 1
            then 'DUAL:' || string_agg(leg, '/' order by leg)
            else max(leg)
       end as kind
from instrument_legs
group by symbol;

-- Un renglón por dual con las patas pivoteadas, listo para la tabla del panel.
create or replace view v_duales as
select v.symbol,
       v.scenario,
       (array_agg(v.leg order by v.vpv desc nulls last))[1] as ganadora,
       max(v.vpv)                                           as vpv_max,
       jsonb_object_agg(v.leg, jsonb_build_object(
         'vpv',       v.vpv,
         'tem',       v.tem,
         'driver',    v.driver,
         'ytm',       v.ytm,
         'breakeven', v.breakeven,
         'is_winner', v.is_winner,
         'params',    v.params
       ))                                                   as patas,
       max(v.ts)                                            as ts
from valuations v
where v.symbol in (select symbol from instrument_legs group by symbol having count(*) > 1)
group by v.symbol, v.scenario;


-- ── 6. Backfill ─────────────────────────────────────────────────────────────
-- Los instrumentos que ya existen quedan como bonos de UNA pata. Nada de lo que
-- hoy funciona cambia de comportamiento.
--
-- Se excluyen ONs (los valúa tir.py y no van al panel de soberanos) y todo lo
-- que pague cupones (ver ALCANCE arriba).
insert into instrument_legs (symbol, leg, params)
select i.symbol,
       case i.referencias
         when 'Tamar' then 'TAMAR'
         when 'CER'   then 'CER'
         when 'A3500' then 'DLK'
         else              'FIJA'
       end as leg,
       jsonb_strip_nulls(jsonb_build_object(
         'margen', case when i.referencias = 'Tamar'  then i.margen_ref end,
         'tem',    case when i.referencias is null    then i.tasa_int   end
       )) as params
from instruments_v2 i
where i.is_active
  and i.periodicidad_int = 'Nula'
  and i.instrument_type <> 'ON'
on conflict (symbol, leg) do nothing;


-- ── Chequeo post-migración ──────────────────────────────────────────────────
-- Esperado hoy: TAMAR 5, CER 14, DLK 5, FIJA 10 — y ningún dual todavía
-- (los duales entran en 002_alta_duales.sql).
--
--   select leg, count(*) from instrument_legs group by leg order by leg;
--   select kind, count(*) from v_instrument_kind group by kind order by 2 desc;
