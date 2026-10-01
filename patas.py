"""
patas.py — Motor genérico de valuación por ESTRUCTURA DE PAGOS.

Un bono no se valúa por quién lo emitió, se valúa por lo que paga. Un CER
provincial y un CER del Tesoro son el mismo problema; un corporativo en dólares
y un hard dollar soberano también. `instrument_type` (ON, HD, CER, SUBSOB, DUAL)
dice de dónde viene el papel y no alcanza para valuarlo: lo que decide es cómo
se ajusta el capital, en qué moneda paga y si paga todo junto al final o
amortiza antes.

De ahí salen las patas. Un bono no tiene "un tipo", tiene patas: una TAMAR común
es un bono de UNA pata, un dual es un bono de DOS y al vencimiento paga el
máximo entre ellas. Las patas de cada bono se deducen de su ficha
(`patas_por_estructura`) y instrument_legs queda sólo para los overrides: lo que
el prospecto define raro y no se puede leer de la ficha.

PATAS
    HD      sin ajuste, paga dólares          -> TIR en USD (absorbe tir.py)
    FIJA    sin ajuste, paga pesos            -> TIR nominal en pesos
    CER     capital ajustado por CER          -> TIR real
    DLK     capital atado al A3500            -> TIR en USD
    TAMAR   TAMAR promedio + margen           -> TIR nominal en pesos
    BADLAR  BADLAR + margen                   -> TIR nominal en pesos

BULLET vs CUPONES
    Un bono que paga todo al vencimiento se resuelve en forma cerrada: el motor
    proyecta el ajuste a la fecha de pago y deja un `driver` despejable, que es
    lo que necesita el breakeven de un dual. Uno que amortiza o paga renta antes
    se resuelve descontando su calendario (instrument_flows) por XIRR. Cada motor
    elige solo cuál de los dos le toca; ver `es_bullet`.

Lee:
  - instruments      (ficha)                  -> de ahí se deducen las patas
  - instrument_legs  (symbol, leg, params)    -> overrides, mandan sobre la ficha
  - instrument_flows (calendario de pagos)    -> bonos con cupones
  - scenarios        (id, supuestos)          -> supuestos de proyección
  - prices, holidays, series (cer / tamar_tna / a3500)
Escribe:
  - valuations       (symbol, leg, scenario)  -> una fila por pata
  - prices           (headline de la pata ganadora: ytm/duration_y/vpv/paridad)

CONTRATO DE UN MOTOR
    motor(ctx, inst, params, esc, driver=None) -> Pata

    Devuelve `vpv` SIEMPRE en pesos, base 100 de VN. Es lo único que hace
    comparables a las patas entre sí. `driver` es la variable que maneja la pata
    (TNA TAMAR, inflación mensual, dólar al vto.); si viene, pisa el supuesto del
    escenario. Ese parámetro es lo que permite calcular el breakeven por
    bisección sin escribir una fórmula por cada combinación de patas.

    Sumar un tipo de pata nuevo = una función + una entrada en MOTORES.
    Sumar un dual nuevo         = nada, si la ficha lo dice.

Uso:
    python patas.py                          # valúa todo y escribe
    python patas.py --dry-run                # no escribe nada
    python patas.py --check                  # compara contra lo que dejó tamar.py
    python patas.py --symbols TMF27,TTS26
    python patas.py --sin-tablas             # ignora instrument_legs: deduce todo
    python patas.py --loop                   # ciclo continuo cada INTERVAL_SEC
"""
import argparse
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, NamedTuple

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY  = os.environ["SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SERVICE_KEY)

INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "1800"))
# Cuántos datos recientes de TAMAR se promedian para proyectar el tramo futuro.
# Supuesto de modelo, NO es el "10 días hábiles" del prospecto.
N_PROY = int(os.getenv("N_PROY", "5"))

POSIBLES_PRECIO = ["price_ars", "closing_price", "price", "last", "px", "ultimo", "cierre", "precio"]


# ════════════════════════════ contrato ════════════════════════════
class Calendario(NamedTuple):
    """Lo que queda por cobrar de un bono. Ver `calendario()`."""
    futuros:   list              # [(fecha, monto)], montos sin ajuste
    residual:  float             # capital que falta amortizar
    devengado: float             # interés corrido del cupón en curso
    dias_prox: Optional[int]     # plazo del cupón en curso, del calendario


class Flujo(NamedTuple):
    """Una fila de instrument_flows. Los montos van SIN AJUSTE, por cada 100 de
    nominal ORIGINAL: en un CER el capital figura como 20, no como 20 por el
    coeficiente, y el ajuste lo aplica el motor. `dias` es el plazo del cupón,
    que es lo que permite devengar sin adivinar la convención de cada bono."""
    fecha:        date
    interes:      Optional[float]
    amortizacion: Optional[float]
    total:        Optional[float]
    dias:         Optional[int]


@dataclass
class Pata:
    vpv:    float                                   # ARS, base 100, al vencimiento
    tem:    Optional[float] = None                  # TEM implícita (decimal)
    driver: Optional[float] = None                  # variable que maneja la pata
    vt:     Optional[float] = None                  # valor técnico devengado a hoy
    params: dict = field(default_factory=dict)      # desglose libre -> valuations.params

    # ── TIR en la convención NATIVA de la pata ──
    # Cada pata se quotea en la unidad que le es propia: una TAMAR en pesos, una
    # CER en tasa real, una dólar-linked en dólares. Es como lo muestran las
    # terminales (1816 lista "TXMD8 @CER 6,29%" y "TXMD8 @TAMAR 38,16%" para el
    # mismo bono), y es lo que hace comparable cada pata contra su propia curva.
    #
    # El cálculo es siempre el mismo: (base_nativa / precio)^(365/días) - 1.
    # Lo que cambia es la base, y con eso la unidad del resultado:
    #   nominal_ars -> base = vpv                      (pago nominal en pesos)
    #   real_cer    -> base = vt * (1+tem)^meses_rest  (pago deflactado por CER)
    #   usd         -> base = 100*fx * (1+spr)^m_rest  (pago en dólares, a spot)
    conv:         str = "nominal_ars"
    base_nativa:  Optional[float] = None

    # ── Bonos con cupones ──
    # El resto de la clase asume pago único al vencimiento: `vpv` es ese pago y
    # la TIR sale de (vpv/precio)^(365/días). Un bono que amortiza o paga renta
    # antes no entra en esa cuenta. Cuando la pata trae `flujos` —[(fecha, monto
    # en la unidad de `conv`), ...]— la TIR se resuelve por XIRR sobre ellos y
    # la duration es la de Macaulay de verdad, no el plazo al vencimiento.
    # `vpv` sigue existiendo para comparar patas entre sí en los duales, que son
    # todos bullet.
    flujos: Optional[list] = None
    # Pesos por unidad de `conv`, para pasar el precio de mercado —que viene en
    # pesos— a la unidad del vector: el coeficiente CER vigente, el A3500, el
    # MEP. None = el vector ya está en pesos. Sin esto la TIR de un bono en
    # dólares saldría comparando pesos contra dólares.
    fx_nativa: Optional[float] = None
    # Columnas de `prices` que YA vienen en la unidad de `conv`, si existen. Un
    # hard dollar cotiza en dólares de verdad (ticker D/C) y ese precio es el
    # bueno: convertir el precio en pesos al MEP es una aproximación, y en varios
    # ON `price_ars` está viejo o directamente en NULL. Si ninguna trae dato se
    # cae al precio en pesos dividido `fx_nativa`.
    precio_cols: Optional[list] = None


# ════════════════════════════ helpers ════════════════════════════
def dias360(d1: date, d2: date) -> int:
    """30/360 US, idéntico a Excel DIAS360(d1, d2)."""
    a, b = d1.day, d2.day
    if a == 31:
        a = 30
    if b == 31 and a == 30:
        b = 30
    return (d2.year - d1.year) * 360 + (d2.month - d1.month) * 30 + (b - a)


def meses360(d1: date, d2: date) -> float:
    """Exponente (DÍAS/360)*12 de la fórmula del prospecto."""
    return dias360(d1, d2) / 30.0


def _yf(d0: date, d1: date) -> float:
    """Años actual/365 entre dos fechas (criterio XIRR)."""
    return (d1 - d0).days / 365.0


def xirr(flujos, guess: float = 0.10) -> Optional[float]:
    """TIR de un vector [(fecha, monto)] con el precio ya incluido en negativo.

    Newton con caída a bisección: Newton solo no alcanza porque con paridades
    muy bajas la derivada se achata y diverge. El barrido de signos cubre desde
    -90% hasta 1000%, que es el rango en el que aparecen las ON argentinas.
    """
    fl = sorted(flujos, key=lambda x: x[0])
    if len(fl) < 2:
        return None
    d0 = fl[0][0]

    def f(r):
        one = 1.0 + r
        if one <= 0:
            return None
        return sum(c / one ** _yf(d0, d) for d, c in fl)

    r = guess
    for _ in range(80):
        one = 1.0 + r
        if one <= 0:
            break
        v = sum(c / one ** _yf(d0, d) for d, c in fl)
        dv = sum(c * (-_yf(d0, d)) * one ** (-_yf(d0, d) - 1) for d, c in fl)
        if not dv or abs(dv) < 1e-18:
            break
        rn = r - v / dv
        if rn <= -0.9999:
            break
        if abs(rn - r) < 1e-12:
            return rn
        r = rn

    lo = hi = None
    prev_x = prev_y = None
    for x in (-0.9, -0.5, -0.1, 0.0, 0.02, 0.05, 0.10, 0.20, 0.40, 0.8, 1.5, 3.0, 10.0):
        y = f(x)
        if y is None:
            continue
        if prev_y is not None and prev_y * y <= 0:
            lo, hi = prev_x, x
            break
        prev_x, prev_y = x, y
    if lo is None:
        return None
    flo = f(lo)
    for _ in range(200):
        m = 0.5 * (lo + hi)
        fm = f(m)
        if fm is None:
            return None
        if abs(fm) < 1e-10 or (hi - lo) < 1e-12:
            return m
        if flo * fm <= 0:
            hi = m
        else:
            lo, flo = m, fm
    return 0.5 * (lo + hi)


def macaulay(flujos, r: float, desde: date) -> Optional[float]:
    """Duration de Macaulay en años sobre los flujos FUTUROS (sin el precio).

    `desde` es la LIQUIDACIÓN, y es obligatoria: tomar como origen el primer
    flujo del vector —que es lo intuitivo— mide los plazos desde el próximo
    cupón y no desde hoy. A un bono de un solo pago le daba duration 0, y a DICP
    le restaba los 91 días que faltan para el cupón que viene.
    """
    if r is None or r <= -0.9999 or not flujos:
        return None
    pv = sum(c / (1 + r) ** _yf(desde, d) for d, c in flujos)
    if pv <= 0:
        return None
    return sum(_yf(desde, d) * (c / (1 + r) ** _yf(desde, d)) for d, c in flujos) / pv


def tamar_tem(tna: float, margen: float = 0.0) -> float:
    """TNA decimal -> TEM decimal.

    TAMAR_TEM = [(1 + (TAMAR + margen)/(365/32))^(365/32)]^(1/12) - 1

    El margen va DENTRO, sumado a la TNA antes de convertir (Res. Conj. 4/2025
    art. 1 y Res. Conj. 32/2026 art. 3-5: la fórmula dice literalmente
    "TAMAR + 3%" en el numerador). tamar.py convierte cada uno por separado y
    suma las TEM resultantes, que da 0,02-0,06 bps de más — despreciable en
    pesos, pero acá se hace como dice el contrato."""
    return ((1 + (tna + margen) / (365 / 32)) ** (365 / 32)) ** (1 / 12) - 1


def tamar_tna(tem: float, margen: float = 0.0) -> float:
    """Inversa de tamar_tem: dada una TEM objetivo, la TNA que la produce.
    Sirve para resolver breakevens en forma cerrada en vez de por bisección."""
    return (((1 + tem) ** 12) ** (32 / 365) - 1) * (365 / 32) - margen


def _pick(row: dict, candidatos):
    for c in candidatos:
        if c in row and row[c] is not None:
            return row[c]
    return None


def cargar_serie(nombre: str) -> dict:
    """{date: valor} de `series`, paginando.

    PostgREST corta en 1.000 filas por defecto y no avisa: devuelve las primeras
    1.000 del orden pedido y listo. Con la serie de CER en 1.100+ datos eso hacía
    que el último dato "observado" fuera de mayo, y la TIR real salía ~275 bps
    abajo sin ningún error a la vista. Cualquier lectura de una tabla que pueda
    crecer tiene que paginar."""
    out, desde = {}, 0
    while True:
        d = (sb.table("series").select("fecha, valor").eq("serie", nombre)
               .order("fecha").range(desde, desde + 999).execute().data or [])
        for r in d:
            v = _f(r.get("valor"))
            if v is None:
                continue
            try:
                out[date.fromisoformat(str(r["fecha"])[:10])] = v
            except ValueError:
                pass
        if len(d) < 1000:
            return out
        desde += 1000


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ════════════════════════════ contexto ════════════════════════════
class Ctx:
    """Datos compartidos por los motores. Carga perezosa: si no hay ninguna pata
    TAMAR que valuar, no se pega el viaje a la tabla de series."""

    def __init__(self, hoy: date):
        self.hoy = hoy
        self._feriados = None
        self._tamar = None
        self._badlar = None
        self._cer = None
        self._rem = {}
        self._fx = None
        self._flujos = None
        self._mep = None

    @property
    def flujos(self) -> dict:
        """{symbol: [Flujo, ...]} de instrument_flows, ordenado por fecha.

        Se pagina igual que `series`: PostgREST corta en 1.000 y no avisa, y la
        tabla ya pasa las 2.900 filas. Un calendario truncado no da error, da una
        TIR alta y creíble."""
        if self._flujos is None:
            out, desde = {}, 0
            while True:
                d = (sb.table("instrument_flows")
                       .select("symbol, fecha_pago, interes, amortizacion, total, dias")
                       .order("fecha_pago")
                       .range(desde, desde + 999).execute().data or [])
                for r in d:
                    try:
                        f = date.fromisoformat(str(r["fecha_pago"])[:10])
                    except (ValueError, TypeError):
                        continue
                    dias = _f(r.get("dias"))
                    out.setdefault(r["symbol"], []).append(Flujo(
                        f, _f(r.get("interes")), _f(r.get("amortizacion")),
                        _f(r.get("total")), int(dias) if dias else None))
                if len(d) < 1000:
                    break
                desde += 1000
            for v in out.values():
                v.sort(key=lambda x: x.fecha)
            self._flujos = out
        return self._flujos

    @property
    def fx_mep(self) -> float:
        """MEP, el dólar al que se liquida un bono hard dollar. precios2.py lo
        calcula como AL30/AL30D y lo escribe en prices.fx_mep de TODAS las filas,
        así que cualquiera sirve; se toma la más reciente.

        No es el A3500 de `fx_spot`: un dólar linked paga pesos atados al
        oficial, un hard dollar paga dólares de verdad. Usar uno por el otro
        mueve la TIR varios puntos."""
        if self._mep is None:
            d = (sb.table("prices").select("symbol, fx_mep, ts")
                   .not_.is_("fx_mep", "null")
                   .order("ts", desc=True).limit(1).execute().data or [])
            v = _f(d[0].get("fx_mep")) if d else None
            if not v or v <= 0:
                raise ValueError("sin fx_mep en prices (lo escribe precios2.py)")
            self._mep = v
        return self._mep

    @property
    def feriados(self) -> set:
        if self._feriados is None:
            # OJO: la columna se llama holiday_date. tamar.py la busca como
            # 'fecha'/'date'/... y por eso su set de feriados sale siempre vacío,
            # lo que le corre la ventana de "10 días hábiles".
            res = sb.table("holidays").select("holiday_date").execute()
            self._feriados = {
                date.fromisoformat(str(r["holiday_date"])[:10])
                for r in (res.data or []) if r.get("holiday_date")
            }
        return self._feriados

    @property
    def tamar(self) -> dict:
        """{date: TNA decimal}. En `series` la TAMAR va cruda en % (23.25)."""
        if self._tamar is None:
            self._tamar = {f: v / 100 for f, v in cargar_serie("tamar_tna").items()}
        return self._tamar

    @property
    def badlar(self) -> dict:
        """{date: TNA decimal}. Igual que la TAMAR, en `series` va cruda en %.
        Es la BADLAR de bancos privados en pesos, TNA (id 7 del BCRA), que es la
        que citan los prospectos; el BCRA publica además la efectiva anual y una
        variante que mete a los bancos públicos."""
        if self._badlar is None:
            self._badlar = {f: v / 100 for f, v in cargar_serie("badlar").items()}
        return self._badlar

    @property
    def cer(self) -> dict:
        """{date: coeficiente CER}. La sincroniza series_sync.py desde el BCRA."""
        if self._cer is None:
            self._cer = cargar_serie("cer")
        return self._cer

    @property
    def fx_spot(self) -> float:
        """A3500 spot. Lo mantiene dlk.py en prices bajo el símbolo 'UST',
        tomándolo de MAE. Cacheado: la bisección del breakeven llama al motor
        DLK cien veces y no puede pegarle a la DB en cada iteración."""
        if self._fx is None:
            row = cargar_precios(["UST"]).get("UST") or {}
            v = _f(_pick(row, ["last", "price_ars", "closing_price"]))
            if not v:
                raise ValueError("sin FX spot en prices.UST (lo mantiene dlk.py)")
            self._fx = v
        return self._fx

    @property
    def fecha_liq(self) -> date:
        """Liquidación T+1 hábil. Es la fecha de valuación real: el CER y el FX
        aplicables se cuentan desde acá, no desde hoy."""
        return self.habil_siguiente(self.hoy)

    def cer_en(self, d: date):
        """CER de una fecha. Si cae en un día sin dato (fin de semana/feriado),
        toma el último anterior publicado."""
        serie = self.cer
        if d in serie:
            return serie[d]
        previas = [x for x in serie if x <= d]
        return serie[max(previas)] if previas else None

    def rem_raw(self, variable: str, percentil: str = "mediana") -> dict:
        """Datos crudos del último informe del REM para una variable, tal como
        vienen: {"mensual": {(año,mes): valor}, "anual": {año: valor}, ...}.

        Sin interpretar: el IPC viene en % y el tipo de cambio en $/USD, así que
        la conversión a senda de tasas depende de la variable y la hace quien
        llama. Las filas 'horizonte' (próx. 12/24 meses) se descartan: son
        ventanas móviles que no anclan a un mes de calendario.
        """
        key = ("raw", variable, percentil)
        if key in self._rem:
            return self._rem[key]

        res = (sb.table("rem").select("*")
                 .eq("variable", variable).order("fecha_rem", desc=True).execute())
        filas = res.data or []
        if not filas:
            raise ValueError(f"sin datos de REM para '{variable}'")
        fecha_rem = max(f["fecha_rem"] for f in filas)
        filas = [f for f in filas if f["fecha_rem"] == fecha_rem]

        col = "mediana" if percentil in ("mediana", "p50") else percentil
        mensual, anual = {}, {}
        for f in filas:
            v = _f(f.get(col))
            if v is None or not f.get("fecha_ref"):
                continue
            fr = date.fromisoformat(str(f["fecha_ref"])[:10])
            if f["tipo"] == "mensual":
                mensual[(fr.year, fr.month)] = v
            elif f["tipo"] == "anual":
                anual[fr.year] = v

        out = {"mensual": mensual, "anual": anual, "fecha_rem": fecha_rem,
               "percentil": percentil}
        self._rem[key] = out
        return out

    def rem_inflacion(self, percentil: str = "mediana") -> dict:
        """Senda mensual de inflación del REM. {(año,mes): tasa_decimal}.

        El IPC viene en % (1.95 = 1,95% mensual para las filas mensuales,
        var. % i.a. para las anuales). Los meses sin dato mensual explícito se
        completan con la cifra anual del año, pasada a equivalente mensual.
        """
        key = ("infl", percentil)
        if key in self._rem:
            return self._rem[key]
        r = self.rem_raw("ipc", percentil)
        senda = {k: v / 100 for k, v in r["mensual"].items()}
        anual = {y: v / 100 for y, v in r["anual"].items()}
        if anual:
            for y, a in anual.items():
                m_eq = (1 + a) ** (1 / 12) - 1
                for m in range(1, 13):
                    senda.setdefault((y, m), m_eq)
            hasta = date(max(anual), 12, 31)
        else:
            ult = max(senda)
            hasta = date(ult[0], ult[1], 28)
        out = {"mensual": senda, "fecha_rem": r["fecha_rem"], "hasta": hasta,
               "percentil": percentil}
        self._rem[key] = out
        return out

    def rem_devaluacion(self, percentil: str = "mediana") -> dict:
        """Senda mensual de devaluación del REM. {(año,mes): tasa_decimal}.

        El tipo de cambio viene en NIVELES ($/USD), no en tasas, así que hay que
        derivar la variación mes contra mes. Para los años que sólo tienen cifra
        anual se reparte parejo entre el último nivel conocido y el de dic.
        """
        key = ("deval", percentil)
        if key in self._rem:
            return self._rem[key]
        r = self.rem_raw("tcn", percentil)
        niveles = dict(r["mensual"])
        for y, v in r["anual"].items():
            niveles.setdefault((y, 12), v)      # 'anual' = nivel a dic de ese año
        if not niveles:
            raise ValueError("REM sin niveles de tipo de cambio")

        ordenados = sorted(niveles)
        senda = {}
        for (y0, m0), (y1, m1) in zip(ordenados, ordenados[1:]):
            n = (y1 - y0) * 12 + (m1 - m0)      # meses entre anclas
            if n <= 0:
                continue
            tasa = (niveles[(y1, m1)] / niveles[(y0, m0)]) ** (1 / n) - 1
            for k in range(1, n + 1):
                mm = m0 + k
                senda[(y0 + (mm - 1) // 12, (mm - 1) % 12 + 1)] = tasa
        ult = ordenados[-1]
        out = {"mensual": senda, "fecha_rem": r["fecha_rem"],
               "hasta": date(ult[0], ult[1], 28), "percentil": percentil,
               "niveles": niveles}
        self._rem[key] = out
        return out

    # ── días hábiles ──
    def es_habil(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.feriados

    def habil_anterior(self, d: date, n: int) -> date:
        c = 0
        while c < n:
            d -= timedelta(days=1)
            if self.es_habil(d):
                c += 1
        return d

    def habil_siguiente(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.es_habil(d):
            d += timedelta(days=1)
        return d

    def rango_habiles(self, ini: date, fin: date):
        out, d = [], ini
        while d <= fin:
            if self.es_habil(d):
                out.append(d)
            d += timedelta(days=1)
        return out


# ════════════════════════════ motores ════════════════════════════
def calendario(ctx: Ctx, sym: str, desde: date):
    """Lo que queda por cobrar de `sym` según instrument_flows, visto desde
    `desde` (la liquidación: un cupón que paga ese mismo día no se cobra).

        -> (futuros, vn_residual, devengado)   o None si no hay calendario

    `futuros` es [(fecha, monto)] con los montos SIN AJUSTE; `vn_residual` es el
    capital que falta amortizar y `devengado` el interés corrido del cupón en
    curso. Las tres cosas en la misma unidad: por 100 de nominal original.

    El devengado se prorratea con el `dias` del propio cupón, que es el plazo con
    el que se calculó su `interes`. Así la convención sale del calendario y no
    hay que deducirla por bono —que es de dónde salen las diferencias de unos
    pocos bps contra el bróker.
    """
    todos = ctx.flujos.get(sym) or []
    if not todos:
        return None
    futuros = [(f.fecha, f.total) for f in todos if f.fecha > desde and f.total]
    if not futuros:
        return None
    vn_res = sum(f.amortizacion or 0.0 for f in todos if f.fecha > desde)

    prox = next((f for f in todos if f.fecha > desde), None)
    devengado = 0.0
    if prox is not None and prox.interes and prox.dias:
        corridos = prox.dias - (prox.fecha - desde).days
        devengado = prox.interes * min(1.0, max(0.0, corridos / prox.dias))
    return Calendario(futuros, vn_res, devengado,
                      prox.dias if prox is not None else None)


def es_bullet(cal: Calendario, inst: dict, capital: float = 100.0) -> bool:
    """¿Vale la forma cerrada para este bono?

    Sólo si capitaliza TODO desde la emisión y paga una vez al vencimiento. La
    forma cerrada hace 100·(1+tasa)^(vida entera), y eso exige tres cosas:

      1. un único pago pendiente, al vencimiento;
      2. el capital entero sin amortizar. A TX26 le queda un solo pago pero de 20
         de capital —amortizó el 80%— y valuarlo así lo multiplicaba por cinco.
         Se pide capital >= 100 y no == 100 porque si SOBRA es que el calendario
         está en otra unidad: TMVE8 trae el nominal en dólares ya pasado al tipo
         de cambio inicial, 149.983 por cada 100, y ahí la cerrada sigue valiendo;
      3. que ese pago devengue desde la EMISIÓN. Un bono de renta trimestral al
         que le queda el último cupón cumple 1 y 2, pero los cupones anteriores
         los PAGÓ en vez de capitalizarlos: capitalizar la vida entera le inventa
         plata que ya salió del bono. A RC3CO le daba un valor técnico de 125,63
         contra un precio de 101 y una TIR del 292%.

    La condición 3 sale del `dias` del cupón, no de la ficha: es el plazo con el
    que se calculó ese pago, así que si cubre la vida del bono es porque no hubo
    pagos antes.
    """
    if len(cal.futuros) != 1 or cal.futuros[0][0] < inst["_vencimiento"]:
        return False
    if cal.residual < capital - 1e-6:
        return False
    vida = (inst["_vencimiento"] - inst["_emision"]).days
    if cal.dias_prox is None:
        return vida <= 0
    return cal.dias_prox >= vida * 0.95


def motor_fija(ctx: Ctx, inst: dict, p: dict, esc: dict, driver=None) -> Pata:
    """Tasa fija efectiva mensual capitalizable hasta el vencimiento.
       VPV = 100 * (1 + Tm) ^ ((DÍAS/360)*12)      params: {"tem": 0.0217}
       No tiene driver: es determinística, siempre es el lado 'target' del breakeven."""
    emi, vto = inst["_emision"], inst["_vencimiento"]
    base = 100 * (_f(p.get("fx_base"), 1.0) or 1.0)

    # Con calendario el cupón no se deduce de nada: los montos en pesos están
    # escritos y son ciertos. Es el caso de los corporativos y sub soberanos en
    # pesos a tasa fija, que antes quedaban sin valuar porque `tem` no alcanza
    # para describir un bono que amortiza.
    cal = calendario(ctx, inst["symbol"], ctx.fecha_liq)
    # La forma cerrada sólo tiene sentido con `tem`: describe una LECAP, que
    # capitaliza desde la emisión y paga todo junto. Sin `tem` —un bono que paga
    # renta, o una letra a descuento como LBN26— el calendario es el único
    # camino, y además es exacto: los montos en pesos están escritos.
    if cal and not (p.get("tem") is not None and es_bullet(cal, inst, base)):
        futuros, vn_res, dev = cal.futuros, cal.residual, cal.devengado
        return Pata(
            vpv=sum(c for _d, c in futuros) * (base / 100),
            tem=_f(p.get("tem")), driver=None,
            vt=(vn_res + dev) * (base / 100),
            conv="nominal_ars", flujos=[(d, c * base / 100) for d, c in futuros],
            params={"n_pagos": len(futuros), "vn_residual": round(vn_res, 6),
                    "devengado": round(dev, 6), "base": round(base, 6)},
        )

    tem = _f(p.get("tem"))
    if tem is None:
        raise ValueError("pata FIJA sin params.tem ni calendario de pagos")
    return Pata(
        vpv=base * (1 + tem) ** meses360(emi, vto),
        tem=tem,
        driver=None,
        vt=base * (1 + tem) ** max(0.0, meses360(emi, ctx.hoy)),
        params={"tem_fija": round(tem, 8), "base": round(base, 6)},
    )


def _tna_futura(ctx: Ctx, serie: dict, clave: str, esc: dict, driver) -> tuple:
    """TNA con la que se proyecta el tramo de la serie que todavía no se publicó."""
    if driver is not None:
        return float(driver), "driver"
    if esc.get(clave) is not None:
        return float(esc[clave]), "escenario"
    recientes = sorted(d for d in serie if d <= ctx.hoy)[-N_PROY:]
    if not recientes:
        raise ValueError(f"sin {clave} observada")
    return sum(serie[d] for d in recientes) / len(recientes), f"prom. últimos {N_PROY}"


def _tamar_tna_futura(ctx: Ctx, esc: dict, driver) -> tuple:
    return _tna_futura(ctx, ctx.tamar, "tamar_tna", esc, driver)


def _flotante_con_cupones(ctx: Ctx, inst: dict, cal, serie: dict, margen: float,
                          base: float, tna_fut: float, origen: str) -> Pata:
    """Bono a tasa variable que amortiza o paga renta antes del vencimiento.

    Acá no hay una sola ventana: cada cupón tiene la suya, [inicio-10h ; pago-10h],
    y la forma cerrada del bullet no sirve. El cupón del período EN CURSO ya está
    fijado —se determinó al arrancar el período— así que se toma el `interes` del
    calendario tal cual. Los períodos que todavía no empezaron se recalculan con
    la serie y, para el tramo sin publicar, con la TNA proyectada.

    El devengamiento es TNA · días/365 sobre el capital residual, que es la
    convención con la que el cargador armó el calendario (se verificó contra el
    `interes` de los cupones ya fijados).

    Sirve igual para TAMAR y para BADLAR: lo único que cambia es la serie y de
    dónde sale la TNA proyectada.
    """
    futuros, vn_res, dev = cal.futuros, cal.residual, cal.devengado
    porf = {f.fecha: f for f in (ctx.flujos.get(inst["symbol"]) or [])}

    # Capital residual durante cada período: el que todavía no amortizó, contando
    # el pago del propio período.
    resid, acum = {}, 0.0
    for f_, _c in reversed(futuros):
        acum += (porf[f_].amortizacion or 0.0) if f_ in porf else 0.0
        resid[f_] = acum

    vector, n_fijos, n_proy = [], 0, 0
    for f_, monto in futuros:
        fl = porf.get(f_)
        if fl is None or not fl.dias:
            vector.append((f_, monto)); n_fijos += 1
            continue
        ini = f_ - timedelta(days=fl.dias)
        if ini <= ctx.hoy:
            vector.append((f_, monto)); n_fijos += 1    # cupón ya fijado
            continue
        ventana = ctx.rango_habiles(ctx.habil_anterior(ini, 10),
                                    ctx.habil_anterior(f_, 10))
        obs = [serie[d] for d in ventana if d in serie and d <= ctx.hoy]
        n_f = sum(1 for d in ventana if d > ctx.hoy)
        n_t = len(obs) + n_f
        tna = ((sum(obs) + tna_fut * n_f) / n_t) if n_t else tna_fut
        interes = resid[f_] * (tna + margen) * fl.dias / 365
        vector.append((f_, interes + (fl.amortizacion or 0.0)))
        n_proy += 1

    k = base / 100
    return Pata(
        vpv=sum(c for _d, c in vector) * k,
        tem=None, driver=tna_fut,
        vt=(vn_res + dev) * k,
        conv="nominal_ars", flujos=[(d, c * k) for d, c in vector],
        params={"n_pagos": len(vector), "cupones_fijados": n_fijos,
                "cupones_proyectados": n_proy, "tna_proy": round(tna_fut, 8),
                "margen": round(margen, 8), "vn_residual": round(vn_res, 6),
                "devengado": round(dev, 6), "base": round(base, 6),
                "origen_proy": origen},
    )


def motor_tamar(ctx: Ctx, inst: dict, p: dict, esc: dict, driver=None) -> Pata:
    """TAMAR promedio de la ventana [emisión-10h ; vto-10h] + margen.

    El prospecto define la TAMAR como el promedio aritmético simple de las TNA
    publicadas por el BCRA en esa ventana, y recién ese promedio se pasa a TEM.
    El tramo que todavía no se publicó se proyecta con `driver` (si viene), con
    el escenario, o con el promedio de los últimos N_PROY datos observados.

    driver = TNA asumida para el tramo NO observado. Para un dual recién emitido
    es el promedio de toda la ventana; para uno con vida corrida es lo único que
    queda libre, que es justamente lo que hay que despejar en el breakeven.

    params: {"margen": 0.065}   (0 en los duales, que no llevan margen)
    """
    margen = _f(p.get("margen"), 0.0) or 0.0
    # Nominal en dólares (TMVE8): la pata TAMAR devenga sobre el VN convertido a
    # pesos al TIPO DE CAMBIO INICIAL, que queda fijo desde la emisión.
    base = 100 * (_f(p.get("fx_base"), 1.0) or 1.0)
    emi, vto = inst["_emision"], inst["_vencimiento"]
    serie = ctx.tamar

    cal = calendario(ctx, inst["symbol"], ctx.fecha_liq)
    if cal and p.get("fx_base") is None and len(cal.futuros) == 1 and cal.futuros[0][0] >= vto:
        # Bullet con nominal en dólares (TMVE8): el capital del calendario ya
        # viene convertido al tipo de cambio inicial, así que ESE es el 100 de
        # esta pata. Deducirlo de acá evita tener que cargar el fx_base a mano
        # por bono.
        base = cal.residual or base
    if cal and not es_bullet(cal, inst, base):
        tna_fut, origen = _tamar_tna_futura(ctx, esc, driver)
        return _flotante_con_cupones(ctx, inst, cal, serie, margen, base,
                                     tna_fut, origen)

    ventana = ctx.rango_habiles(ctx.habil_anterior(emi, 10), ctx.habil_anterior(vto, 10))
    if not ventana:
        raise ValueError("ventana de TAMAR vacía")

    # Un día hábil de la ventana sin dato en la serie NO es futuro: es un día en
    # el que el BCRA no publicó (feriado que falta en `holidays`, o un bache del
    # sync). Contarlo como proyectado lo valuaba a la TNA futura y arrastraba el
    # promedio: el 21-08-2026 eso le sacaba 13 puntos de TEA a TTS26, que tenía
    # 16 feriados de 2025 sin cargar dentro de su ventana.
    #
    # El prospecto promedia las TNA PUBLICADAS, así que un día sin publicación no
    # va ni en el numerador ni en el denominador.
    obs = [serie[d] for d in ventana if d in serie and d <= ctx.hoy]
    fut = [d for d in ventana if d > ctx.hoy]
    n_obs, n_proy = len(obs), len(fut)
    n_tot = n_obs + n_proy
    n_sin_dato = len(ventana) - n_tot

    tna_fut, origen = _tamar_tna_futura(ctx, esc, driver)

    # Promedio simple sobre TODA la ventana, y recién ahí a TEM (prospecto).
    tna_ventana = (sum(obs) + tna_fut * n_proy) / n_tot
    tem = tamar_tem(tna_ventana, margen)

    return Pata(
        vpv=base * (1 + tem) ** meses360(emi, vto),
        tem=tem,
        driver=tna_fut,
        vt=base * (1 + tem) ** max(0.0, meses360(emi, ctx.hoy)),
        params={
            "tamar_obs":   round(sum(obs) / n_obs, 8) if obs else None,
            "tamar_proy":  round(tna_fut, 8),
            "tamar_vent":  round(tna_ventana, 8),
            "tem_sin_margen": round(tamar_tem(tna_ventana), 8),
            "margen":      round(margen, 8),
            "base":        round(base, 6),
            "n_obs":       n_obs,
            "n_proy":      n_proy,
            "pct_obs":     round(n_obs / n_tot, 6),
            # Días hábiles de la ventana sin publicación. >0 esperable por
            # feriados; si crece de golpe, mirar el sync de la serie.
            "n_sin_dato":  n_sin_dato,
            "ventana":     [str(ventana[0]), str(ventana[-1])],
            "origen_proy": origen,
        },
    )


def _dias_mes(y: int, m: int) -> int:
    return (date(y + (m == 12), (m % 12) + 1, 1) - date(y, m, 1)).days


def capitalizar(desde: date, hasta: date, senda: dict, fallback: float):
    """Factor de inflación acumulada entre dos fechas, aplicando la tasa mensual
    de `senda` prorrateada por días dentro de cada mes. Los meses que faltan en
    la senda usan `fallback`. Devuelve (factor, meses_extrapolados)."""
    if hasta <= desde:
        return 1.0, 0
    factor, extrap, d = 1.0, 0, desde
    while d < hasta:
        fin_mes = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
        tramo = min(fin_mes, hasta)
        peso = (tramo - d).days / _dias_mes(d.year, d.month)
        tasa = senda.get((d.year, d.month))
        if tasa is None:
            tasa, extrap = fallback, extrap + 1
        factor *= (1 + tasa) ** peso
        d = tramo
    return factor, extrap


def motor_cer(ctx: Ctx, inst: dict, p: dict, esc: dict, driver=None) -> Pata:
    """Capital ajustado por CER entre 10 días hábiles antes de la emisión y 10
    días hábiles antes del vencimiento (Res. Conj. 32/2026 art. 3-5, punto i).

        VPV = 100 * CER(vto-10h) / CER(emisión-10h) * (1 + tem)^meses

    El tramo de CER que todavía no se publicó se proyecta con la senda de
    inflación del REM; más allá del horizonte del REM se mantiene plana la
    última cifra anual y se deja constancia en params.

    driver = inflación mensual asumida para TODO el tramo no observado. Es lo
    que se despeja en el breakeven.

    params: {"tem": 0.02}  cupón real sobre el CER, opcional (0 en los duales
            CER/TAMAR, que ajustan capital y no devengan interés adicional)
            {"cer_base": 123.45}  pisa el CER de emisión si hace falta
    """
    emi, vto = inst["_emision"], inst["_vencimiento"]
    tem = _f(p.get("tem"), 0.0) or 0.0

    f0, f1 = ctx.habil_anterior(emi, 10), ctx.habil_anterior(vto, 10)
    cer0 = _f(p.get("cer_base")) or _f(inst.get("cer_emision")) or ctx.cer_en(f0)
    if not cer0:
        raise ValueError(f"sin CER base (ni params, ni cer_emision, ni serie en {f0})")

    serie = ctx.cer
    ult_obs = max(d for d in serie if d <= ctx.hoy)
    cer_ult = serie[ult_obs]

    if f1 <= ult_obs:
        # Ventana cerrada: el CER final ya está publicado, no se proyecta nada.
        cer1, extrap, origen, infl = ctx.cer_en(f1), 0, "observado", None
        rem_info = None
    else:
        if driver is not None:
            senda, fallback, origen = {}, float(driver), "driver"
            rem_info = None
        else:
            pct = esc.get("cer_percentil", "mediana")
            r = ctx.rem_inflacion(pct)
            senda = r["mensual"]
            # Más allá del horizonte del REM: se sostiene el último mes conocido.
            fallback = senda.get((r["hasta"].year, r["hasta"].month), 0.0)
            origen = f"REM {r['fecha_rem']} {pct}"
            rem_info = r
        factor, extrap = capitalizar(ult_obs, f1, senda, fallback)
        cer1 = cer_ult * factor
        infl = (factor ** (30 / max(1, (f1 - ult_obs).days))) - 1  # mensual equivalente

    meses = meses360(emi, vto)
    ajuste = cer1 / cer0

    # El CER aplicable a un pago es el de 10 días hábiles antes: el coeficiente
    # vigente para liquidar hoy arrastra ese rezago. Usar el de hoy sobrestima
    # la TIR ~30 bps.
    f_apl_cer = ctx.habil_anterior(ctx.fecha_liq, 10)
    cer_apl = ctx.cer_en(min(f_apl_cer, ult_obs))

    cal = calendario(ctx, inst["symbol"], ctx.fecha_liq)
    if cal and not es_bullet(cal, inst):
        # Bono CER que amortiza o paga renta. Los montos del calendario están
        # SIN ajustar, o sea expresados en pesos de la base CER del bono: eso es
        # exactamente la unidad en la que se quotea un CER, y descontarlos
        # contra el precio dividido por el coeficiente vigente da la TASA REAL
        # directo, sin proyectar inflación.
        #
        # El VPV nominal sí necesita proyección: cada pago se multiplica por el
        # CER estimado a su propia fecha de aplicación, no por el del último.
        futuros, vn_res, dev = cal.futuros, cal.residual, cal.devengado
        # La senda de proyección se arma acá y no se reusa la del tramo bullet:
        # si el vencimiento ya tiene CER publicado, ese tramo no la calcula, pero
        # un bono con cupones igual puede tener pagos más allá del último dato.
        if driver is not None:
            senda_c, fb_c, origen_c = {}, float(driver), "driver"
        else:
            pct_c = esc.get("cer_percentil", "mediana")
            r_c = ctx.rem_inflacion(pct_c)
            senda_c = r_c["mensual"]
            fb_c = senda_c.get((r_c["hasta"].year, r_c["hasta"].month), 0.0)
            origen_c = f"REM {r_c['fecha_rem']} {pct_c}"

        def _cer_proy(f: date) -> float:
            fa = ctx.habil_anterior(f, 10)
            if fa <= ult_obs:
                return ctx.cer_en(fa)
            fac, _ = capitalizar(ult_obs, fa, senda_c, fb_c)
            return cer_ult * fac
        vpv_nom = sum(c * _cer_proy(f) / cer0 for f, c in futuros)
        params_cup = {
            "cer_base": round(cer0, 8), "cer_ultimo": round(cer_ult, 8),
            "cer_ult_fecha": str(ult_obs),
            "cer_aplicable": round(cer_apl, 8),
            "cer_apl_fecha": str(min(f_apl_cer, ult_obs)),
            "n_pagos": len(futuros), "vn_residual": round(vn_res, 6),
            "devengado": round(dev, 6), "origen_proy": origen_c,
            "tem_cupon": round(tem, 8), "es_real": True,
        }
        return Pata(
            vpv=vpv_nom, tem=None, driver=None,
            vt=(vn_res + dev) * cer_apl / cer0,
            conv="real_cer", flujos=futuros, fx_nativa=cer_apl / cer0,
            params=params_cup,
        )

    vpv = 100 * ajuste * (1 + tem) ** meses

    # Valor técnico devengado, con el mismo coeficiente rezagado de arriba.
    meses_dev = max(0.0, meses360(emi, ctx.hoy))
    vt = 100 * (cer_apl / cer0) * (1 + tem) ** meses_dev

    params = {
        "cer_base":     round(cer0, 8),
        "cer_final":    round(cer1, 8),
        "cer_ultimo":   round(cer_ult, 8),
        "cer_ult_fecha": str(ult_obs),
        "ajuste":       round(ajuste, 8),
        "tem_cupon":    round(tem, 8),
        "ventana":      [str(f0), str(f1)],
        "origen_proy":  origen,
        "meses_extrapolados": extrap,
        # Marca para que el runner calcule además la TIR REAL (sobre CER), que
        # es la cotización estándar de estos bonos y no depende de la
        # proyección de inflación. Es la que devuelve cerv2.py en prices.ytm;
        # la ytm de esta tabla es NOMINAL en pesos, que es lo único comparable
        # contra una pata TAMAR. No son el mismo número.
        "es_real": True,
    }
    if infl is not None:
        params["infl_mens_impl"] = round(infl, 8)
    if rem_info:
        params["rem_fecha"] = str(rem_info["fecha_rem"])
        params["rem_hasta"] = str(rem_info["hasta"])
    params["cer_aplicable"] = round(cer_apl, 8)
    params["cer_apl_fecha"] = str(min(f_apl_cer, ult_obs))
    # La pata CER se quotea en TASA REAL: contra el valor técnico ya ajustado por
    # CER, el pago restante es sólo el devengamiento del cupón real (1 si es
    # zero-coupon, que es el caso de los duales CER/TAMAR).
    base_real = vt * (1 + tem) ** max(0.0, meses - meses_dev)
    return Pata(vpv=vpv, tem=None, driver=(infl if infl is not None else None),
                vt=vt, params=params, conv="real_cer", base_nativa=base_real)


def motor_dlk(ctx: Ctx, inst: dict, p: dict, esc: dict, driver=None) -> Pata:
    """Capital ajustado por el tipo de cambio A3500.

        VPV = 100 * FX(vto - 3 hábiles) * (1 + spread)^meses

    El "tipo de cambio aplicable" del prospecto es el A3500 del TERCER día hábil
    previo a la fecha de pago (Res. Conj. 46/2026 art. 3, y misma convención en
    los DLK del Tesoro). El FX futuro se proyecta con la senda de devaluación
    del REM a partir del spot; más allá del horizonte del REM se sostiene el
    último mes conocido y queda asentado en params.

    driver = A3500 asumido AL VENCIMIENTO, en $/USD. Es lo que se despeja en el
    breakeven, y es directamente comparable contra un futuro de ROFEX.

    params: {"spread": 0.0}  interés sobre el capital ajustado (0 en TMVE8, que
            es ajuste puro de capital sin devengamiento)

    OJO con la unidad: estos bonos están denominados en dólares, así que "100 de
    VN" son USD 100 y el VPV sale en pesos por VNO USD 100 (~150.000, no ~150).
    Es la misma base en la que precios2.py guarda price_ars para los DLK, así
    que la TIR y la paridad salen bien.
    """
    emi, vto = inst["_emision"], inst["_vencimiento"]
    spread = _f(p.get("spread"), 0.0) or 0.0

    fx_spot = ctx.fx_spot

    cal = calendario(ctx, inst["symbol"], ctx.fecha_liq)
    if cal and not es_bullet(cal, inst):
        # Dólar linked que amortiza o paga renta. Los montos del calendario son
        # el nominal en dólares de cada pago; se liquidan en pesos al A3500, no
        # al MEP. Descontarlos contra el precio pasado a dólares al A3500 da la
        # TIR en dólares, que es la convención del mercado para estos bonos y no
        # necesita proyectar devaluación.
        futuros, vn_res, dev = cal.futuros, cal.residual, cal.devengado
        return Pata(
            vpv=sum(c for _d, c in futuros) * fx_spot,
            tem=None, driver=None,
            vt=(vn_res + dev) * fx_spot,
            conv="usd", flujos=futuros, fx_nativa=fx_spot,
            base_nativa=sum(c for _d, c in futuros),
            params={"n_pagos": len(futuros), "vn_residual": round(vn_res, 6),
                    "devengado": round(dev, 6), "fx_spot": round(fx_spot, 6),
                    "spread": round(spread, 8)},
        )

    f_apl = ctx.habil_anterior(vto, 3)
    if f_apl <= ctx.hoy:
        fx1, extrap, origen = fx_spot, 0, "spot (vencido o ventana cerrada)"
    elif driver is not None:
        fx1, extrap, origen = float(driver), 0, "driver"
    elif esc.get("fx_vto") is not None:
        fx1, extrap, origen = float(esc["fx_vto"]), 0, "escenario"
    else:
        pct = esc.get("fx_percentil", esc.get("cer_percentil", "mediana"))
        r = ctx.rem_devaluacion(pct)
        senda = r["mensual"]
        fallback = senda.get((r["hasta"].year, r["hasta"].month), 0.0)
        factor, extrap = capitalizar(ctx.hoy, f_apl, senda, fallback)
        fx1 = fx_spot * factor
        origen = f"REM {r['fecha_rem']} {pct} sobre spot"

    meses = meses360(emi, vto)
    vpv = 100 * fx1 * (1 + spread) ** meses
    vt = 100 * fx_spot * (1 + spread) ** max(0.0, meses360(emi, ctx.hoy))

    # La pata dólar-linked se quotea en DÓLARES: pagás precio_ars/fx hoy y cobrás
    # VNO USD 100 al vencimiento. No requiere proyectar el tipo de cambio, por eso
    # es la convención que usa el mercado para estos bonos.
    base_usd = 100 * fx_spot * (1 + spread) ** meses
    return Pata(
        vpv=vpv, tem=None, driver=fx1, vt=vt,
        conv="usd", base_nativa=base_usd,
        params={
            "fx_spot":     round(fx_spot, 6),
            "fx_vto":      round(fx1, 6),
            "deval_impl":  round(fx1 / fx_spot - 1, 8),
            "spread":      round(spread, 8),
            "fecha_aplic": str(f_apl),
            "origen_proy": origen,
            "meses_extrapolados": extrap,
        },
    )


def motor_hd(ctx: Ctx, inst: dict, p: dict, esc: dict, driver=None) -> Pata:
    """Bono sin ajuste que paga en dólares: el flujo es cierto en USD.

    Es la familia más grande de la base —hard dollar soberanos y las ON en
    dólares— y hasta ahora la resolvía tir.py por fuera, con su propia carga de
    flujos y su propio XIRR. Acá entra al mismo contrato: `conv` queda en "usd",
    el vector va en dólares y `fx_nativa` es el MEP, así que la TIR sale en
    dólares contra el precio en pesos sin que el motor toque el precio.

    `vpv` queda en pesos como el resto de las patas, pero para un bono que
    amortiza es la suma sin descontar de lo que falta cobrar, no un pago único:
    sirve para elegir ganadora en un dual y para nada más. Ningún dual tiene
    pata HD hoy.
    """
    cal = calendario(ctx, inst["symbol"], ctx.fecha_liq)
    if not cal:
        raise ValueError("sin calendario de pagos")
    futuros, vn_res, dev = cal.futuros, cal.residual, cal.devengado
    fx = _f(esc.get("fx_mep")) or ctx.fx_mep
    return Pata(
        vpv=sum(c for _d, c in futuros) * fx,
        tem=None, driver=None,
        vt=(vn_res + dev) * fx,
        conv="usd", flujos=futuros, fx_nativa=fx,
        precio_cols=["last", "price_ars_usd"],
        base_nativa=sum(c for _d, c in futuros),
        params={"n_pagos": len(futuros), "vn_residual": round(vn_res, 6),
                "devengado": round(dev, 6), "fx_mep": round(fx, 4)},
    )


def motor_badlar(ctx: Ctx, inst: dict, p: dict, esc: dict, driver=None) -> Pata:
    """BADLAR de bancos privados + margen, sobre el calendario del bono.

    Misma mecánica que TAMAR: cada cupón se arma con el promedio de la serie en
    su propia ventana, el del período en curso ya está fijado y el tramo sin
    publicar se proyecta. La serie es la TNA de bancos privados en pesos (id 7
    del BCRA); el BCRA publica además la efectiva anual y una variante con
    bancos públicos, que no son las del prospecto.

    No tiene rama bullet: todo lo que paga BADLAR en la base amortiza o paga
    renta. Si algún día entra un bullet, hay que sumarle la forma cerrada como
    tiene motor_tamar.
    """
    cal = calendario(ctx, inst["symbol"], ctx.fecha_liq)
    if not cal:
        raise ValueError("sin calendario de pagos")
    margen = _f(p.get("margen"))
    if margen is None:
        margen = _f(inst.get("margen_ref"), 0.0) or 0.0
    tna_fut, origen = _tna_futura(ctx, ctx.badlar, "badlar_tna", esc, driver)
    return _flotante_con_cupones(ctx, inst, cal, ctx.badlar, margen, 100.0,
                                 tna_fut, origen)


MOTORES: dict[str, Callable[..., Pata]] = {
    "HD":     motor_hd,
    "BADLAR": motor_badlar,
    "FIJA":  motor_fija,
    "TAMAR": motor_tamar,
    "CER":   motor_cer,
    "DLK":   motor_dlk,
}

# Cómo se llama el driver de cada pata en el escenario. `leg_types` espeja esto,
# y lo que NO está acá es una pata sin driver: determinística, siempre el lado
# 'target' del breakeven.
DRIVER_NOMBRE: dict[str, str] = {
    "TAMAR":  "tamar_tna",
    "BADLAR": "badlar_tna",
    "CER":    "infl_mens",
    "DLK":    "fx_vto",
}

# Rango de bisección del driver de cada pata, en sus propias unidades.
DRIVER_BOUNDS: dict[str, tuple] = {
    # TNA de depósitos: no puede ser negativa. Con el piso en -0.99 la bisección
    # devolvía breakevens imposibles (-85,7% para TTS26) en vez de decir que la
    # opción ya está definida y no hay TAMAR futura que la dé vuelta.
    "TAMAR":  (0.0, 3.0),       # TNA decimal
    "BADLAR": (0.0, 3.0),       # TNA decimal
    "CER":   (-0.50, 5.0),      # inflación mensual decimal
    "DLK":   (1.0, 1_000_000),  # A3500 al vencimiento
}


# ════════════════════════════ breakeven ════════════════════════════
def breakeven(motor, ctx, inst, p, esc, objetivo: float, bounds) -> Optional[float]:
    """Valor del driver que hace que esta pata iguale a `objetivo` (el VPV de la
    pata rival). Bisección: sirve para cualquier motor monótono creciente en su
    driver, así que no hay que escribir un breakeven por familia de dual."""
    lo, hi = bounds

    def f(x):
        try:
            return motor(ctx, inst, p, esc, driver=x).vpv
        except Exception:
            return None

    v_lo, v_hi = f(lo), f(hi)
    if v_lo is None or v_hi is None:
        return None
    if not (v_lo <= objetivo <= v_hi):
        return None                     # inalcanzable dentro del rango
    for _ in range(100):
        mid = (lo + hi) / 2
        v = f(mid)
        if v is None:
            return None
        if v < objetivo:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ════════════════════════════ carga ════════════════════════════
def cargar_instrumentos(symbols=None) -> dict:
    q = sb.table("instruments").select("*").eq("is_active", True)
    if symbols:
        q = q.in_("symbol", symbols)
    out = {}
    for i in (q.execute().data or []):
        emi, vto = i.get("emision"), i.get("vencimiento")
        if not emi or not vto:
            continue
        try:
            i["_emision"] = date.fromisoformat(str(emi)[:10])
            i["_vencimiento"] = date.fromisoformat(str(vto)[:10])
        except ValueError:
            continue
        out[i["symbol"]] = i
    return out


def cargar_precios(symbols=None) -> dict:
    q = sb.table("prices").select("*")
    if symbols:
        q = q.in_("symbol", symbols)
    return {r["symbol"]: r for r in (q.execute().data or [])}


def cargar_escenario(sid: str) -> dict:
    res = sb.table("scenarios").select("supuestos").eq("id", sid).limit(1).execute()
    return ((res.data or [{}])[0].get("supuestos")) or {}


MARCA_DERIVADA = "_origen"          # en instrument_legs.params de las patas deducidas
ORIGEN_ESTRUCTURA = "estructura"


def cargar_patas(insts: dict, solo_manuales: bool = False) -> dict:
    """{symbol: [(leg, params), ...]}: lo cargado a mano manda, y lo que no está
    se deduce de la estructura de pagos del bono.

    La tabla existe para los casos que no se pueden deducir —un margen que no
    está en la ficha, un fx_base, una pata que el prospecto define raro— y
    siempre gana. Pero cargar a mano los 400 activos no escala y, peor, deja sin
    valuar a cada bono nuevo hasta que alguien se acuerde de insertarle las
    patas.

    Las deducidas se marcan con params["_origen"]="estructura" y se vuelven a
    deducir en cada corrida, así un cambio en la ficha se propaga. Las cargadas a
    mano no se tocan nunca. `sincronizar_patas` es lo que las escribe: tienen que
    estar en la tabla porque `valuations` tiene una foreign key contra ella.

    `solo_manuales=True` devuelve nada más lo curado a mano. Lo usa valuar_loop,
    que durante la rueda ya resuelve el resto del universo con su propio
    descuento de flujos: valuar los 400 ahí le multiplicaría por diez el trabajo
    de cada ciclo para reescribir lo mismo.
    """
    q = sb.table("instrument_legs").select("symbol, leg, params")
    if insts:
        q = q.in_("symbol", list(insts))
    tabla = {}
    for r in (q.execute().data or []):
        tabla.setdefault(r["symbol"], []).append((r["leg"], r.get("params") or {}))

    # Un símbolo es "manual" si alguna de sus patas NO está marcada como derivada.
    # Se mira por símbolo y no por pata: si alguien cargó a mano una de las dos
    # patas de un dual, rededucir la otra podría dejar un par incoherente.
    manual = {sym for sym, v in tabla.items()
              if any((p or {}).get(MARCA_DERIVADA) != ORIGEN_ESTRUCTURA for _lg, p in v)}
    if solo_manuales:
        return {sym: v for sym, v in tabla.items() if sym in manual}

    out = {}
    for sym, i in insts.items():
        out[sym] = tabla[sym] if sym in manual else patas_por_estructura(i)
    return out


def sincronizar_leg_types() -> int:
    """Da de alta en `leg_types` los tipos de pata que tiene MOTORES.

    `instrument_legs.leg` referencia esa tabla, así que un motor nuevo no puede
    escribir ni una pata hasta que su tipo exista. Antes eso era un INSERT a mano
    que nadie recordaba —HD y BADLAR hicieron rebotar la primera corrida— y ahora
    sale de MOTORES, que ya es la lista de la verdad.

    Sólo da de alta lo que falta. Las descripciones ya cargadas están escritas a
    mano y no se pisan; si el `driver` de una existente no coincide con el código
    se avisa y no se toca, porque eso no es un alta que falte sino una
    contradicción que alguien tiene que mirar.
    """
    actuales = {r["leg"]: r for r in
                (sb.table("leg_types").select("leg, descripcion, driver").execute().data or [])}
    filas = []
    for leg, fn in MOTORES.items():
        esperado = DRIVER_NOMBRE.get(leg)
        if leg in actuales:
            if (actuales[leg].get("driver") or None) != esperado:
                print(f"[LEGS] ojo: leg_types.{leg}.driver = "
                      f"{actuales[leg].get('driver')!r} y el código espera {esperado!r}")
            continue
        doc = (fn.__doc__ or "").strip().splitlines()
        filas.append({"leg": leg,
                      "descripcion": (doc[0].strip() if doc else leg)[:200],
                      "driver": esperado})
    if filas:
        sb.table("leg_types").insert(filas).execute()
        print(f"[LEGS] alta en leg_types: {', '.join(f['leg'] for f in filas)}")
    return len(filas)


def sincronizar_patas(insts: dict, patas: dict) -> tuple:
    """Deja en instrument_legs las patas deducidas, y borra las que sobraron.

    Hace falta porque `valuations` referencia (symbol, leg) contra esta tabla:
    sin la fila, el upsert de la valuación rebota con violación de foreign key.
    Escribirlas además las hace auditables —se ve qué dedujo el motor— y
    editables: si alguien le saca la marca y le corrige un parámetro, la corrida
    siguiente respeta lo que cargó.

    Devuelve (altas_o_cambios, borradas).
    """
    actuales = {}
    for r in (sb.table("instrument_legs").select("symbol, leg, params").execute().data or []):
        actuales[(r["symbol"], r["leg"])] = r.get("params") or {}
    manual = {sym for (sym, _lg), p in actuales.items()
              if p.get(MARCA_DERIVADA) != ORIGEN_ESTRUCTURA}

    quiero, filas = set(), []
    for sym, legs in patas.items():
        if sym in manual:
            continue
        for leg, p in legs:
            quiero.add((sym, leg))
            nuevo = dict(p or {})
            nuevo[MARCA_DERIVADA] = ORIGEN_ESTRUCTURA
            if actuales.get((sym, leg)) != nuevo:
                filas.append({"symbol": sym, "leg": leg, "params": nuevo})

    # Sobrantes: derivadas que ya no corresponden (cambió la ficha, o el bono
    # salió de activos). Las manuales no se tocan.
    sobran = [k for k, p in actuales.items()
              if p.get(MARCA_DERIVADA) == ORIGEN_ESTRUCTURA and k not in quiero
              and k[0] in insts]

    for i in range(0, len(filas), 500):
        sb.table("instrument_legs").upsert(filas[i:i + 500]).execute()
    for sym, leg in sobran:
        sb.table("valuations").delete().eq("symbol", sym).eq("leg", leg).execute()
        sb.table("instrument_legs").delete().eq("symbol", sym).eq("leg", leg).execute()
    return len(filas), len(sobran)


def patas_sinteticas(insts: dict) -> dict:
    """Las patas de TODOS los bonos deducidas de su estructura, ignorando
    instrument_legs. --sin-tablas usa esto para ver qué haría el deductor puro,
    sin que lo tape ningún override cargado a mano."""
    out = {}
    for sym, i in insts.items():
        for leg, p in patas_por_estructura(i):
            out.setdefault(sym, []).append((leg, p))
    return out


def patas_por_estructura(i: dict) -> list:
    """Qué patas le tocan a un bono por su ESTRUCTURA DE PAGOS, no por quién lo
    emitió. Un provincial CER y un soberano CER se valúan igual; un corporativo
    en dólares y un hard dollar del Tesoro también. `instrument_type` dice de
    dónde viene el papel y no sirve para valuar: lo que decide es el ajuste del
    capital (`referencias`) y, cuando no hay ajuste, la moneda de pago.

    Devuelve [(leg, params), ...]. Un dual lleva dos.
    """
    ref = (i.get("referencias") or "").strip()
    margen = _f(i.get("margen_ref"), 0.0)

    if ref == "Dual":
        # Un dual cobra el máximo entre sus dos patas, y `referencias` dice
        # "Dual" en los tres sabores que hay en la base sin distinguirlos. La
        # pata TAMAR está siempre; la otra sale de la ficha:
        #   nominal en dólares  -> dólar linked (TMVE8)
        #   tasa fija cargada   -> tasa fija    (TTD26)
        #   ninguna de las dos  -> CER          (TXMD8 y la familia TXM*)
        otra = ("CER", {})
        if (i.get("moneda_denom") or "").upper() == "USD":
            otra = ("DLK", {"spread": 0.0})
        elif i.get("tasa_int") is not None:
            otra = ("FIJA", {"tem": _f(i.get("tasa_int"))})
        # Cuando el nominal es en dólares la pata TAMAR devenga sobre ese nominal
        # pasado a pesos al tipo de cambio INICIAL, que queda fijo desde la
        # emisión. No hace falta pasarlo: el motor lo deduce del calendario, que
        # ya trae la amortización convertida.
        return [("TAMAR", {"margen": margen}), otra]
    if ref == "Tamar":
        return [("TAMAR", {"margen": margen})]
    if ref == "CER":
        return [("CER", {})]
    if ref == "A3500":
        return [("DLK", {})]
    if ref == "Badlar":
        return [("BADLAR", {"margen": margen})]

    # Sin ajuste de capital: decide la moneda en la que paga.
    if i.get("moneda_pago") == "USD":
        return [("HD", {})]
    p = {}
    # `tasa_int` NO tiene una sola convención: en un bullet tipo LECAP es la TEM
    # (es lo que hay cargado en instrument_legs), y en un bono con cupones es la
    # tasa anual del cupón. Pasarla como `tem` en el segundo caso daría una TEM
    # del 29,5% mensual. Con cupones no hace falta: el motor descuenta el
    # calendario, que ya trae cada cupón en pesos.
    if i.get("tasa_int") is not None and i.get("periodicidad_int") in (None, "Nula"):
        p["tem"] = _f(i.get("tasa_int"))
    return [("FIJA", p)]


# ════════════════════════════ valuación ════════════════════════════
def valuar_simbolo(ctx: Ctx, inst: dict, patas: list, esc: dict, prow: Optional[dict]):
    """Devuelve [(leg, Pata, extras)] con ganadora, ytm y breakeven resueltos."""
    sym = inst["symbol"]

    faltan = [lg for lg, _ in patas if lg not in MOTORES]
    if faltan:
        # Valuar sólo algunas patas y declarar ganadora entre ellas daría un
        # número mal: el max() tiene que ser sobre TODAS las patas del bono.
        print(f"[SKIP] {sym}: sin motor para {', '.join(faltan)}")
        return None

    vals = {}
    for leg, p in patas:
        try:
            vals[leg] = (MOTORES[leg](ctx, inst, p, esc), p)
        except Exception as e:
            print(f"[SKIP] {sym}/{leg}: {e}")
            return None
    if not vals:
        return None

    ganadora = max(vals, key=lambda lg: vals[lg][0].vpv)

    # TIR de cada pata contra el precio de mercado. Liquidación T+1 hábil,
    # actual/365 (criterio XIRR), pago único al vencimiento.
    precio = _f(_pick(prow, POSIBLES_PRECIO)) if prow else None
    fecha_liq = ctx.habil_siguiente(ctx.hoy)
    dias_corr = (inst["_vencimiento"] - fecha_liq).days

    out = []
    for leg, (pata, p) in vals.items():
        ytm = ytm_nat = dur = par = None
        if precio and precio > 0 and pata.flujos:
            # Bono con cupones: la TIR sale de descontar el vector entero. El
            # precio entra como flujo negativo en la liquidación, así que el
            # resultado ya es la TIR del comprador de hoy.
            #
            # El vector está en la unidad de `conv` y el precio viene en pesos:
            # `fx_nativa` es el puente (el MEP de un hard dollar, el A3500 de un
            # dólar linked, el coeficiente CER vigente de un CER). Dividir el
            # precio en vez de inflar el vector deja la TIR en la unidad del
            # bono, que es como la quotea el mercado.
            futuros = [(d, c) for d, c in pata.flujos if d > fecha_liq]
            if futuros:
                precio_nat = _f(_pick(prow, pata.precio_cols)) if pata.precio_cols else None
                if not precio_nat or precio_nat <= 0:
                    precio_nat = precio / (pata.fx_nativa or 1.0)
                ytm_nat = xirr([(fecha_liq, -precio_nat)] + futuros)
                dur = macaulay(futuros, ytm_nat, fecha_liq) if ytm_nat is not None else None
                # La TIR nominal en pesos de un bono con cupones exigiría
                # proyectar todo el camino del ajuste pago por pago. Sólo sirve
                # para comparar patas dentro de un dual y ningún dual amortiza,
                # así que se reporta la nativa en las dos columnas y `ytm_conv`
                # dice en qué unidad está.
                ytm = ytm_nat
                if pata.vt:
                    par = precio / pata.vt * 100
        elif precio and precio > 0 and dias_corr > 0:
            # ytm: NOMINAL en pesos. Es la única comparable entre patas, y la que
            # decide cuál gana.
            ytm = (pata.vpv / precio) ** (365 / dias_corr) - 1
            dur = dias_corr / 365.0
            # ytm_nativa: la misma cuenta con la base propia de la pata, así que
            # sale en su unidad (real sobre CER, dólares, o pesos). Es la que se
            # compara contra la curva de su clase.
            base_nat = pata.base_nativa if pata.base_nativa is not None else pata.vpv
            ytm_nat = (base_nat / precio) ** (365 / dias_corr) - 1
            if pata.vt:
                par = precio / pata.vt * 100

        # ── margen de mercado de una pata TAMAR ──
        # A qué spread sobre la TAMAR esperada cotiza el bono. Es la columna
        # "Margen mkt." del panel y hasta ahora la escribía sólo tamar.py, que
        # para un bono con cupones capitaliza la vida entera y da disparates: a
        # RVS1O le ponía TAMAR +620 pp.
        #
        # Se replica la convención de tamar.py al pie de la letra —TEM sobre
        # períodos de 30 días con días 30/360, y la resta en TNA contra TNA— para
        # que el número de los bullet no se mueva ni un bp: es el que se validó
        # contra el terminal. Lo único nuevo es de dónde sale la TEM cuando el
        # bono paga cupones: ahí no hay un vpv/precio único y se deriva de la TIR.
        #
        # No se calcula para BADLAR: su cupón devenga TNA·días/365 simple, así que
        # pasar por tamar_tna() —que capitaliza en períodos de 32 días— mezclaría
        # convenciones y el margen saldría corrido.
        margen_mkt = None
        if leg == "TAMAR" and pata.driver is not None and precio and precio > 0:
            tem_ef = None
            if pata.flujos:
                if ytm_nat is not None:
                    tem_ef = (1 + ytm_nat) ** (30 / 360) - 1
            else:
                d360 = dias360(fecha_liq, inst["_vencimiento"])
                if d360 > 0:
                    tem_ef = (pata.vpv / precio) ** (30 / d360) - 1
            if tem_ef is not None and tem_ef > -1:
                margen_mkt = tamar_tna(tem_ef) - pata.driver

        be = None
        if len(vals) > 1 and pata.driver is not None:
            rival = max(v[0].vpv for lg, v in vals.items() if lg != leg)
            be = breakeven(MOTORES[leg], ctx, inst, p, esc, rival,
                           DRIVER_BOUNDS.get(leg, (-0.99, 20.0)))

        out.append((leg, pata, {
            "is_winner": leg == ganadora,
            "ytm": ytm, "ytm_nativa": ytm_nat, "ytm_conv": pata.conv,
            "duration_y": dur, "paridad": par, "breakeven": be,
            "margen_mercado": margen_mkt, "precio": precio,
        }))
    return out


def _r(x, n=8):
    return None if x is None else round(float(x), n)


def once(args) -> int:
    ctx = ctx_de(args)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    insts = cargar_instrumentos(symbols)
    patas = patas_sinteticas(insts) if args.sin_tablas else cargar_patas(insts)
    esc = {} if args.sin_tablas else cargar_escenario(args.scenario)

    # Las patas deducidas tienen que existir en instrument_legs antes de escribir
    # una valuación: `valuations` las referencia por foreign key. Con --symbols no
    # se sincroniza, porque el borrado de sobrantes sólo puede decidirse viendo el
    # universo completo.
    if not args.dry_run and not args.sin_tablas and not symbols:
        sincronizar_leg_types()
        altas, bajas = sincronizar_patas(insts, patas)
        if altas or bajas:
            print(f"[LEGS] {altas} patas deducidas escritas, {bajas} sobrantes borradas")

    precios = cargar_precios(list(patas.keys()) or None)

    n = 0
    for sym in sorted(patas):
        inst = insts.get(sym)
        if inst is None:
            continue
        res = valuar_simbolo(ctx, inst, patas[sym], esc, precios.get(sym))
        if not res:
            continue
        n += 1
        dual = len(res) > 1
        marca = f"DUAL:{'/'.join(sorted(lg for lg, _, _ in res))}" if dual else res[0][0]
        print(f"\n[{marca}] {sym}  emisión {inst['_emision']} → vto {inst['_vencimiento']}")
        for leg, pata, x in res:
            gana = " ◄ paga" if x["is_winner"] and dual else ""
            tem = f"TEM {pata.tem*100:6.3f}%" if pata.tem is not None else " " * 11
            if x["ytm"] is None:
                ytm = "TIR    s/precio"
            elif x["ytm_conv"] == "nominal_ars":
                ytm = f"TIR {x['ytm']:7.2%}"
            elif x["ytm"] == x["ytm_nativa"]:
                # Bono con cupones: hay una sola TIR y está en su unidad.
                u = {"real_cer": "real", "usd": "USD"}.get(x["ytm_conv"], x["ytm_conv"])
                ytm = f"TIR {x['ytm_nativa']:7.2%} {u}"
            else:
                u = {"real_cer": "real", "usd": "USD"}.get(x["ytm_conv"], x["ytm_conv"])
                ytm = f"TIR {x['ytm']:7.2%} $ / {x['ytm_nativa']:6.2%} {u}"
            be = f" | BE {pata_be_fmt(leg, x['breakeven'])}" if x["breakeven"] is not None else ""
            print(f"    {leg:<6} {tem}  VPV {pata.vpv:8.2f}  {ytm}{be}{gana}")

        if args.dry_run:
            continue

        ts = datetime.now(timezone.utc).isoformat()
        filas = [{
            "symbol": sym, "leg": leg, "scenario": args.scenario,
            "vpv": _r(pata.vpv, 6), "vt": _r(pata.vt, 6), "tem": _r(pata.tem),
            "driver": _r(pata.driver), "ytm": _r(x["ytm"], 6),
            "ytm_nativa": _r(x["ytm_nativa"], 6), "ytm_conv": x["ytm_conv"],
            "duration_y": _r(x["duration_y"], 6), "breakeven": _r(x["breakeven"]),
            "is_winner": x["is_winner"], "params": pata.params, "ts": ts,
        } for leg, pata, x in res]
        sb.table("valuations").upsert(filas).execute()

        # Headline en prices. Ver sql/006_ytm_semantica.sql para el reparto.
        #
        # ytm_ars: patas.py es el ÚNICO dueño, y lo escribe para todo lo que
        # cubre. Es la TIR nominal en pesos, la única comparable entre clases de
        # activo, y nadie más la puede calcular porque hace falta proyectar
        # inflación o dólar según la pata.
        #
        # ytm / duration_y / paridad: sólo para duales. Un bono de una pata ya
        # tiene dueño de esas columnas (cerv2.py, tamar.py, dlk.py, tir.py) y
        # cada uno las escribe en SU convención: pisarlas con la nominal
        # convertiría un 3,91% real en un 29,55% nominal sin que se note.
        # --headline-all fuerza la toma de posesión; sólo tiene sentido si
        # apagás el motor viejo del mismo universo.
        g = next(r for r in res if r[2]["is_winner"])
        head = {"symbol": sym, "vpv": _r(g[1].vpv, 4), "ts": ts}
        # ytm_ars es por definición nominal en pesos. En un bullet `ytm` ya es
        # esa, porque el motor proyecta el ajuste hasta el vencimiento. En un
        # bono con cupones la TIR sale en la unidad del vector —real sobre CER,
        # dólares— y ponerla acá haría pasar un 5,8% real por un 5,8% nominal;
        # proyectar el ajuste pago por pago para esos no está hecho, así que la
        # columna queda vacía en vez de mentir.
        if g[2]["ytm"] is not None and (g[1].flujos is None
                                        or g[2]["ytm_conv"] == "nominal_ars"):
            head["ytm_ars"] = _r(g[2]["ytm"], 6)

        if dual or args.headline_all:
            # `ytm` va en la convención NATIVA del bono, que es la que escriben
            # los motores viejos y la que espera el front: un CER se quotea en
            # tasa real y un hard dollar en dólares. Poner acá la nominal en
            # pesos con la etiqueta del nativo es justo el error que esta
            # columna no perdona: un 3,91% real pasaría por un 29,55% nominal
            # sin que se note. La nominal vive en ytm_ars.
            if g[2]["ytm_nativa"] is not None:
                head["ytm"] = _r(g[2]["ytm_nativa"], 6)
                head["ytm_tipo"] = g[2]["ytm_conv"]
                head["duration_y"] = _r(g[2]["duration_y"], 6)
            if g[2]["paridad"] is not None:
                head["paridad"] = _r(g[2]["paridad"], 4)
            if g[2]["margen_mercado"] is not None:
                head["margen_mercado"] = _r(g[2]["margen_mercado"], 6)
        sb.table("prices").upsert(head).execute()

    print(f"\n[PATAS] {n} instrumentos valuados"
          f"{' (dry-run, no se escribió nada)' if args.dry_run else ''}")
    return n


def pata_be_fmt(leg: str, v: float) -> str:
    return f"${v:,.0f}" if leg == "DLK" else f"{v:.2%}"


def ctx_de(args) -> Ctx:
    return Ctx(date.fromisoformat(args.hoy) if args.hoy else date.today())


# ════════════════════════════ check ════════════════════════════
def check(args):
    """Compara la pata TAMAR contra lo que ya dejó tamar.py en prices.

    No tienen por qué dar idéntico y la diferencia es esperable por dos motivos:
      1. tamar.py promedia TEMs ponderadas por días; el prospecto promedia TNA
         sobre toda la ventana y recién ahí pasa a TEM.
      2. tamar.py lee mal la tabla holidays (busca 'fecha', la columna es
         'holiday_date') y por eso corre con feriados vacíos: su ventana de
         "10 días hábiles" arranca y termina en fechas distintas.
    """
    ctx = ctx_de(args)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    insts = cargar_instrumentos(symbols)
    patas = patas_sinteticas(insts) if args.sin_tablas else cargar_patas(insts)
    esc = {} if args.sin_tablas else cargar_escenario(args.scenario)
    precios = cargar_precios()

    print(f"feriados cargados: {len(ctx.feriados)}   (tamar.py corre con 0 por el bug de columna)\n")
    print(f"{'symbol':8} {'TEM patas':>10} {'TEM prices':>11} {'Δ bps':>7} "
          f"{'VPV patas':>10} {'VPV prices':>11} {'Δ%':>7} {'TIR patas':>10} {'TIR prices':>11}")
    print("─" * 100)
    for sym in sorted(patas):
        if not any(lg == "TAMAR" for lg, _ in patas[sym]):
            continue
        inst, prow = insts.get(sym), precios.get(sym)
        if inst is None or not prow or prow.get("tem_total") is None:
            continue
        res = valuar_simbolo(ctx, inst, patas[sym], esc, prow)
        if not res:
            continue
        pata = next(p for lg, p, _ in res if lg == "TAMAR")
        x = next(e for lg, _, e in res if lg == "TAMAR")
        t0, v0, y0 = _f(prow["tem_total"]), _f(prow.get("vpv")), _f(prow.get("ytm"))
        if pata.tem is None:
            # Pata que fue por el calendario: no hay una TEM única que comparar,
            # porque cada cupón tiene su propia ventana de TAMAR. tamar.py le
            # inventa una capitalizando desde la emisión, y es justo el número
            # que no sirve para estos bonos.
            print(f"{sym:8} {'(cupones)':>10} {t0:>11.4%} {'':>7} "
                  f"{pata.vpv:>10.2f} {v0 if v0 else float('nan'):>11.2f} {'':>7} "
                  f"{x['ytm_nativa'] if x['ytm_nativa'] else float('nan'):>10.2%} "
                  f"{y0 if y0 else float('nan'):>11.2%}")
            continue
        d_tem = (pata.tem - t0) * 10000
        d_vpv = (pata.vpv / v0 - 1) * 100 if v0 else float("nan")
        print(f"{sym:8} {pata.tem:>10.4%} {t0:>11.4%} {d_tem:>7.1f} "
              f"{pata.vpv:>10.2f} {v0 if v0 else float('nan'):>11.2f} {d_vpv:>6.2f}% "
              f"{x['ytm'] if x['ytm'] else float('nan'):>10.2%} "
              f"{y0 if y0 else float('nan'):>11.2%}")


# ════════════════════════════ main ════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="Valuación por patas (bullet y duales)")
    ap.add_argument("--dry-run", action="store_true", help="no escribe en la DB")
    ap.add_argument("--check", action="store_true", help="compara la pata TAMAR contra prices")
    ap.add_argument("--symbols", help="lista separada por comas")
    ap.add_argument("--scenario", default="base")
    ap.add_argument("--sin-tablas", action="store_true",
                    help="ignora instrument_legs: deduce las patas de la ficha")
    ap.add_argument("--headline-all", action="store_true",
                    help="escribe el headline en prices también para bonos de una pata "
                         "(por default sólo duales, para no pisar a tir.py/cerv2.py)")
    ap.add_argument("--hoy", help="fecha de valuación YYYY-MM-DD (default: hoy)")
    ap.add_argument("--loop", action="store_true", help=f"ciclo continuo cada {INTERVAL_SEC}s")
    args = ap.parse_args()

    if args.check:
        check(args)
        return
    if not args.loop:
        once(args)
        return
    print(f"[PATAS] ciclo cada {INTERVAL_SEC/60:.0f} min")
    while True:
        try:
            once(args)
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
