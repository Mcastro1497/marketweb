"""
valuar_loop.py — Revaluación continua durante la rueda.

POR QUÉ EXISTE
Una corrida de patas.py tarda ~3,8 segundos, pero medido por partes:

    cálculo (41 bonos)   0,24s   <- lo único que es cómputo
    instrumentos+patas   1,19s   ┐
    series + feriados    1,05s   ├ no cambian dentro del día
    REM                  0,38s   ┘
    precios              0,52s   <- lo único que hay que releer

O sea que el 94% del tiempo se va releyendo de Supabase cosas que no cambiaron.
Este bucle las carga UNA VEZ y después, cada N segundos, sólo relee precios y
recalcula los símbolos que se movieron. Ciclo típico: menos de un segundo.

QUÉ CUBRE
Todo el universo valuable, en dos familias:

  · PATAS   bullet y duales (FIJA, TAMAR, CER, DLK). Fórmula cerrada.
  · FLUJOS  ONs, hard dollar, dólar linked y soberanos ARS. XIRR sobre la tabla
            de flujos.

El XIRR también es despreciable: 0,03 segundos para 197 bonos, 0,1 ms cada uno.
Los 13-19 segundos que tarda tir.py son, otra vez, todo carga de datos.

Para no reimplementar la lógica de cada motor —y que después diverjan en
silencio— el cálculo pasa por lib.tasas.tir_y_duracion, que es el mismo código
que usan tir.py, dlk.py y cerv2.py. Lo único propio de cada familia es cómo se
AJUSTA el precio antes de entrar ahí:

    ON / HD        precio en dólares
    dólar linked   precio en pesos / tipo de cambio
    CER            precio deflactado por el ratio CER  -> TIR real
    FIJA           precio tal cual                     -> TIR nominal

RECARGA DE REFERENCIA
Cada --refresco minutos se vuelven a leer instrumentos, series y REM. Sin eso, un
bucle que arranca a las 10:00 no vería el CER del día si el BCRA lo publica más
tarde, ni un bono dado de alta a media rueda.

Uso:
    python valuar_loop.py                          # cada 5s, hasta que lo cortes
    python valuar_loop.py --intervalo 10
    python valuar_loop.py --hasta 17:15            # se apaga solo, como precios2
    python valuar_loop.py --refresco 30            # recargar referencia cada 30 min
    python valuar_loop.py --una-vez                # un ciclo y sale (para probar)
"""
import argparse
import signal
import sys
import time
from datetime import date, datetime, timezone

import patas as P
from lib.tasas import tir_y_duracion

# cerv2 lee sys.argv al importarse (APPLY y SYM_ARG son módulo-level), así que se
# lo blanquea durante el import: si no, tomaría los argumentos de este script.
_argv = sys.argv[:]
sys.argv = [sys.argv[0]]
import cerv2 as C
import tir as TIR
import dlk as D
sys.argv = _argv

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import os

_corriendo = True


def _tz():
    """Zona local, de LOCAL_TZ. Misma convención que precios2.py y dlk.py."""
    nombre = os.getenv("LOCAL_TZ", "America/Argentina/Cordoba")
    if ZoneInfo:
        try:
            return ZoneInfo(nombre)
        except Exception:
            pass
    return timezone.utc


def _parar(sig, frame):
    global _corriendo
    print("\nDeteniendo…")
    _corriendo = False


class Referencia:
    """Todo lo que no cambia dentro del día, cargado una vez.

    Se guarda el instante de carga para poder recargar cada tanto: el CER del día
    puede publicarse con el mercado abierto, y un bono nuevo puede darse de alta a
    media rueda.
    """

    def __init__(self, escenario: str, hoy: date | None = None):
        self.escenario = escenario
        self.hoy = hoy
        self.cargar()

    def cargar(self):
        t0 = time.monotonic()
        self.ctx = P.Ctx(self.hoy or date.today())
        self.insts = P.cargar_instrumentos()
        self.patas = P.cargar_patas()
        self.esc = P.cargar_escenario(self.escenario)
        # Forzar las cachés perezosas ahora y no en el primer ciclo, para que el
        # costo no aparezca disfrazado de latencia de valuación.
        _ = self.ctx.feriados
        for leg, _p in ((l, p) for v in self.patas.values() for l, p in v):
            if leg == "TAMAR":
                _ = self.ctx.tamar
            elif leg == "CER":
                _ = self.ctx.cer
                _ = self.ctx.rem_inflacion(self.esc.get("cer_percentil", "mediana"))
            elif leg == "DLK":
                _ = self.ctx.fx_spot
                _ = self.ctx.rem_devaluacion(self.esc.get("fx_percentil", "mediana"))
        self.cargada = time.monotonic()
        print(f"[REF] {len(self.insts)} instrumentos, {len(self.patas)} con patas, "
              f"escenario '{self.escenario}'  ({self.cargada - t0:.1f}s)")

    def vencida(self, minutos: int) -> bool:
        return (time.monotonic() - self.cargada) > minutos * 60


class ReferenciaFlujos:
    """Universo que valúa por XIRR sobre flujos: ONs, hard dollar, dólar linked y
    soberanos ARS. Se cachea todo salvo el precio.

    Para cada bono se guarda cómo hay que AJUSTAR el precio antes de calcular la
    TIR, que es lo único que distingue a una familia de otra, y en qué convención
    queda el resultado (ver sql/006_ytm_semantica.sql).
    """

    def __init__(self):
        self.cargar()

    def cargar(self):
        t0 = time.monotonic()
        hols = TIR.load_holidays()
        cut, self.val_dt = TIR.cutoff(datetime.now(timezone.utc), hols)

        # ── ONs y hard dollar: precio y flujos en dólares ──
        inst_usd, _ = TIR.load_instruments_and_prices()
        syms_usd = [r["symbol"] for r in inst_usd]

        # ── dólar linked: flujos en USD, precio en pesos a convertir ──
        inst_dlk, _ = D.load_dlk_instruments_and_prices()
        syms_dlk = [r["symbol"] for r in inst_dlk]

        # ── soberanos ARS: CER (precio deflactado) y FIJA (precio tal cual) ──
        inst_ars, _ = C.ars_instruments()
        ctx_cer = C.ctx_actual()
        self.ratio = {b["symbol"]: C.ratio_de(b, ctx_cer) for b in inst_ars}

        # `familia` decide CÓMO se ajusta el precio; `conv` es la unidad en la que
        # queda la TIR y va a prices.ytm_tipo. No son lo mismo: ON/HD y dólar
        # linked reportan los dos "usd" pero se ajustan distinto.
        self.familia, self.conv = {}, {}
        for r in inst_usd:
            self.familia[r["symbol"]] = "usd_ticker"
            self.conv[r["symbol"]] = "usd"
        for sdlk in syms_dlk:
            # Pisa a usd_ticker si un símbolo estuviera en las dos listas: para un
            # DLK manda el criterio de dlk.py.
            self.familia[sdlk] = "usd_dlk"
            self.conv[sdlk] = "usd"
        for b in inst_ars:
            self.familia[b["symbol"]] = "cer" if b["tipo"] == "CER" else "fija"
            self.conv[b["symbol"]] = "real_cer" if b["tipo"] == "CER" else "nominal_ars"

        todos = list(dict.fromkeys(syms_usd + syms_dlk + [b["symbol"] for b in inst_ars]))
        df = TIR.load_flows(cut, todos)
        self.flujos = {}
        if not df.empty:
            for sym, sub in df.groupby("symbol"):
                self.flujos[sym] = [(r.fecha_pago.to_pydatetime(), float(r.total))
                                    for r in sub.itertuples() if float(r.total) != 0]

        self.fx = D.get_fx_oficial()
        self.cargada = time.monotonic()
        print(f"[REF-FLUJOS] {len(self.flujos)} bonos con flujos "
              f"(USD {len(syms_usd)}, DLK {len(syms_dlk)}, ARS {len(inst_ars)})  "
              f"FX {self.fx}  ({self.cargada - t0:.1f}s)")

    def vencida(self, minutos: int) -> bool:
        return (time.monotonic() - self.cargada) > minutos * 60

    def precio_ajustado(self, sym: str, fila) -> tuple:
        """(precio_para_el_xirr, convención). None si no se puede valuar.

        Cada familia ajusta distinto, y confundirlas da números muy equivocados:
        al principio usaba price_ars_usd para los dólar linked y daba hasta 706 bps
        de más. Esa columna la calcula precios2 con el MEP, no con el oficial, y
        dlk.py convierte con el OFICIAL.
        """
        fam = self.familia.get(sym)
        conv = self.conv.get(sym)
        if fam is None:
            return None, None

        if fam == "usd_ticker":
            # tir.py: precio del ticker en dólares, con price_ars_usd de respaldo.
            px = P._f(fila.get("last")) or P._f(fila.get("price_ars_usd"))
            return (px if px and px > 0 else None), conv

        ars = P._f(fila.get("price_ars")) or P._f(fila.get("closing_price"))
        if not ars or ars <= 0:
            return None, conv

        if fam == "usd_dlk":
            # dlk.py: SIEMPRE precio en pesos dividido por el tipo de cambio
            # OFICIAL. Nunca price_ars_usd, que va por MEP.
            return ((ars / self.fx) if self.fx else None), conv
        if fam == "cer":
            return ars / (self.ratio.get(sym) or 1.0), conv
        return ars, conv


def _precio_de(row) -> float | None:
    return P._f(P._pick(row, P.POSIBLES_PRECIO)) if row else None


def ciclo(ref: Referencia, previos: dict, args) -> tuple:
    """Un ciclo: relee precios, recalcula lo que cambió, escribe. Devuelve
    (precios_nuevos, cuántos se revaluaron, segundos)."""
    t0 = time.monotonic()
    precios = P.cargar_precios(list(ref.patas.keys()) or None)

    # Sólo los símbolos cuyo precio se movió. Es lo que hace que el ciclo sea
    # barato: en una rueda normal se mueven unos pocos por vez, no los 41.
    cambiados = []
    for sym in ref.patas:
        p = _precio_de(precios.get(sym))
        if p is not None and p != previos.get(sym):
            cambiados.append(sym)

    if not cambiados:
        return precios, 0, time.monotonic() - t0

    ts = datetime.now(timezone.utc).isoformat()
    filas_val, filas_px = [], []
    for sym in cambiados:
        inst = ref.insts.get(sym)
        if inst is None:
            continue
        try:
            res = P.valuar_simbolo(ref.ctx, inst, ref.patas[sym], ref.esc, precios.get(sym))
        except Exception as e:
            print(f"  [ERROR] {sym}: {str(e)[:80]}")
            continue
        if not res:
            continue
        dual = len(res) > 1
        for leg, pata, x in res:
            filas_val.append({
                "symbol": sym, "leg": leg, "scenario": args.escenario,
                "vpv": P._r(pata.vpv, 6), "vt": P._r(pata.vt, 6), "tem": P._r(pata.tem),
                "driver": P._r(pata.driver), "ytm": P._r(x["ytm"], 6),
                "ytm_nativa": P._r(x["ytm_nativa"], 6), "ytm_conv": x["ytm_conv"],
                "duration_y": P._r(x["duration_y"], 6), "breakeven": P._r(x["breakeven"]),
                "is_winner": x["is_winner"], "params": pata.params, "ts": ts,
            })
        g = next(r for r in res if r[2]["is_winner"])
        head = {"symbol": sym, "vpv": P._r(g[1].vpv, 4), "ts": ts}
        if g[2]["ytm"] is not None:
            head["ytm_ars"] = P._r(g[2]["ytm"], 6)
            if dual:
                head["ytm"] = P._r(g[2]["ytm"], 6)
                head["ytm_tipo"] = "nominal_ars"
                head["duration_y"] = P._r(g[2]["duration_y"], 6)
        if dual and g[2]["paridad"] is not None:
            head["paridad"] = P._r(g[2]["paridad"], 4)
        filas_px.append(head)

    if not args.dry_run:
        for i in range(0, len(filas_val), 500):
            P.sb.table("valuations").upsert(filas_val[i:i + 500]).execute()
        for i in range(0, len(filas_px), 500):
            P.sb.table("prices").upsert(filas_px[i:i + 500]).execute()

    return precios, len(cambiados), time.monotonic() - t0


def ciclo_flujos(ref: "ReferenciaFlujos", previos: dict, args) -> tuple:
    """Revalúa por XIRR los bonos de flujos cuyo precio se movió."""
    t0 = time.monotonic()
    filas = P.cargar_precios(list(ref.conv.keys()) or None)

    cambiados = []
    for sym in ref.conv:
        f = filas.get(sym)
        if not f:
            continue
        px, _conv = ref.precio_ajustado(sym, f)
        if px is not None and px != previos.get(sym):
            cambiados.append((sym, px))

    if not cambiados:
        return {s: p for s, p in ((s, ref.precio_ajustado(s, filas[s])[0])
                                  for s in ref.conv if filas.get(s)) if p}, 0, time.monotonic() - t0

    ts = datetime.now(timezone.utc).isoformat()
    salida, nuevos = [], dict(previos)
    for sym, px in cambiados:
        fl = ref.flujos.get(sym)
        if not fl:
            continue
        ytm, dur = tir_y_duracion(ref.val_dt, px, fl)
        nuevos[sym] = px
        if ytm is None:
            continue
        salida.append({
            "symbol": sym, "ytm": round(ytm, 6),
            "ytm_tipo": ref.conv[sym],
            "duration_y": round(dur, 6) if dur else None,
            "ts": ts,
        })

    if salida and not args.dry_run:
        for i in range(0, len(salida), 500):
            P.sb.table("prices").upsert(salida[i:i + 500]).execute()

    return nuevos, len(salida), time.monotonic() - t0


def main():
    ap = argparse.ArgumentParser(description="Revaluación continua durante la rueda")
    ap.add_argument("--intervalo", type=float, default=5, help="segundos entre ciclos (default 5)")
    ap.add_argument("--refresco", type=int, default=30,
                    help="minutos entre recargas de la referencia (default 30)")
    ap.add_argument("--escenario", default="base")
    ap.add_argument("--hasta", metavar="HH:MM", help="apagarse solo a esta hora local")
    ap.add_argument("--una-vez", action="store_true", help="un solo ciclo y salir")
    ap.add_argument("--solo-patas", action="store_true",
                    help="omitir el universo de flujos (ONs, HD, DLK, soberanos ARS)")
    ap.add_argument("--dry-run", action="store_true", help="calcula pero no escribe")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _parar)
    signal.signal(signal.SIGTERM, _parar)

    limite = None
    if args.hasta:
        h, m = args.hasta.split(":")
        limite = (int(h), int(m))

    ref = Referencia(args.escenario)
    ref_f = None if args.solo_patas else ReferenciaFlujos()
    previos: dict = {}
    previos_f: dict = {}
    n_ciclos = total_reval = 0
    t_inicio = time.monotonic()

    if args.una_vez:
        _, n, dt = ciclo(ref, previos, args)
        print(f"[CICLO patas]  {n:3} revaluados en {dt:.2f}s")
        if ref_f:
            _, nf, dtf = ciclo_flujos(ref_f, previos_f, args)
            print(f"[CICLO flujos] {nf:3} revaluados en {dtf:.2f}s")
        return 0

    print(f"[LOOP] cada {args.intervalo}s, referencia cada {args.refresco} min"
          f"{f', hasta las {args.hasta}' if args.hasta else ''}"
          f"{'  (dry-run)' if args.dry_run else ''}")

    while _corriendo:
        if limite:
            ahora = datetime.now(_tz())
            if (ahora.hour, ahora.minute) >= limite:
                print(f"[LOOP] hora de cierre ({args.hasta}), terminando.")
                break
        if ref.vencida(args.refresco):
            print("[REF] recargando referencia…")
            ref.cargar()

        previos, n, dt = ciclo(ref, previos, args)
        nf, dtf = 0, 0.0
        if ref_f:
            if ref_f.vencida(args.refresco):
                ref_f.cargar()
            previos_f, nf, dtf = ciclo_flujos(ref_f, previos_f, args)
        n_ciclos += 1
        total_reval += n + nf
        if n or nf:
            print(f"[{datetime.now():%H:%M:%S}] patas {n:3} ({dt:.2f}s)  "
                  f"flujos {nf:3} ({dtf:.2f}s)")
        dt = dt + dtf
        # Descontar lo que tardó el ciclo, para que el período sea el pedido y no
        # el pedido más el trabajo.
        time.sleep(max(0.0, args.intervalo - dt))

    mins = (time.monotonic() - t_inicio) / 60
    print(f"[LOOP] {n_ciclos} ciclos en {mins:.1f} min, {total_reval} revaluaciones")
    return 0


if __name__ == "__main__":
    sys.exit(main())
