"""
patas.py — Motor genérico de valuación por patas (bonos bullet y duales).

Un bono no tiene "un tipo", tiene patas. Un TAMAR común es un bono de UNA pata;
un dual es un bono de DOS. Al vencimiento paga el máximo entre ellas.

Lee:
  - instrument_legs  (symbol, leg, params)   -> qué patas tiene cada bono
  - scenarios        (id, supuestos)         -> supuestos de proyección
  - instruments_v2, prices, holidays, tamar_historico
Escribe:
  - valuations       (symbol, leg, scenario) -> una fila por pata
  - prices           (headline de la pata ganadora: ytm/duration_y/vpv/paridad)

CONTRATO DE UN MOTOR
    motor(ctx, inst, params, esc, driver=None) -> Pata

    Devuelve `vpv` SIEMPRE en pesos, base 100 de VN, al vencimiento. Es lo único
    que hace comparables a las patas entre sí. `driver` es la variable que maneja
    la pata (TNA TAMAR, inflación mensual, dólar al vto.); si viene, pisa el
    supuesto del escenario. Ese parámetro es lo que permite calcular el breakeven
    por bisección sin escribir una fórmula por cada combinación de patas.

    Sumar un tipo de pata nuevo = una función + una entrada en MOTORES.
    Sumar un dual nuevo         = dos INSERT en instrument_legs, sin tocar código.

Uso:
    python patas.py                          # valúa todo y escribe
    python patas.py --dry-run                # no escribe nada
    python patas.py --check                  # compara contra lo que dejó tamar.py
    python patas.py --symbols TMF27,TTS26
    python patas.py --sin-tablas             # deriva las patas de instruments_v2
                                             #   (permite validar antes del DDL)
    python patas.py --loop                   # ciclo continuo cada INTERVAL_SEC
"""
import argparse
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

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


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ════════════════════════════ contexto ════════════════════════════
class Ctx:
    """Datos compartidos por los motores. Carga perezosa: si no hay ninguna pata
    TAMAR que valuar, no se pega el viaje a tamar_historico."""

    def __init__(self, hoy: date):
        self.hoy = hoy
        self._feriados = None
        self._tamar = None
        self._cer = None
        self._rem = {}
        self._fx = None

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
        """{date: TNA decimal}. valor_tna viene en % en la tabla."""
        if self._tamar is None:
            res = sb.table("tamar_historico").select("fecha, valor_tna").order("fecha").execute()
            self._tamar = {}
            for r in (res.data or []):
                v = _f(r.get("valor_tna"))
                if v is None:
                    continue
                try:
                    self._tamar[date.fromisoformat(str(r["fecha"])[:10])] = v / 100
                except ValueError:
                    pass
        return self._tamar

    @property
    def cer(self) -> dict:
        """{date: coeficiente CER}. Serie que sincroniza cerv2.py desde el BCRA."""
        if self._cer is None:
            res = sb.table("cer_historico").select("fecha, valor_cer").order("fecha").execute()
            self._cer = {}
            for r in (res.data or []):
                v = _f(r.get("valor_cer"))
                if v is None:
                    continue
                try:
                    self._cer[date.fromisoformat(str(r["fecha"])[:10])] = v
                except ValueError:
                    pass
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
def motor_fija(ctx: Ctx, inst: dict, p: dict, esc: dict, driver=None) -> Pata:
    """Tasa fija efectiva mensual capitalizable hasta el vencimiento.
       VPV = 100 * (1 + Tm) ^ ((DÍAS/360)*12)      params: {"tem": 0.0217}
       No tiene driver: es determinística, siempre es el lado 'target' del breakeven."""
    tem = _f(p.get("tem"))
    if tem is None:
        raise ValueError("pata FIJA sin params.tem")
    emi, vto = inst["_emision"], inst["_vencimiento"]
    base = 100 * (_f(p.get("fx_base"), 1.0) or 1.0)
    return Pata(
        vpv=base * (1 + tem) ** meses360(emi, vto),
        tem=tem,
        driver=None,
        vt=base * (1 + tem) ** max(0.0, meses360(emi, ctx.hoy)),
        params={"tem_fija": round(tem, 8), "base": round(base, 6)},
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

    ventana = ctx.rango_habiles(ctx.habil_anterior(emi, 10), ctx.habil_anterior(vto, 10))
    if not ventana:
        raise ValueError("ventana de TAMAR vacía")

    obs = [serie[d] for d in ventana if d in serie and d <= ctx.hoy]
    n_tot, n_obs = len(ventana), len(obs)
    n_proy = n_tot - n_obs

    if driver is not None:
        tna_fut, origen = float(driver), "driver"
    elif esc.get("tamar_tna") is not None:
        tna_fut, origen = float(esc["tamar_tna"]), "escenario"
    else:
        recientes = sorted(d for d in serie if d <= ctx.hoy)[-N_PROY:]
        if not recientes:
            raise ValueError("sin TAMAR observada")
        tna_fut, origen = sum(serie[d] for d in recientes) / len(recientes), f"prom. últimos {N_PROY}"

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
    vpv = 100 * ajuste * (1 + tem) ** meses

    # Valor técnico devengado: el CER APLICABLE hoy es el de 10 días hábiles
    # antes de la liquidación, no el de hoy. El prospecto define el ajuste sobre
    # la ventana [emisión-10h ; pago-10h], así que el coeficiente vigente arrastra
    # ese rezago. Usar el CER de hoy sobrestima la TIR real ~30 bps.
    f_apl_cer = ctx.habil_anterior(ctx.fecha_liq, 10)
    cer_apl = ctx.cer_en(min(f_apl_cer, ult_obs))
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


MOTORES: dict[str, Callable[..., Pata]] = {
    "FIJA":  motor_fija,
    "TAMAR": motor_tamar,
    "CER":   motor_cer,
    "DLK":   motor_dlk,
}

# Rango de bisección del driver de cada pata, en sus propias unidades.
DRIVER_BOUNDS: dict[str, tuple] = {
    "TAMAR": (-0.99, 20.0),     # TNA decimal
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
    q = sb.table("instruments_v2").select("*").eq("is_active", True)
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


def cargar_patas(symbols=None) -> dict:
    """{symbol: [(leg, params), ...]} desde instrument_legs."""
    q = sb.table("instrument_legs").select("symbol, leg, params")
    if symbols:
        q = q.in_("symbol", symbols)
    out = {}
    for r in (q.execute().data or []):
        out.setdefault(r["symbol"], []).append((r["leg"], r.get("params") or {}))
    return out


def patas_sinteticas(insts: dict) -> dict:
    """Misma regla que el backfill de 001_patas_duales.sql, en memoria. Permite
    validar los motores antes de correr el DDL."""
    out = {}
    for sym, i in insts.items():
        if i.get("periodicidad_int") != "Nula" or i.get("instrument_type") == "ON":
            continue
        ref = (i.get("referencias") or "").strip()
        leg = {"Tamar": "TAMAR", "CER": "CER", "A3500": "DLK"}.get(ref, "FIJA")
        p = {}
        if leg == "TAMAR":
            p["margen"] = _f(i.get("margen_ref"), 0.0)
        elif leg == "FIJA" and i.get("tasa_int") is not None:
            p["tem"] = _f(i.get("tasa_int"))
        out[sym] = [(leg, p)]
    return out


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
        if precio and precio > 0 and dias_corr > 0:
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

        be = None
        if len(vals) > 1 and pata.driver is not None:
            rival = max(v[0].vpv for lg, v in vals.items() if lg != leg)
            be = breakeven(MOTORES[leg], ctx, inst, p, esc, rival,
                           DRIVER_BOUNDS.get(leg, (-0.99, 20.0)))

        out.append((leg, pata, {
            "is_winner": leg == ganadora,
            "ytm": ytm, "ytm_nativa": ytm_nat, "ytm_conv": pata.conv,
            "duration_y": dur, "paridad": par, "breakeven": be,
            "precio": precio,
        }))
    return out


def _r(x, n=8):
    return None if x is None else round(float(x), n)


def once(args) -> int:
    ctx = ctx_de(args)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    insts = cargar_instrumentos(symbols)
    patas = patas_sinteticas(insts) if args.sin_tablas else cargar_patas(symbols)
    esc = {} if args.sin_tablas else cargar_escenario(args.scenario)
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
        if g[2]["ytm"] is not None:
            head["ytm_ars"] = _r(g[2]["ytm"], 6)

        if dual or args.headline_all:
            if g[2]["ytm"] is not None:
                head["ytm"] = _r(g[2]["ytm"], 6)
                head["ytm_tipo"] = "nominal_ars"
                head["duration_y"] = _r(g[2]["duration_y"], 6)
            if g[2]["paridad"] is not None:
                head["paridad"] = _r(g[2]["paridad"], 4)
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
    patas = patas_sinteticas(insts) if args.sin_tablas else cargar_patas(symbols)
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
                    help="deriva las patas de instruments_v2 en vez de instrument_legs")
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
