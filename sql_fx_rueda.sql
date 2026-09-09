-- ─────────────────────────────────────────────────────────────────────────────
-- fx_mae_rueda — recorrido intradiario del dólar mayorista de MAE.
--
-- POR QUÉ ESTA SÍ VA A LA BASE
-- El precio del mayorista vive en `prices` bajo UST, pero ahí se hace upsert:
-- cada lectura pisa la anterior y del recorrido de la rueda no queda nada. A
-- diferencia de una curva —que se recalcula del estado actual cuando haga
-- falta— esto no se puede reconstruir después. O se guarda cuando pasa, o se
-- pierde.
--
-- EL VOLUMEN ES ACUMULADO
-- `monto_operado` es el negociado del día hasta ese instante, no el del minuto.
-- Lo que se opera en cada intervalo es la DIFERENCIA contra la lectura anterior,
-- y de ahí sale el tamaño de cada burbuja del gráfico. Se guarda crudo, como lo
-- publica MAE, para que el dato siga siendo auditable contra la fuente.
--
-- TAMAÑO
-- El relay corre cada 60 segundos entre las 10 y las 16, así que son unas 300
-- filas por rueda: ~78 mil al año. Nada.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.fx_mae_rueda (
  ts             timestamptz not null default now(),
  symbol         text        not null default 'UST$T',
  last           numeric     not null,   -- último operado
  apertura       numeric,
  maximo         numeric,
  minimo         numeric,
  cierre_anterior numeric,
  monto_operado  numeric,                -- ACUMULADO del día, no del minuto
  primary key (symbol, ts)
);

comment on table public.fx_mae_rueda is
  'Una fila por lectura del mayorista MAE. La escribe fx_relay.py cada 60s. '
  'monto_operado es acumulado: el volumen del intervalo es su diferencia.';

-- El gráfico siempre pide "la rueda de tal día, en orden".
create index if not exists fx_mae_rueda_dia
  on public.fx_mae_rueda (symbol, ts desc);

-- El front lee con la anon key; el relay escribe con la service key, que saltea
-- RLS. Si `prices` usa otro criterio, copiale ése en vez de dejar el de abajo.
alter table public.fx_mae_rueda enable row level security;

drop policy if exists fx_mae_rueda_lectura on public.fx_mae_rueda;
create policy fx_mae_rueda_lectura
  on public.fx_mae_rueda
  for select
  to anon, authenticated
  using (true);
