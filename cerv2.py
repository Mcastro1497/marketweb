# cerv2.py
# -*- coding: utf-8 -*-
"""
Engine de soberanos ARS (CER / FIJA). Reemplaza a cer.py (desestimado).

Hace TODO:
  - Lee el CER de `series` (lo sincroniza series_sync.py).
  - Valúa cada bono (XIRR/duration/TNA) y escribe prices (ytm, duration_y, tna,
    cer_fixed).  Para CER ajusta el precio por el coeficiente CER/cer_emision.
  - Lógica de "bono fijo": si hoy ya pasó el t-10 del vencimiento, el CER del
    pago final ya está publicado -> el bono es determinístico; se usa el CER del
    vto (no el de hoy) y se marca prices.cer_fixed = true.
  - Mantiene instrument_flows_v3: NO es una réplica completa de v2. Cubre los
    soberanos ARS y las ONs que operan (v2 tiene 75 símbolos más, ilíquidos, que
    se dejan afuera a propósito), con las columnas proyectadas en pesos
    (interes/amortizacion/total _proyectado = base × ratio CER).

Uso:
  .venv/bin/python cerv2.py                 # daemon: cada INTERVAL hace sync + valúa + proyecta v3
  .venv/bin/python cerv2.py --once          # un ciclo completo
  .venv/bin/python cerv2.py --build [--apply]    # siembra v3 desde v2 (base + proyectados)
  .venv/bin/python cerv2.py --project [--apply]  # refresca solo los proyectados de v3
  .venv/bin/python cerv2.py --compare [SYM] # debug: TIR simple vs proyectado
"""
import os, sys, time, requests
from math import isfinite
from datetime import datetime, timezone, time as dtime, date as dtdate
from typing import Optional, Set

import pandas as pd
from supabase import create_client
from dotenv import load_dotenv
load_dotenv()

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

def _get_tz():
    name = os.getenv("LOCAL_TZ", "America/Argentina/Cordoba")
    if ZoneInfo:
        try: return ZoneInfo(name)
        except: pass
    return timezone.utc

LOCAL_TZ     = _get_tz()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_KEY  = os.getenv("SERVICE_KEY")
INTERVAL_SEC = float(os.getenv("INTERVAL_SEC", "300"))
PAGE_SIZE    = int(os.getenv("PAGE_SIZE", "1000"))
CER_LIMIT    = int(os.getenv("CER_SYNC_LIMIT", "60"))
BCRA_URL     = "https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/30"  # idVariable 30 = CER

FLOWS = "instrument_flows"   # base + columnas proyectadas, fusionadas por sql/011
PROY = ("interes_proyectado", "amortizacion_proyectado", "total_proyectado")
BASE = ("interes", "amortizacion", "total")

sb = create_client(SUPABASE_URL, SERVICE_KEY)

APPLY   = "--apply" in sys.argv
SYM_ARG = next((a for a in sys.argv[1:] if not a.startswith("-")), None)


# ── Sync CER ──────────────────────────────────────────────
def sync_cer_from_bcra() -> int:
    """El sync de CER se mudó a series_sync.py, que recorre el catálogo
    series_defs y trae todas las series con el mismo código.

    El de acá además era frágil: hacía delete() de TODA cer_historico y después
    insert(). Si el insert fallaba después del delete, la serie quedaba vacía. Es
    la razón de que sólo hubiera 60 días de historia y de que el CER base de los
    duales haya tenido que buscarse a mano en la API. series_sync.py usa upsert
    incremental con solape, así que no puede perder datos."""
    print("[CER] El sync se hace con series_sync.py (serie 'cer'). Nada que hacer acá.")
    return 0


# ── Calendario ────────────────────────────────────────────
def load_holidays() -> Set[dtdate]:
    rows = sb.table("holidays").select("holiday_date").execute().data or []
    return {pd.to_datetime(r["holiday_date"]).date() for r in rows}

def next_bday(d: pd.Timestamp, hols: Set[dtdate]) -> pd.Timestamp:
    d = d + pd.Timedelta(days=1)
    while d.weekday() >= 5 or d.date() in hols: d += pd.Timedelta(days=1)
    return d

def prev_bday(d: pd.Timestamp, hols: Set[dtdate]) -> pd.Timestamp:
    d = d - pd.Timedelta(days=1)
    while d.weekday() >= 5 or d.date() in hols: d -= pd.Timedelta(days=1)
    return d

def minus_n_bdays(d: pd.Timestamp, n: int, hols: Set[dtdate]) -> pd.Timestamp:
    for _ in range(n): d = prev_bday(d, hols)
    return d

def cutoff_and_val(now_utc, hols):
    t1 = next_bday(pd.Timestamp(now_utc.astimezone(LOCAL_TZ).date()), hols).date()
    val = datetime.combine(t1, dtime(0,0,0), tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    return val, pd.Timestamp(t1), val


# ── XIRR / duration ───────────────────────────────────────
def _yf(d0, d1):
    def norm(d):
        if isinstance(d, pd.Timestamp): d = d.to_pydatetime()
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (norm(d1) - norm(d0)).days / 365.0

def _fdf(r, cfs):
    d0, one = cfs[0][0], 1.0 + r
    if one <= 0: return float("inf"), float("inf")
    f = df = 0.0
    for di, ci in cfs:
        t = _yf(d0, di); f += ci/one**t; df += ci*(-t)*one**(-t-1)
    return f, df

def xirr(cfs, guess=0.10):
    cfs = sorted(cfs, key=lambda x: x[0]); r = guess
    for _ in range(80):
        f, df = _fdf(r, cfs)
        if not (df and abs(df) > 1e-18): break
        rn = r - f/df
        if rn <= -0.9999: break
        if abs(rn - r) < 1e-12: return rn
        r = rn
    fval = lambda x: _fdf(x, cfs)[0]
    a = b = None; lx = ly = None
    for x in [-0.9,-0.5,-0.1,0,0.02,0.05,0.08,0.10,0.15,0.25,0.5,1,2,5,10]:
        y = fval(x)
        if lx is not None and pd.notna(ly) and pd.notna(y) and ly*y <= 0: a, b = lx, x; break
        lx, ly = x, y
    if a is None:
        a, b = 0.0, 10.0; fa, fb = fval(a), fval(b); tries = 0
        while fa*fb > 0 and b < 200 and tries < 12: b *= 2; fb = fval(b); tries += 1
        if fa*fb > 0: return None
    lo, hi = a, b; flo, fhi = fval(lo), fval(hi)
    for _ in range(200):
        m = 0.5*(lo+hi); fm = fval(m)
        if abs(fm) < 1e-10 or (hi-lo) < 1e-12: return m
        if flo*fm <= 0: hi, fhi = m, fm
        else: lo, flo = m, fm
    return m

def macaulay(cfs_pos, r):
    if r <= -0.9999: return None
    pv = sum(cf/(1+r)**t for t, cf in cfs_pos)
    if pv <= 0: return None
    return sum(t*(cf/(1+r)**t) for t, cf in cfs_pos) / pv


# ── Datos ─────────────────────────────────────────────────
def load_cer_value(d: pd.Timestamp) -> Optional[float]:
    rows = (sb.table("series").select("fecha, valor").eq("serie", "cer")
              .lte("fecha", d.strftime("%Y-%m-%d")).order("fecha", desc=True)
              .limit(1).execute().data or [])
    return float(rows[0]["valor"]) if rows and rows[0].get("valor") is not None else None

def all_bonds():
    """Todos los instrumentos activos (para poblar/proyectar v3 completo)."""
    return sb.table("instruments").select("symbol, referencias, cer_emision, vencimiento") \
             .eq("is_active", True).execute().data or []

def ars_instruments():
    """Universo CER/FIJA para valuar (excluye A3500/Tamar) + price_map (ARS)."""
    raw = sb.table("instruments").select("symbol, referencias, cer_emision, vencimiento") \
            .eq("is_active", True).eq("moneda_pago", "ARS").execute().data or []
    instr = []
    for r in raw:
        ref = (r.get("referencias") or "").strip()
        if ref in ("A3500", "Tamar"): continue
        r["tipo"] = "CER" if ref.upper() == "CER" else "FIJA"
        instr.append(r)
    prices = sb.table("prices").select("symbol, price_ars, closing_price").execute().data or []
    price_map = {}
    for p in prices:
        base = p.get("price_ars")
        if base is None or float(base) <= 0: base = p.get("closing_price")
        if base is not None and float(base) > 0: price_map[p["symbol"]] = float(base)
    return instr, price_map

def load_flows(cutoff_utc, symbols):
    if not symbols: return pd.DataFrame(columns=["symbol", "fecha_pago", "total"])
    rows, start = [], 0
    while True:
        data = (sb.table("instrument_flows").select("symbol, fecha_pago, total")
                  .in_("symbol", symbols).gt("fecha_pago", cutoff_utc.date().isoformat())
                  .order("fecha_pago", desc=False).range(start, start+PAGE_SIZE-1).execute().data or [])
        rows.extend(data)
        if len(data) < PAGE_SIZE: break
        start += PAGE_SIZE
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["symbol", "fecha_pago", "total"])
    df["fecha_pago"] = pd.to_datetime(df["fecha_pago"], utc=True, errors="coerce")
    df["total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0)
    return df

def upsert_metrics(symbol, ytm, duration_y, tna, cer_fixed=False, tipo="FIJA"):
    # Este engine escribe DOS convenciones distintas segun el bono:
    #   CER  -> ytm REAL, porque el precio se deflacta por el ratio CER antes
    #           de correr el XIRR sobre los flujos base.
    #   FIJA -> ytm NOMINAL en pesos, precio y flujos sin ajustar.
    # Hay que declararlo o la columna queda ambigua. Ver sql/006_ytm_semantica.sql.
    sb.table("prices").upsert({
        "symbol": symbol, "ytm": ytm, "duration_y": duration_y, "tna": tna,
        "ytm_tipo": "real_cer" if tipo == "CER" else "nominal_ars",
        "cer_fixed": bool(cer_fixed), "ts": datetime.now(timezone.utc).isoformat(),
    }).execute()


# ── CER: fijo / ratio ─────────────────────────────────────
def es_cer(b):
    return (b.get("referencias") or "").strip().upper() == "CER" \
           and b.get("cer_emision") and float(b["cer_emision"]) > 0

def ctx_actual():
    now = datetime.now(timezone.utc)
    hols = load_holidays()
    _, t1_date, _ = cutoff_and_val(now, hols)
    cer_t10 = load_cer_value(minus_n_bdays(t1_date, 10, hols))
    today = pd.Timestamp(now.astimezone(LOCAL_TZ).date())
    return {"cer_t10": cer_t10, "hols": hols, "today": today}

def es_fijo(b, ctx):
    """True si hoy ya pasó el t-10 del vto (CER del pago final publicado)."""
    if not es_cer(b) or not b.get("vencimiento"): return False
    venc_t10 = minus_n_bdays(pd.Timestamp(b["vencimiento"]), 10, ctx["hols"])
    return venc_t10 <= ctx["today"] and load_cer_value(venc_t10) is not None

def ratio_de(b, ctx):
    """CER -> CER/cer_emision (CER del vto si fijo, si no t-10 de hoy); resto -> 1.0."""
    if not es_cer(b): return 1.0
    cer_ref = ctx["cer_t10"]
    if b.get("vencimiento"):
        venc_t10 = minus_n_bdays(pd.Timestamp(b["vencimiento"]), 10, ctx["hols"])
        if venc_t10 <= ctx["today"]:
            cv = load_cer_value(venc_t10)
            if cv: cer_ref = cv  # fijo: CER del vto ya conocido
    return cer_ref / float(b["cer_emision"])


# ── Valuación -> prices ───────────────────────────────────
def valuar_prices() -> int:
    now = datetime.now(timezone.utc)
    hols = load_holidays()
    cut, t1_date, val_utc = cutoff_and_val(now, hols)
    cer_t10 = load_cer_value(minus_n_bdays(t1_date, 10, hols))
    ctx = {"cer_t10": cer_t10, "hols": hols, "today": pd.Timestamp(now.astimezone(LOCAL_TZ).date())}

    instr, price_map = ars_instruments()
    if not instr: print("[WARN] Sin instrumentos ARS"); return 0
    flows = load_flows(cut, [r["symbol"] for r in instr])
    if flows.empty: print("[WARN] Sin flujos ARS futuros"); return 0

    updated, skipped = 0, []
    for row in instr:
        sym, tipo, base = row["symbol"], row["tipo"], price_map.get(row["symbol"])
        if not base: skipped.append((sym, "sin precio")); continue

        if tipo == "CER":
            if not es_cer(row) or not cer_t10:
                skipped.append((sym, "sin CER")); continue
            fixed = es_fijo(row, ctx)
            price = base / ratio_de(row, ctx)   # ratio_de ya usa el CER del vto si está fijo
        else:
            fixed, price = False, base

        g = (flows[flows["symbol"] == sym][["fecha_pago", "total"]]
             .groupby("fecha_pago", as_index=False).agg(total=("total", "sum")).sort_values("fecha_pago"))
        cf = [(val_utc, -price)]
        for _, r2 in g.iterrows():
            amt = float(r2["total"])
            if amt != 0: cf.append((r2["fecha_pago"].to_pydatetime(), amt))
        if len(cf) < 2: skipped.append((sym, "sin CF")); continue

        ytm_val = xirr(cf, 0.10)
        if ytm_val is None or not isfinite(ytm_val): skipped.append((sym, "sin convergencia")); continue
        cfs_pos = [(_yf(val_utc, r2["fecha_pago"].to_pydatetime()), float(r2["total"]))
                   for _, r2 in g.iterrows() if float(r2["total"]) != 0]
        dur = macaulay(cfs_pos, ytm_val)

        tna_val = None
        if tipo == "FIJA":
            pos = g[g["total"].astype(float) != 0]
            if len(pos) == 1:
                pf = float(pos.iloc[0]["total"]); dt = pos.iloc[0]["fecha_pago"].to_pydatetime()
                t = _yf(val_utc, dt)
                if pf > 0 and price > 0 and t > 0: tna_val = ((pf/price) - 1.0) / t

        upsert_metrics(sym, float(ytm_val), float(dur) if dur else None, tna_val,
                       cer_fixed=fixed, tipo=tipo)
        updated += 1
        fx = " [FIJO]" if fixed else ""
        print(f"[OK] {sym:8s} ({tipo}) price={price:,.4f}  XIRR={ytm_val:.4%}  dur={round(dur,3) if dur else None}{fx}")
    if skipped: print(f"[INFO] Saltados ({len(skipped)}): {skipped[:6]}")
    return updated


# ── instrument_flows_v3 (base + proyectados) ──────────────
def build(apply=False):
    """Compat: antes sembraba instrument_flows_proyectados como copia de la tabla
    base. Desde sql/011 las proyecciones viven en columnas de instrument_flows, así
    que no hay nada que sembrar: proyectar es actualizar 3 columnas in situ."""
    print("[INFO] --build quedó sin efecto: las proyecciones son columnas de "
          "instrument_flows desde sql/011. Se ejecuta --project.")
    return project_once(apply=apply)


def project_once(apply=True):
    """Recalcula las columnas proyectadas de instrument_flows: base × ratio CER.

    Se escribe con upsert en lotes y no con un update por fila. Antes eran ~1.800
    llamadas HTTP por ciclo, una por flujo; ahora son unas pocas. El id va en el
    payload, así que el upsert resuelve por clave primaria y actualiza sólo las
    columnas proyectadas."""
    ctx = ctx_actual()
    if not ctx["cer_t10"]: print("[X] Sin CER(t-10)."); return 0
    bonos = all_bonds(); n = 0; lote = []
    for b in bonos:
        s_ = b["symbol"]; k = ratio_de(b, ctx)
        rows = (sb.table(FLOWS).select("id, interes, amortizacion, total")
                  .eq("symbol", s_).execute().data or [])
        for r in rows:
            fila = {"id": r["id"]}
            fila.update({proy: (None if r.get(base) is None else float(r[base]) * k)
                         for base, proy in zip(BASE, PROY)})
            lote.append(fila); n += 1
    if apply and lote:
        for i in range(0, len(lote), 500):
            sb.table(FLOWS).upsert(lote[i:i+500]).execute()
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] CER(t-10)={ctx['cer_t10']} proyectados: {n} flujos de {len(bonos)} bonos")
    return n


# ── Debug: comparación simple vs proyectado ───────────────
def comparar():
    now = datetime.now(timezone.utc); hols = load_holidays()
    cut, t1_date, val_utc = cutoff_and_val(now, hols)
    ctx = {"cer_t10": load_cer_value(minus_n_bdays(t1_date, 10, hols)), "hols": hols,
           "today": pd.Timestamp(now.astimezone(LOCAL_TZ).date())}
    _, price_map = ars_instruments()
    bonos = [b for b in all_bonds() if es_cer(b) and (not SYM_ARG or b["symbol"] == SYM_ARG)]

    def tir(price, data):
        df = pd.DataFrame(data)
        if df.empty: return None
        df["fecha_pago"] = pd.to_datetime(df["fecha_pago"], utc=True)
        df["total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0)
        g = df.groupby("fecha_pago", as_index=False).agg(total=("total", "sum")).sort_values("fecha_pago")
        cf = [(val_utc, -price)]
        for _, r in g.iterrows():
            if float(r["total"]) != 0: cf.append((r["fecha_pago"].to_pydatetime(), float(r["total"])))
        if len(cf) < 2: return None
        r = xirr(cf, 0.10); return r if (r is not None and isfinite(r)) else None

    def fetch(tbl, s, col):
        d = (sb.table(tbl).select(f"fecha_pago,{col}").eq("symbol", s)
             .gt("fecha_pago", cut.date().isoformat()).order("fecha_pago").execute().data or [])
        return [{"fecha_pago": x["fecha_pago"], "total": x[col]} for x in d]

    print(f"{'symbol':<10} {'TIR simple':>11} {'TIR v3':>11}  estado")
    for b in bonos:
        s = b["symbol"]; price = price_map.get(s)
        if not price: print(f"{s:<10} {'sin precio':>11}"); continue
        ts = tir(price / ratio_de(b, ctx), fetch("instrument_flows", s, "total"))
        tn = tir(price, fetch(V3, s, "total_proyectado"))
        ss = f"{ts:.4%}" if ts is not None else "—"; sn = f"{tn:.4%}" if tn is not None else "—"
        est = "v3 sin proyectar" if tn is None else ("OK" if ts is not None and abs(ts-tn) < 1e-4 else "DIFERENTE")
        print(f"{s:<10} {ss:>11} {sn:>11}  {est}")


def once_full() -> int:
    sync_cer_from_bcra()
    n = valuar_prices()
    project_once(apply=True)
    return n

def main():
    while True:
        try:
            n = once_full()
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}] ARS actualizados: {n}")
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    if "--build" in sys.argv:     build(apply=APPLY)
    elif "--project" in sys.argv: project_once(apply=APPLY)
    elif "--compare" in sys.argv: comparar()
    elif "--once" in sys.argv:    once_full()
    else:                          main()
