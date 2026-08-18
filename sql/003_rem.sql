-- ============================================================================
-- 003_rem.sql
--
-- Tabla del REM (Relevamiento de Expectativas de Mercado, BCRA) y los
-- escenarios que se apoyan en él.
--
-- La carga la hace rem_sync.py parseando el XLSX mensual del BCRA. Se usa el
-- XLSX y no la API de estadísticas porque la API sólo expone la mediana del IPC
-- interanual a 12 meses (idVariable 29): un único horizonte, sin percentiles y
-- sin TAMAR ni tipo de cambio. El XLSX trae los tres, con p10/p25/p75/p90.
--
-- Cobertura del informe de jul-2026, para dimensionar:
--   ipc    mensual jul-26→ene-27 · anual dic-26 / dic-27 / dic-28
--   tamar  mensual ago-26→ene-27 · anual dic-26 / dic-27
--   tcn    mensual ago-26→ene-27 · anual dic-26 / dic-27
--
-- Más allá de esos horizontes hay que extrapolar. Los motores lo hacen
-- manteniendo plano el último anual disponible y dejando asentado en
-- valuations.params desde qué fecha arranca la extrapolación, para que se vea
-- qué tramo es REM y qué tramo es supuesto propio.
--
-- Requiere 001_patas_duales.sql. Idempotente.
-- ============================================================================

create table if not exists rem (
  fecha_rem  date not null,   -- mes del relevamiento (2026-07-01 = informe jul-2026)
  variable   text not null,   -- 'ipc' | 'ipc_nucleo' | 'tamar' | 'tcn'
  periodo    text not null,   -- '2026-08-31' | 'próx. 12 meses' | '2027'
  tipo       text not null,   -- 'mensual' | 'anual' | 'horizonte'
  referencia text,            -- 'var. % mensual' | 'TNA; %' | '$/USD'
  fecha_ref  date,            -- fecha a la que aplica, cuando es parseable

  mediana  numeric,
  promedio numeric,
  desvio   numeric,
  maximo   numeric,
  minimo   numeric,
  p90      numeric,
  p75      numeric,
  p25      numeric,
  p10      numeric,
  n_participantes int,

  ts timestamptz not null default now(),
  primary key (fecha_rem, variable, periodo)
);

create index if not exists rem_var_fecha_idx on rem (variable, fecha_rem desc);

comment on table rem is
  'REM del BCRA. Una fila por (informe, variable, período). Las unidades siguen '
  'la columna referencia: el IPC viene en % (1.95 = 1,95% mensual), TAMAR en TNA % '
  'y el tipo de cambio en $/USD. Los motores dividen por 100 donde corresponde.';


-- ── Escenarios ──────────────────────────────────────────────────────────────
-- Convención: si el escenario no fija un supuesto, el motor usa su default y
-- deja el origen asentado en valuations.params.origen_proy.
--
-- 'base' es el que va en pantalla:
--   · TAMAR -> promedio de los últimos 5 datos observados, plano. NO se toca:
--     es la convención con la que el mercado quotea la TIR de los TAMAR.
--   · CER   -> mediana del REM. No hay convención de "últimos N" que sirva acá;
--     extrapolar la inflación de los últimos datos a 2029/2030 no tiene sentido.
update scenarios
   set nombre    = 'Base',
       supuestos = '{"cer_fuente": "rem", "cer_percentil": "mediana"}'::jsonb,
       fuente    = 'TAMAR: promedio últimos 5 observados · CER: mediana REM'
 where id = 'base';

insert into scenarios (id, nombre, supuestos, fuente) values
  ('rem_p25', 'REM p25',
   '{"cer_fuente":"rem","cer_percentil":"p25","tamar_fuente":"rem","tamar_percentil":"p25"}'::jsonb,
   'BCRA REM, percentil 25'),
  ('rem_p50', 'REM mediana',
   '{"cer_fuente":"rem","cer_percentil":"mediana","tamar_fuente":"rem","tamar_percentil":"mediana"}'::jsonb,
   'BCRA REM, mediana'),
  ('rem_p75', 'REM p75',
   '{"cer_fuente":"rem","cer_percentil":"p75","tamar_fuente":"rem","tamar_percentil":"p75"}'::jsonb,
   'BCRA REM, percentil 75')
on conflict (id) do nothing;

-- Nota sobre rem_p50 vs base: no son lo mismo. Los dos usan mediana REM para
-- CER, pero base deja TAMAR en la convención de últimos-5 y rem_p50 la proyecta
-- con REM. Sirve justamente para medir cuánto cambia la TIR por el supuesto.


-- ── Verificación ────────────────────────────────────────────────────────────
--   python rem_sync.py --dry-run     # parsea sin escribir
--   python rem_sync.py               # carga
--
--   select variable, tipo, count(*), max(fecha_rem)
--     from rem group by 1,2 order by 1,2;
--   -> ipc/ipc_nucleo: 7 mensual + 2 horizonte + 3 anual
--      tamar/tcn:      6 mensual + 1 horizonte + 2 anual
