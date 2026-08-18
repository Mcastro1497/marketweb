# TIR_v5_supabase.py
# -*- coding: utf-8 -*-
"""
Calcula TIR (XIRR) y Duration de Macaulay para ONs y Soberanos HD.
- Lee instruments y flows desde Supabase (tablas unificadas).
- Precio: prices.last (ticker USD); fallback prices.price_ars_usd (peso dolarizado).
- Upsert en prices: ytm, duration_y.
"""

import os, time
from math import isfinite
from datetime import datetime, timezone, time as dtime, date as dtdate
from typing import Dict, List, Optional, Set, Tuple

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
INTERVAL_SEC = float(os.getenv("INTERVAL_SEC", "60"))
DEBUG_TICKER = (os.getenv("DEBUG_TICKER") or "").strip().upper() or None
PAGE_SIZE    = int(os.getenv("PAGE_SIZE", "1000"))

sb = create_client(SUPABASE_URL, SERVICE_KEY)

# ── Calendario ────────────────────────────────────────────
def load_holidays() -> Set[dtdate]:
    rows = sb.table("holidays").select("holiday_date").execute().data or []
    return {pd.to_datetime(r["holiday_date"]).date() for r in rows}

def next_bday(d: pd.Timestamp, hols: Set[dtdate]) -> pd.Timestamp:
    d = d + pd.Timedelta(days=1)
    while d.weekday() >= 5 or d.date() in hols:
        d += pd.Timedelta(days=1)
    return d

def cutoff(now_utc: datetime, hols: Set[dtdate]):
    t1 = next_bday(pd.Timestamp(now_utc.astimezone(LOCAL_TZ).date()), hols).date()
    val = datetime.combine(t1, dtime(0,0,0), tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    return val, val

# ── XIRR ──────────────────────────────────────────────────
def _yf(d0, d1):
    def norm(d):
        if isinstance(d, pd.Timestamp): d = d.to_pydatetime()
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    return (norm(d1)-norm(d0)).days / 365.0

def _fdf(r, cfs):
    d0, one = cfs[0][0], 1.0+r
    if one <= 0: return float("inf"), float("inf")
    f = df = 0.0
    for di, ci in cfs:
        t = _yf(d0, di)
        f  += ci / one**t
        df += ci * (-t) * one**(-t-1)
    return f, df

def xirr(cfs, guess=0.10):
    cfs = sorted(cfs, key=lambda x: x[0])
    r = guess
    for _ in range(80):
        f, df = _fdf(r, cfs)
        if not (df and abs(df)>1e-18): break
        rn = r - f/df
        if rn <= -0.9999: break
        if abs(rn-r) < 1e-12: return rn
        r = rn
    fval = lambda x: _fdf(x,cfs)[0]
    a = b = None; lx = ly = None
    for x in [-0.9,-0.5,-0.1,0,0.02,0.05,0.08,0.10,0.15,0.25,0.5,1,2,5,10]:
        y = fval(x)
        if lx is not None and pd.notna(ly) and pd.notna(y) and ly*y <= 0:
            a,b = lx,x; break
        lx,ly = x,y
    if a is None:
        a,b = 0.0,10.0; fa,fb = fval(a),fval(b); tries=0
        while fa*fb>0 and b<200 and tries<12: b*=2; fb=fval(b); tries+=1
        if fa*fb>0: return None
    lo,hi = a,b; flo,fhi = fval(lo),fval(hi)
    for _ in range(200):
        m=0.5*(lo+hi); fm=fval(m)
        if abs(fm)<1e-10 or (hi-lo)<1e-12: return m
        if flo*fm<=0: hi,fhi=m,fm
        else: lo,flo=m,fm
    return m

def macaulay(cfs_pos, r):
    if r <= -0.9999: return None
    pv = sum(cf/(1+r)**t for t,cf in cfs_pos)
    if pv <= 0: return None
    return sum(t*(cf/(1+r)**t) for t,cf in cfs_pos) / pv

# ── IO Supabase ───────────────────────────────────────────
def load_instruments_and_prices():
    rows = sb.table("instruments_v2") \
             .select("symbol, instrument_type") \
             .eq("is_active", True) \
             .in_("instrument_type", ["ON","HD"]) \
             .execute().data or []
    symbols = [r["symbol"] for r in rows]
    prices  = sb.table("prices").select("symbol, last, price_ars_usd").execute().data or []

    # Precio en USD: 1) last (precio del ticker USD); 2) fallback price_ars_usd (peso dolarizado)
    def precio_usd(p):
        if p.get("last") and float(p["last"]) > 0:
            return float(p["last"])
        if p.get("price_ars_usd") and float(p["price_ars_usd"]) > 0:
            return float(p["price_ars_usd"])
        return None

    price_map = {}
    for p in prices:
        pu = precio_usd(p)
        if pu is not None:
            price_map[p["symbol"]] = pu
    return rows, price_map

def load_flows(cutoff_utc: datetime, symbols: List[str]) -> pd.DataFrame:
    if not symbols: return pd.DataFrame(columns=["symbol","fecha_pago","total"])
    all_rows = []
    chunk_size = 300
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i+chunk_size]
        start = 0
        while True:
            data = (sb.table("instrument_flows_v2")
                      .select("symbol, fecha_pago, total")
                      .in_("symbol", chunk)
                      .gt("fecha_pago", cutoff_utc.date().isoformat())
                      .order("fecha_pago", desc=False)
                      .range(start, start+PAGE_SIZE-1)
                      .execute().data or [])
            all_rows.extend(data)
            if len(data) < PAGE_SIZE: break
            start += PAGE_SIZE

    df = pd.DataFrame(all_rows, columns=["symbol","fecha_pago","total"]) if all_rows else \
         pd.DataFrame(columns=["symbol","fecha_pago","total"])
    df["fecha_pago"] = pd.to_datetime(df["fecha_pago"], utc=True, errors="coerce")
    df["total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0)
    return df

def upsert_metrics(symbol: str, ytm: float, duration_y: Optional[float]):
    # ytm va en DÓLARES: este engine cubre ON y HD, con precio y flujos en USD.
    # Ver sql/006_ytm_semantica.sql.
    sb.table("prices").upsert({
        "ytm_tipo": "usd",
        "symbol":     symbol,
        "ytm":        ytm,
        "duration_y": duration_y,
        "ts":         datetime.now(timezone.utc).isoformat()
    }).execute()

# ── Ciclo ─────────────────────────────────────────────────
def once() -> int:
    now_utc = datetime.now(timezone.utc)
    hols    = load_holidays()
    cut, val_utc = cutoff(now_utc, hols)
    print(f"[INFO] Cutoff T+1 = {cut.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')}")

    rows, price_map = load_instruments_and_prices()
    if not rows: print("[WARN] Sin instrumentos"); return 0

    symbols = [r["symbol"] for r in rows]
    flows   = load_flows(cut, symbols)
    if flows.empty: print("[WARN] Sin flujos futuros"); return 0
    if not price_map: print("[WARN] Sin precios"); return 0

    candidates = sorted(set(symbols) & set(flows["symbol"].unique()))
    print(f"[INFO] {len(candidates)} candidatos con flujos y precio")

    updated = 0
    skipped = []

    for sym in candidates:
        price = price_map.get(sym)
        if not price: skipped.append(sym); continue

        g = (flows[flows["symbol"]==sym][["fecha_pago","total"]]
             .groupby("fecha_pago", as_index=False).agg(total=("total","sum"))
             .sort_values("fecha_pago"))

        cf = [(val_utc, -float(price))]
        for _, row in g.iterrows():
            amt = float(row["total"])
            if amt != 0: cf.append((row["fecha_pago"].to_pydatetime(), amt))
        if len(cf) < 2: continue

        r = xirr(cf, 0.10)
        if r is None or not isfinite(r): continue

        cfs_pos = [(_yf(val_utc, row["fecha_pago"].to_pydatetime()), float(row["total"]))
                   for _, row in g.iterrows() if float(row["total"]) != 0]
        dur = macaulay(cfs_pos, r)

        upsert_metrics(sym, float(r), float(dur) if dur else None)
        updated += 1
        print(f"[OK] {sym:8s} last={price:,.4f}  XIRR={r:.4%}  dur={round(dur,3) if dur else None}")

    if skipped: print(f"[INFO] Sin precio ({len(skipped)}): {skipped[:8]}")
    return updated

def main():
    while True:
        try:
            n = once()
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Actualizados: {n}")
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(INTERVAL_SEC)

if __name__ == "__main__":
    main()
