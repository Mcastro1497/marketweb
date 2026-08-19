"""Cálculo de tasas: XIRR, duración y las conversiones de los prospectos.

xirr, _fdf, _yf y macaulay se movieron sin modificar desde cerv2.py, dlk.py y
tir.py, donde estaban triplicadas. Se verificó por AST que las tres copias eran
idénticas antes de extraerlas.
"""
from datetime import timezone

import pandas as pd


def _yf(d0, d1) -> float:
    """Fracción de año actual/365 entre dos fechas, normalizando a UTC medianoche."""
    def norm(d):
        if isinstance(d, pd.Timestamp):
            d = d.to_pydatetime()
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (norm(d1) - norm(d0)).days / 365.0


def _fdf(r, cfs):
    d0, one = cfs[0][0], 1.0 + r
    if one <= 0:
        return float("inf"), float("inf")
    f = df = 0.0
    for di, ci in cfs:
        t = _yf(d0, di)
        f += ci / one ** t
        df += ci * (-t) * one ** (-t - 1)
    return f, df


def xirr(cfs, guess=0.10):
    """TIR de una serie de (fecha, monto). Newton y, si no converge, bisección."""
    cfs = sorted(cfs, key=lambda x: x[0])
    r = guess
    for _ in range(80):
        f, df = _fdf(r, cfs)
        if not (df and abs(df) > 1e-18):
            break
        rn = r - f / df
        if rn <= -0.9999:
            break
        if abs(rn - r) < 1e-12:
            return rn
        r = rn
    fval = lambda x: _fdf(x, cfs)[0]
    a = b = None
    lx = ly = None
    for x in [-0.9, -0.5, -0.1, 0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.25, 0.5, 1, 2, 5, 10]:
        y = fval(x)
        if lx is not None and pd.notna(ly) and pd.notna(y) and ly * y <= 0:
            a, b = lx, x
            break
        lx, ly = x, y
    if a is None:
        a, b = 0.0, 10.0
        fa, fb = fval(a), fval(b)
        tries = 0
        while fa * fb > 0 and b < 200 and tries < 12:
            b *= 2
            fb = fval(b)
            tries += 1
        if fa * fb > 0:
            return None
    lo, hi = a, b
    flo, fhi = fval(lo), fval(hi)
    for _ in range(200):
        m = 0.5 * (lo + hi)
        fm = fval(m)
        if abs(fm) < 1e-10 or (hi - lo) < 1e-12:
            return m
        if flo * fm <= 0:
            hi, fhi = m, fm
        else:
            lo, flo = m, fm
    return m


def macaulay(cfs_pos, r):
    """Duración de Macaulay en años, con (t_en_años, monto) ya positivos."""
    if r is None or r <= -0.999:
        return None
    num = den = 0.0
    for t, c in cfs_pos:
        pv = c / (1.0 + r) ** t
        num += t * pv
        den += pv
    return (num / den) if den else None


def tamar_tem(tna: float, margen: float = 0.0) -> float:
    """TNA decimal -> TEM decimal, fórmula de los prospectos:

        TAMAR_TEM = [(1 + (TAMAR + margen)/(365/32))^(365/32)]^(1/12) - 1

    El margen va DENTRO, sumado a la TNA antes de convertir (Res. Conj. 4/2025 y
    32/2026: la fórmula dice literalmente "TAMAR + 3%" en el numerador).
    """
    return ((1 + (tna + margen) / (365 / 32)) ** (365 / 32)) ** (1 / 12) - 1


def tamar_tna(tem: float, margen: float = 0.0) -> float:
    """Inversa de tamar_tem: dada una TEM objetivo, la TNA que la produce."""
    return (((1 + tem) ** 12) ** (32 / 365) - 1) * (365 / 32) - margen


def dias360(d1, d2) -> int:
    """30/360 US, idéntico a Excel DIAS360(d1, d2)."""
    a, b = d1.day, d2.day
    if a == 31:
        a = 30
    if b == 31 and a == 30:
        b = 30
    return (d2.year - d1.year) * 360 + (d2.month - d1.month) * 30 + (b - a)


def meses360(d1, d2) -> float:
    """Exponente (DÍAS/360)*12 de la fórmula del prospecto."""
    return dias360(d1, d2) / 30.0


def tir_y_duracion(fecha_val, precio: float, flujos):
    """TIR y duración de Macaulay de un bono bullet o con cupones.

    fecha_val: datetime de liquidación (el flujo negativo).
    precio:    ya AJUSTADO por quien llama. Es lo único que cambia entre motores:
               tir.py y dlk.py pasan el precio en dólares, cerv2.py pasa el precio
               deflactado por el ratio CER, y por eso la TIR sale en la convención
               de cada uno. El cálculo de acá es el mismo para los tres.
    flujos:    [(datetime, monto)] futuros, sin el precio.

    Devuelve (ytm, duration_y) o (None, None) si no converge.

    Estaba repetido tal cual dentro del bucle de cerv2.py, dlk.py y tir.py. Se
    unifica para que el bucle de revaluación continua use exactamente el mismo
    código que los motores y no puedan divergir.
    """
    cfs = [(fecha_val, -float(precio))]
    cfs += [(d, float(m)) for d, m in flujos if float(m) != 0]
    if len(cfs) < 2:
        return None, None
    r = xirr(cfs, 0.10)
    if r is None or r != r or r in (float("inf"), float("-inf")):
        return None, None
    pos = [(_yf(fecha_val, d), float(m)) for d, m in flujos if float(m) != 0]
    return r, macaulay(pos, r)
