-- ============================================================================
-- 012_series.sql
--
-- Unifica las series temporales en una sola tabla, con catálogo.
--
--   cer_historico    (fecha, valor_cer)              ─┐
--   tamar_historico  (fecha, valor_tna, valor_tem)   ─┼─>  series (serie, fecha, valor)
--   dólar A3500      (sólo el spot en prices.UST)    ─┘
--
-- POR QUÉ
-- Son la misma cosa —fecha y valor— con nombres de columna distintos, y cada una
-- se ganó su propio sync, su propio loader y su propio manejo de unidades. El
-- dólar directamente no tenía historia: cuando hubo que dar de alta TMVE8, el
-- A3500 de la fecha de emisión hubo que ir a buscarlo a la API a mano.
--
-- Con el catálogo, agregar una serie nueva es un INSERT: ni DDL ni código. El
-- sync recorre series_defs y trae lo que haya, así que sumar la BADLAR o la
-- inflación mensual del INDEC no requiere tocar nada.
--
-- UNIDADES: se guarda el valor CRUDO como lo publica el BCRA, igual que hace la
-- tabla rem. El catálogo dice en qué unidad está y quien lee convierte. Guardar
-- ya convertido esconde el origen y hace imposible auditar contra la fuente.
--
-- El REM NO entra acá: tiene percentiles y horizontes móviles, es otra forma.
--
-- Las vistas de compatibilidad son de SÓLO LECTURA (renombran columnas y filtran
-- por serie, así que no son auto-actualizables). Los dos escritores —cerv2.py y
-- updatetamar.py— se migran en el mismo commit; las vistas cubren a los lectores,
-- incluido cer-historico-table.tsx del front.
--
-- Idempotente.
-- ============================================================================

-- ── 1. Catálogo ─────────────────────────────────────────────────────────────
create table if not exists series_defs (
  serie       text primary key,
  descripcion text,
  fuente      text,             -- 'BCRA' | 'MAE' | ...
  fuente_id   text,             -- idVariable de la API del BCRA
  unidad      text,             -- 'pct_tna' | 'coeficiente' | 'ars_usd' | 'pct_mensual'
  activa      boolean not null default true
);

insert into series_defs (serie, descripcion, fuente, fuente_id, unidad) values
  ('cer',       'Coeficiente de Estabilización de Referencia', 'BCRA', '30', 'coeficiente'),
  ('tamar_tna', 'TAMAR bancos privados, TNA',                  'BCRA', '44', 'pct_tna'),
  ('a3500',     'Tipo de cambio mayorista de referencia A3500','BCRA', '5',  'ars_usd'),
  ('ipc_mensual','Inflación mensual INDEC',                    'BCRA', '27', 'pct_mensual')
on conflict (serie) do nothing;


-- ── 2. La tabla ─────────────────────────────────────────────────────────────
create table if not exists series (
  serie text not null references series_defs(serie),
  fecha date not null,
  valor numeric not null,
  ts    timestamptz not null default now(),
  primary key (serie, fecha)
);

create index if not exists series_fecha_idx on series (serie, fecha desc);

comment on table series is
  'Series temporales de una sola dimensión. Valor CRUDO en la unidad que declara '
  'series_defs.unidad: la TAMAR viene en % (23.25 = 23,25% TNA), el CER como '
  'coeficiente y el A3500 en $/USD. Quien lee convierte.';


-- ── 3. Migrar los datos ─────────────────────────────────────────────────────
insert into series (serie, fecha, valor)
select 'cer', fecha, valor_cer from cer_historico where valor_cer is not null
on conflict (serie, fecha) do nothing;

insert into series (serie, fecha, valor)
select 'tamar_tna', fecha, valor_tna from tamar_historico where valor_tna is not null
on conflict (serie, fecha) do nothing;

-- valor_tem no se migra: es derivado de valor_tna y ningún consumidor lo lee
-- (patas.py y tamar.py calculan la TEM ellos). La vista de compat lo recalcula
-- con la fórmula del prospecto para no romper a nadie que lo pida.


-- ── 4. Vistas de compatibilidad (SÓLO LECTURA) ──────────────────────────────
do $$
begin
  if exists (select 1 from information_schema.tables
              where table_schema='public' and table_name='cer_historico'
                and table_type='BASE TABLE') then
    alter table cer_historico rename to zz_dropped_cer_historico;
  end if;
  if exists (select 1 from information_schema.tables
              where table_schema='public' and table_name='tamar_historico'
                and table_type='BASE TABLE') then
    alter table tamar_historico rename to zz_dropped_tamar_historico;
  end if;
end $$;

create or replace view cer_historico as
  select fecha, valor as valor_cer from series where serie = 'cer';

create or replace view tamar_historico as
  select fecha,
         valor as valor_tna,
         -- TEM del prospecto: [(1 + TNA/(365/32))^(365/32)]^(1/12) - 1, en %
         (power(power(1 + (valor/100) / (365.0/32), 365.0/32), 1.0/12) - 1) * 100
           as valor_tem
    from series where serie = 'tamar_tna';

comment on view cer_historico   is 'COMPAT de sólo lectura -> series. Escribir en series.';
comment on view tamar_historico is 'COMPAT de sólo lectura -> series. Escribir en series.';


-- ── 5. Limpieza de lo ya verificado ─────────────────────────────────────────
-- La fusión de flujos (011) quedó verificada: 2.194 filas, 1.809 con proyección.
drop table if exists zz_dropped_instrument_flows_proyectados;


-- ── Verificación ────────────────────────────────────────────────────────────
--   select serie, count(*), min(fecha), max(fecha) from series group by 1 order by 1;
--   -> cer 60, tamar_tna 449 (a3500 e ipc_mensual entran con series_sync.py)
--
--   select count(*) from cer_historico;    -- 60, por la vista
--   select count(*) from tamar_historico;  -- 449
--
-- Después correr:  python series_sync.py
-- que carga el histórico de a3500 e ipc_mensual y deja las cuatro al día.
