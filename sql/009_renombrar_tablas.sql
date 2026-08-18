-- ============================================================================
-- 009_renombrar_tablas.sql
--
-- Saca los sufijos de versión. Los nombres pasan a decir QUÉ es cada tabla en
-- vez de en qué orden se creó.
--
--   instrument_flows_v2  ->  instrument_flows                (todos los flujos)
--   instrument_flows_v3  ->  instrument_flows_proyectados    (subset + proy. CER)
--   instruments          ->  tickers_legacy                  (registro por ticker)
--   instruments_v2       ->  instruments                     (registro por bono)
--
-- POR QUÉ instrument_flows_v3 NO se llama instrument_flows_v2 ni queda como está:
-- v2 y v3 nunca fueron dos generaciones. v2 tiene TODOS los flujos; v3 tiene los
-- soberanos ARS más las ONs que operan, con las columnas proyectadas por CER.
-- Llamarla "v3" sugiere que reemplaza a v2, y no la reemplaza. Si preferís otro
-- nombre, este es el momento: cambiarlo después cuesta lo mismo que esto.
--
-- POR QUÉ la vieja `instruments` NO se dropea:
-- Tiene 213 símbolos que `instruments_v2` no tiene. De esos, 196 son los
-- ticker_usd de filas de instruments_v2 (el modelo pasó de una fila por TICKER a
-- una fila por BONO) y 17 son huérfanos, varios de ellos vencidos y podados de
-- v2 (S14G6 venció el 14-08 y desapareció).
--
-- Y sobre todo: las 181 filas de `prices` que tienen TIR y no tienen instrumento
-- están TODAS ahí. `prices` está indexada por ticker (456 filas, incluye los
-- terminados en D) e `instruments_v2` por bono (272). Borrarla dejaría esas 181
-- filas sin ninguna fuente de identidad. Por eso se conserva con un nombre que
-- dice lo que es: un registro a nivel ticker.
--
-- VISTAS DE COMPATIBILIDAD
-- Cada nombre viejo queda como vista sobre la tabla nueva. Una vista de una sola
-- tabla en Postgres es auto-actualizable, así que soporta select, insert, update
-- y delete — y todos los escritores actuales usan justamente eso (load.py y
-- cerv2.py hacen delete+insert+update, ninguno upsert).
--
-- OJO: si alguna vez alguien hace .upsert() contra un nombre viejo, va a fallar.
-- Postgres no admite ON CONFLICT sobre vistas, porque necesita inferir un índice
-- real. Es la única cosa que las vistas no cubren.
--
-- Las vistas son una red de seguridad temporal, no el estado final. El código de
-- marketweb y de Market-Datita se actualiza en el mismo commit que esta
-- migración; las vistas están para lo que se me haya pasado. Cuando confirmes que
-- todo anda, correr 010_drop_compat.sql.
--
-- Idempotente.
-- ============================================================================

-- ── 1. Flujos ───────────────────────────────────────────────────────────────
-- El nombre instrument_flows quedó libre en 008 (la tabla sin consumidores pasó
-- a zz_dropped_instrument_flows).
do $$
begin
  if exists (select 1 from information_schema.tables
              where table_schema='public' and table_name='instrument_flows_v2'
                and table_type='BASE TABLE') then
    alter table public.instrument_flows_v2 rename to instrument_flows;
    raise notice 'instrument_flows_v2 -> instrument_flows';
  end if;

  if exists (select 1 from information_schema.tables
              where table_schema='public' and table_name='instrument_flows_v3'
                and table_type='BASE TABLE') then
    alter table public.instrument_flows_v3 rename to instrument_flows_proyectados;
    raise notice 'instrument_flows_v3 -> instrument_flows_proyectados';
  end if;
end $$;


-- ── 2. Instrumentos ─────────────────────────────────────────────────────────
-- Hay que liberar el nombre `instruments` antes de poder reusarlo, así que van
-- los dos renames en orden. La FK de instrument_legs viaja sola: apunta a la
-- tabla, no al nombre.
do $$
begin
  if exists (select 1 from information_schema.tables
              where table_schema='public' and table_name='instruments'
                and table_type='BASE TABLE')
     and exists (select 1 from information_schema.tables
                  where table_schema='public' and table_name='instruments_v2'
                    and table_type='BASE TABLE') then
    alter table public.instruments   rename to tickers_legacy;
    alter table public.instruments_v2 rename to instruments;
    raise notice 'instruments -> tickers_legacy ; instruments_v2 -> instruments';
  end if;
end $$;

comment on table tickers_legacy is
  'Registro a nivel TICKER (modelo viejo, era `instruments`). Conserva los 196 '
  'ticker_usd que `instruments` modela como columna, más 17 símbolos vencidos y '
  'podados. Es la única fuente de identidad de las ~181 filas de prices que no '
  'tienen instrumento. No borrar sin resolver antes el modelo de identidad de prices.';

comment on table instruments is
  'Registro a nivel BONO, una fila por instrumento (era `instruments_v2`). El '
  'ticker en dólares va en la columna ticker_usd, no como fila aparte.';

comment on table instrument_flows_proyectados is
  'Flujos con las columnas proyectadas por CER (era `instrument_flows_v3`). NO es '
  'una réplica de instrument_flows: cubre los soberanos ARS y las ONs que operan. '
  'Los ~75 símbolos ilíquidos que faltan están excluidos a propósito.';


-- ── 3. Vistas de compatibilidad ─────────────────────────────────────────────
create or replace view instrument_flows_v2 as select * from instrument_flows;
create or replace view instrument_flows_v3 as select * from instrument_flows_proyectados;
create or replace view instruments_v2      as select * from instruments;

comment on view instrument_flows_v2 is 'COMPAT temporal -> instrument_flows. Dropear con 010.';
comment on view instrument_flows_v3 is 'COMPAT temporal -> instrument_flows_proyectados. Dropear con 010.';
comment on view instruments_v2      is 'COMPAT temporal -> instruments. Dropear con 010.';


-- ── Verificación ────────────────────────────────────────────────────────────
--   select table_name, table_type from information_schema.tables
--    where table_schema='public' order by table_type, table_name;
--
-- Esperado: instrument_flows, instrument_flows_proyectados, instruments y
-- tickers_legacy como BASE TABLE; los tres nombres viejos como VIEW.
--
--   select count(*) from instruments_v2;   -- 272, por la vista
--   select count(*) from instruments;      -- 272, la tabla
--
-- El front y los motores tienen que seguir andando IGUAL antes de actualizar el
-- código, gracias a las vistas. Eso es lo que hay que comprobar primero.
