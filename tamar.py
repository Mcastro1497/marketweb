"""
valuar_tamar_supabase.py
Valuación de BONOS TAMAR del Tesoro (floater capitalizable, bullet) — TMF27 y similares.

Lee:
  - series (serie=tamar_tna, valor en %)      -> sincronizada por series_sync.py
  - instruments (tipo='TAMAR', is_active)      -> fechas, margen (cupon)
  - prices                                     -> precio de mercado (de Precios_v5)
  - holidays                                   -> feriados para los "10 días hábiles"
Escribe:
  - prices: TIR + duración (UPDATE sobre la fila existente, no toca el precio)

Metodología (valuación en TEM):
  TAMAR TEM(x) = [ (1 + x/(365/32))^(365/32) ]^(1/12) - 1
  Ventana [emisión - 10 háb ; vto - 10 háb]. Futuro = promedio de los últimos
  N_PROY datos de TAMAR, plano.
  1) TAMAR observada (TNA) -> TEM  y  TAMAR proyectada (TNA) -> TEM   (ambas SIN margen)
  2) TEM TAMAR = promedio ponderado por días = TEM_obs*(n_obs/total) + TEM_proy*(n_proy/total)
  3) TEM total = TEM TAMAR + TEM margen,  con TEM margen = TAMAR TEM(MARGEN)
  4) DÍAS = DIAS360(emisión, vencimiento)   (30/360 US = Excel DIAS360)
     VPV  = 100 * (1 + TEM total)^((DÍAS/360)*12)

NOTA: patas.py reimplementa esta valuación como la pata 'TAMAR' del modelo
genérico (ver sql/001_patas_duales.sql), donde además sirve para los duales.
Está verificado que reproduce estos números. Mientras los dos convivan, este
script sigue siendo el dueño de las columnas TAMAR de `prices`; patas.py sólo
escribe el headline de bonos multi-pata salvo que se lo corra con --headline-all.
"""
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY  = os.environ["SERVICE_KEY"]          # OJO: SERVICE_KEY, NO SUPABASE_SERVICE_KEY
sb = create_client(SUPABASE_URL, SERVICE_KEY)

INTERVAL_SEC = 60   # 30 min

# Cuántos datos recientes de TAMAR se promedian para proyectar el futuro.
# OJO: NO es el "10 días hábiles" de la ventana del prospecto, es el supuesto de modelo.
N_PROY = int(os.getenv("N_PROY", "5"))   # cantidad de datos recientes de TAMAR para proyectar

# ---- Salida en prices: IDÉNTICA a TIR_v5_supabase.py ----
#   ytm        = rendimiento anual efectivo (XIRR), en DECIMAL (0.2646 = 26.46%)
#   duration_y = duración en años
# Se hace upsert (merge): solo toca estas columnas + ts, no pisa last/bid/etc.

# Posibles nombres de la columna de precio en prices (se autodetecta)
# Prioridad: price_ars (precio en pesos), luego closing_price, y recién después last/etc.
POSIBLES_PRECIO = ["price_ars", "closing_price", "price", "last", "px", "ultimo", "cierre", "precio"]

# Fallback de fecha de emisión si todavía no agregaste la columna a instruments
FECHA_EMISION_OVERRIDE = {
    "TMF27": date(2026, 2, 13),
}

# ============================ helpers de cálculo ============================
def dias360(d1: date, d2: date) -> int:
    """30/360 US, idéntico a Excel DIAS360(d1, d2)."""
    a, b = d1.day, d2.day
    if a == 31:
        a = 30
    if b == 31 and a == 30:
        b = 30
    return (d2.year - d1.year) * 360 + (d2.month - d1.month) * 30 + (b - a)

def tamar_tem(tamar_dec: float, margen_dec: float) -> float:
    """TAMAR y margen en DECIMAL (0.28, 0.065). Devuelve TEM mensual en decimal."""
    base = 1 + (tamar_dec + margen_dec) / (365 / 32)
    tea = base ** (365 / 32)
    return tea ** (1 / 12) - 1

# ============================ días hábiles / feriados ============================
def _pick(row: dict, candidatos):
    for c in candidatos:
        if c in row and row[c] is not None:
            return row[c]
    return None

def col_existente(cols, candidatos):
    """Devuelve el primer NOMBRE de columna candidato que exista en 'cols', o None."""
    for c in candidatos:
        if c in cols:
            return c
    return None

def cargar_feriados() -> set:
    """Lee la tabla holidays y devuelve un set de date. Autodetecta la columna de fecha."""
    res = sb.table("holidays").select("*").execute()
    fer = set()
    for r in (res.data or []):
        # holiday_date es el nombre real de la columna. Sin él, _pick devolvía
        # None para todas las filas y la ventana de "10 días hábiles" corría
        # ignorando los feriados.
        val = _pick(r, ["holiday_date", "fecha", "date", "dia", "feriado", "holiday"])
        if val is None:
            continue
        try:
            fer.add(date.fromisoformat(str(val)[:10]))
        except Exception:
            pass
    return fer

def es_habil(d: date, feriados: set) -> bool:
    return d.weekday() < 5 and d not in feriados

def habil_anterior(d: date, n: int, feriados: set) -> date:
    cnt = 0
    while cnt < n:
        d -= timedelta(days=1)
        if es_habil(d, feriados):
            cnt += 1
    return d

def habil_siguiente(d: date, feriados: set) -> date:
    d += timedelta(days=1)
    while not es_habil(d, feriados):
        d += timedelta(days=1)
    return d

def rango_habiles(ini: date, fin: date, feriados: set):
    out, d = [], ini
    while d <= fin:
        if es_habil(d, feriados):
            out.append(d)
        d += timedelta(days=1)
    return out

# ============================ carga de datos ============================
def cargar_tamar() -> dict:
    """{date: TNA_decimal}. En `series` la TAMAR va cruda en %, se divide por 100."""
    # Paginado: PostgREST corta en 1.000 filas por defecto, sin avisar.
    filas, desde = [], 0
    while True:
        d = (sb.table("series").select("fecha, valor").eq("serie", "tamar_tna")
               .order("fecha").range(desde, desde + 999).execute().data or [])
        filas += d
        if len(d) < 1000:
            break
        desde += 1000
    serie = {}
    for r in filas:
        try:
            serie[date.fromisoformat(str(r["fecha"])[:10])] = float(r["valor"]) / 100
        except Exception:
            pass
    return serie

def cargar_instrumentos_tamar():
    res = (sb.table("instruments")
             .select("*")
             .eq("referencias", "Tamar")
             .eq("is_active", True)
             .execute())
    return res.data or []

def cargar_fila_precio(symbol: str):
    res = sb.table("prices").select("*").eq("symbol", symbol).limit(1).execute()
    return (res.data or [None])[0]

# ============================ valuación de un bono ============================
def valuar_bono(inst: dict, tamar_obs: dict, feriados: set, hoy: date):
    symbol = inst["symbol"]

    # fechas y margen (instruments_v2: emision, vencimiento, margen_ref)
    emi_raw = inst.get("emision")
    emision = (date.fromisoformat(str(emi_raw)[:10]) if emi_raw
               else FECHA_EMISION_OVERRIDE.get(symbol))
    if emision is None:
        print(f"[SKIP] {symbol}: sin emision (ni columna ni override)")
        return None
    vto = date.fromisoformat(str(inst["vencimiento"])[:10])
    margen = float(inst.get("margen_ref") or 0)   # margen_ref ya viene en decimal (0.069 = 6.9% TNA)

    # ventana de promediado: [emisión - 10 háb ; vto - 10 háb]
    v_ini = habil_anterior(emision, 10, feriados)
    v_fin = habil_anterior(vto, 10, feriados)
    dias_habiles = rango_habiles(v_ini, v_fin, feriados)
    if not dias_habiles:
        print(f"[SKIP] {symbol}: ventana de TAMAR vacía")
        return None

    # proyección: promedio de los últimos N_PROY observados (<= hoy)
    obs_fechas = sorted(d for d in tamar_obs if d <= hoy)
    if len(obs_fechas) < 1:
        print(f"[SKIP] {symbol}: sin TAMAR observada en DB")
        return None
    ult = obs_fechas[-N_PROY:]
    proy = sum(tamar_obs[d] for d in ult) / len(ult)

    # separar observados (en la ventana, <= hoy) de proyectados
    obs_vals = [tamar_obs[d] for d in dias_habiles if d in tamar_obs and d <= hoy]
    n_obs  = len(obs_vals)
    n_proy = len(dias_habiles) - n_obs
    total  = len(dias_habiles)

    tamar_obs_avg = (sum(obs_vals) / n_obs) if obs_vals else 0.0   # TAMAR observada (TNA prom)
    # proy = TAMAR proyectada (TNA), calculada arriba

    # 1) cada tramo de TAMAR -> TEM SIN margen   2) promedio ponderado por días
    # 3) + TEM margen   4) VPV
    w_obs  = n_obs  / total
    w_proy = n_proy / total
    tem_obs    = tamar_tem(tamar_obs_avg, 0.0) if obs_vals else 0.0  # TEM TAMAR observada (sin margen)
    tem_proy   = tamar_tem(proy, 0.0)                                # TEM TAMAR proyectada (sin margen)
    tem_tamar  = tem_obs * w_obs + tem_proy * w_proy                 # TEM TAMAR ponderada por días
    tem_margen = tamar_tem(margen, 0.0)                              # margen (TNA) -> TEM
    tem        = tem_tamar + tem_margen                              # TEM total

    dias = dias360(emision, vto)
    vpv = 100 * (1 + tem) ** ((dias / 360) * 12)     # = (1+TEM)^(dias/30)

    # precio de mercado
    prow = cargar_fila_precio(symbol)
    if not prow:
        print(f"[SKIP] {symbol}: sin fila en prices")
        return None
    precio = _pick(prow, POSIBLES_PRECIO)
    if precio in (None, 0):
        print(f"[SKIP] {symbol}: precio inexistente o cero")
        return None
    precio = float(precio)

    # TIR a partir del precio (pago único al vto). Liquidación T+1 hábil, actual/365 (criterio XIRR)
    fecha_liq = habil_siguiente(hoy, feriados)
    dias_corr = (vto - fecha_liq).days
    if dias_corr <= 0:
        print(f"[SKIP] {symbol}: vencido o liquida >= vto")
        return None
    tea = (vpv / precio) ** (365 / dias_corr) - 1
    md = dias_corr / 365.0                           # Macaulay = plazo (bullet, pago único)

    # valor técnico (capitalizado a la TEM hasta hoy) y paridad — informativos
    vt = 100 * (1 + tem) ** ((dias360(emision, hoy) / 360) * 12)
    paridad = precio / vt * 100

    # ytm/duration_y como TIR_v5 + desglose TAMAR
    # unidades: TNA/TEM en decimal (0.0225 = 2.25%); vpv base 100; paridad en %
    payload = {
        "ytm":           round(tea, 6),
        "ytm_tipo":      "nominal_ars",   # ver sql/006_ytm_semantica.sql
        "duration_y":    round(md, 6),
        "tamar_obs":     round(tamar_obs_avg, 6),   # TNA observada
        "tamar_proy":    round(proy, 6),            # TNA proyectada
        "tem_obs":       round(tem_obs, 6),         # TEM observada (s/margen)
        "tem_proy":      round(tem_proy, 6),        # TEM proyectada (s/margen)
        "tem_ponderada": round(tem_tamar, 6),       # TEM ponderada por días (s/margen)
        "tem_margen":    round(tem_margen, 6),      # TEM del margen
        "tem_total":     round(tem, 6),             # TEM total
        "vpv":           round(vpv, 4),             # base 100
        "paridad":       round(paridad, 4),         # en %
    }
    print(f"[OK] {symbol} | margen {margen*100:.2f}%  ->  TEM margen {tem_margen*100:.3f}%")
    print(f"     observada  : TEM {tem_obs*100:6.3f}% (s/margen)   ({n_obs} días, peso {w_obs*100:.1f}%)")
    print(f"     proyectada : TEM {tem_proy*100:6.3f}% (s/margen)   ({n_proy} días, peso {w_proy*100:.1f}%)")
    print(f"     TEM TAMAR ponderada: {tem_tamar*100:.3f}%  (= {tem_obs*100:.3f}×{w_obs:.3f} + {tem_proy*100:.3f}×{w_proy:.3f})")
    print(f"     TEM total: {tem_tamar*100:.3f}% + {tem_margen*100:.3f}% margen = {tem*100:.3f}%   ->  VPV {vpv:.2f}")
    print(f"     precio {precio:.2f} | paridad {paridad:.2f}% | YTM {tea:.4%} | dur {md:.3f}")
    return symbol, payload

# ============================ once / main ============================
def once():
    hoy = date.today()
    feriados = cargar_feriados()
    tamar_obs = cargar_tamar()
    insts = cargar_instrumentos_tamar()
    if not insts:
        print("[TAMAR] No hay instrumentos tipo='TAMAR' activos.")
        return
    for inst in insts:
        try:
            r = valuar_bono(inst, tamar_obs, feriados, hoy)
            if r:
                symbol, payload = r
                payload["symbol"] = symbol
                payload["ts"] = datetime.now(timezone.utc).isoformat()
                sb.table("prices").upsert(payload).execute()   # merge: no pisa last/bid
        except Exception as e:
            print(f"[ERROR] {inst.get('symbol','?')}: {e}")

def main():
    # --once: una sola pasada y sale. Es como lo invoca run.py, y es lo que
    # permite correr todo desde un scheduler en vez de dejar daemons vivos.
    if "--once" in sys.argv:
        try:
            once()
        except Exception as e:
            print(f"[ERROR] {e}")
            raise SystemExit(1)
        raise SystemExit(0)
    print(f"[TAMAR] Valuación cada {INTERVAL_SEC/60:.0f} min")
    while True:
        try:
            once()
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(INTERVAL_SEC)

if __name__ == "__main__":
    main()   # loop continuo cada INTERVAL_SEC