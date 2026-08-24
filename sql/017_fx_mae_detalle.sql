-- Detalle de rueda del mayorista, que MAE devuelve y no estábamos guardando.
--
-- El backend público de MAE (api.marketdata.mae.com.ar) trae, además del último
-- operado, la apertura, el máximo, el mínimo, la variación y el monto negociado.
-- Hasta ahora sólo se guardaba `last` en prices.UST y el resto se tiraba.
--
-- Las columnas van en `prices` y no en una tabla aparte porque son atributos de
-- rueda de cualquier instrumento, no del dólar: el día que ECO los exponga para
-- bonos, se llenan solas sin migrar nada.
--
-- cierreAyer y variacionPerc reusan closing_price y change_pct, que ya existen.
-- OJO con la unidad: change_pct se guarda como FRACCIÓN (-0.0129 = -1,29%) y MAE
-- devuelve variacionPerc en porcentaje, así que el código divide por 100.

alter table public.prices add column if not exists apertura       numeric;
alter table public.prices add column if not exists maximo         numeric;
alter table public.prices add column if not exists minimo         numeric;
alter table public.prices add column if not exists monto_operado  numeric;

comment on column public.prices.apertura      is 'Primer precio operado de la rueda.';
comment on column public.prices.maximo        is 'Máximo operado en la rueda.';
comment on column public.prices.minimo        is 'Mínimo operado en la rueda.';
comment on column public.prices.monto_operado is 'Monto total negociado en la rueda, en la moneda del instrumento.';
