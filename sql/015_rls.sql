-- ============================================================================
-- 015_rls.sql
--
-- Activa Row Level Security. Hoy TODAS las tablas están UNRESTRICTED: con la
-- clave anónima —que viaja en el bundle del front y es pública por diseño—
-- cualquiera puede escribir o borrar cualquier tabla desde la consola del
-- navegador. Un `delete from prices` de un desconocido es hoy una posibilidad
-- real.
--
-- QUÉ HACE ESTA MIGRACIÓN
-- Cierra las ESCRITURAS y deja las lecturas como están. Es el cambio de mayor
-- impacto con cero riesgo de romper algo.
--
-- LECTURA PÚBLICA, a propósito y no por descuido: el middleware sólo protege
-- /protected/*, así que /soberanos-ars, /duales, /ons y el resto del dashboard
-- son accesibles sin login y leen con la clave anónima. Restringir la lectura a
-- `authenticated` apagaría el sitio entero para cualquiera que no esté logueado.
-- Si querés que el dashboard pida login, es una decisión de producto: se cambia
-- el matcher del middleware y después acá `anon` -> `authenticated`.
--
-- ESCRITURAS: ninguna para anon ni authenticated, con una excepción.
--   · El backend usa la SERVICE_KEY, que SALTA RLS por diseño. Ni patas.py ni
--     ningún motor se entera de esto.
--   · La API de feriados escribe del lado servidor con service key: también
--     sigue funcionando.
--   · Los uploaders del panel de admin escriben desde el NAVEGADOR con la clave
--     anónima, sobre instruments_test e instrument_flows_test. Se les deja
--     INSERT para no romperlos, pero ver la advertencia al final.
--
-- Idempotente.
-- ============================================================================

-- ── 1. RLS en todo lo que sea tabla ─────────────────────────────────────────
do $$
declare t record;
begin
  for t in
    select tablename from pg_tables where schemaname = 'public'
  loop
    execute format('alter table public.%I enable row level security', t.tablename);
  end loop;
end $$;


-- ── 2. Lectura ──────────────────────────────────────────────────────────────
-- Una política de SELECT por tabla, para anon y authenticated. Sin esto, activar
-- RLS deniega TODO y el front queda en blanco: RLS sin políticas es denegar.
do $$
declare t record;
begin
  for t in
    select tablename from pg_tables where schemaname = 'public'
  loop
    execute format('drop policy if exists lectura_publica on public.%I', t.tablename);
    execute format(
      'create policy lectura_publica on public.%I for select to anon, authenticated using (true)',
      t.tablename);
  end loop;
end $$;


-- ── 3. Escritura: sólo las tablas de staging del panel de admin ─────────────
-- Son destinos de carga cruda, no datos de producción: lo peor que puede pasar
-- ahí es que alguien las ensucie, no que corrompa una valuación.
do $$
declare t text;
begin
  foreach t in array array['instruments_test', 'instrument_flows_test'] loop
    if exists (select 1 from pg_tables where schemaname='public' and tablename=t) then
      execute format('drop policy if exists carga_staging on public.%I', t);
      execute format(
        'create policy carga_staging on public.%I for insert to anon, authenticated with check (true)', t);
    end if;
  end loop;
end $$;


-- ── Verificación ────────────────────────────────────────────────────────────
--   select tablename,
--          rowsecurity as rls,
--          (select count(*) from pg_policies p
--            where p.schemaname='public' and p.tablename=t.tablename) as politicas
--     from pg_tables t where schemaname='public' order by 1;
--
-- Todas tienen que quedar con rls=true y al menos 1 política.
--
-- Prueba real, desde la consola del navegador en el sitio:
--   await supabase.from('prices').select('symbol').limit(1)   -> anda
--   await supabase.from('prices').delete().eq('symbol','X')   -> 0 filas, sin permiso
--
-- Y que el backend siga entero: python run.py, los 7 pasos en verde.


-- ── LO QUE ESTO *NO* RESUELVE ───────────────────────────────────────────────
--
-- 1. El panel de admin es público. /(dash)/admin no está en el matcher del
--    middleware, así que cualquiera puede entrar y usar los uploaders sobre las
--    tablas *_test. Se arregla en el front, no acá: agregar /(dash) al matcher, o
--    al menos /admin.
--
-- 2. app/api/feriados/route.ts tiene la contraseña HARDCODEADA como default:
--        process.env.FERIADOS_PASSWORD ?? "trolazo123"
--    Está en el repo de GitHub. Cualquiera que lo lea puede agregar o borrar
--    feriados, y los feriados definen las ventanas de 10 días hábiles de TODOS
--    los prospectos: tocarlos mueve las valuaciones. Además esa ruta escribe con
--    service key, así que RLS no la frena. Hay que sacar el default y exigir la
--    variable de entorno.
--
-- 3. Las vistas (v_duales, v_prices_ytm, ...) corren con los permisos de su
--    dueño, así que no aplican RLS de las tablas de abajo. Acá no agrega
--    exposición —anon ya puede leer esas tablas— pero si algún día una tabla
--    pasa a ser privada, hay que revisar las vistas que la tocan.
