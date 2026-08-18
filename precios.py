# Precios_v5_supabase.py  (con diagnóstico de suscripción/precio por ticker)
# -*- coding: utf-8 -*-
"""
Ingestor de precios en tiempo real desde ECO/BYMA vía WebSocket.
- Lee tickers activos desde la tabla instruments (Supabase).
- Calcula FX MEP = AL30D / AL30.
- Hace upsert en prices cada PUSH_INTERVAL_SEC segundos.

DEBUG: setear DEBUG_TICKER (env o constante) para rastrear un ticker puntual
a lo largo de instruments -> universo MERV -> suscripción -> llegada de market data.
"""

import os, time, signal, threading
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

import requests
import pyRofex
from supabase import create_client

# ── Config ────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_KEY  = os.getenv("SERVICE_KEY")

PRIMARY_USER = os.getenv("ECO_USER")
PRIMARY_PASS = os.getenv("ECO_PASS")
PRIMARY_ACCT = os.getenv("ECO_ACCT")
ECO_BASE     = os.getenv("ECO_BASE", "https://api.eco.xoms.com.ar")
ECO_WS       = os.getenv("ECO_WS",   "wss://api.eco.xoms.com.ar")

DEFAULT_SEG       = os.getenv("DEFAULT_SEGMENT", "24hs").strip()
PUSH_INTERVAL_SEC = float(os.getenv("PUSH_INTERVAL_SEC", "10"))

# Ticker a rastrear en los logs. Vacío ("") desactiva el diagnóstico.
DEBUG_TICKER = os.getenv("DEBUG_TICKER", "TMF27").strip().upper()

sb = create_client(SUPABASE_URL, SERVICE_KEY)

# ── Helpers ───────────────────────────────────────────────
def extract_ticker(full_symbol: str) -> str:
    parts = [p.strip() for p in (full_symbol or "").split(" - ")]
    return parts[2] if len(parts) >= 3 and parts[2] else parts[0] if parts else full_symbol

def init_eco():
    pyRofex._set_environment_parameter("url", ECO_BASE + "/", pyRofex.Environment.LIVE)
    pyRofex._set_environment_parameter("ws",  ECO_WS   + "/", pyRofex.Environment.LIVE)
    r = requests.post(ECO_BASE + "/login",
                      json={"username": PRIMARY_USER, "password": PRIMARY_PASS}, timeout=8)
    r.raise_for_status()
    pyRofex.initialize(user=PRIMARY_USER, password=PRIMARY_PASS,
                       account=PRIMARY_ACCT, environment=pyRofex.Environment.LIVE)
    print(f"[OK] Conectado a ECO")

# ── Cargar tickers desde instruments ─────────────────────
def load_instruments():
    rows = sb.table("instruments").select("symbol, segment").eq("is_active", True).execute().data or []
    print(f"[DB] {len(rows)} instrumentos activos")
    if DEBUG_TICKER:
        hit = [r for r in rows if (r.get("symbol") or "").upper() == DEBUG_TICKER]
        if hit:
            print(f"[CHK] {DEBUG_TICKER} EN instruments (is_active=true), segment={hit[0].get('segment')}")
        else:
            print(f"[CHK] {DEBUG_TICKER} NO está en instruments activos "
                  f"(¿falta cargarlo o is_active=false?)")
    return rows

def fetch_merv_symbols():
    try:
        resp = pyRofex.get_instruments('by_segments',
                                       market=pyRofex.Market.ROFEX,
                                       market_segment=[pyRofex.MarketSegment.MERV])
        data = resp.get("instruments") or []
    except Exception:
        data = (pyRofex.get_all_instruments() or {}).get("instruments") or []

    by_ticker = {}
    for it in data:
        sym = (it.get("symbol") or "").strip()
        if not sym.startswith("MERV - "): continue
        tk = extract_ticker(sym)
        by_ticker.setdefault(tk, []).append(sym)
    print(f"[instr] {sum(len(v) for v in by_ticker.values())} símbolos MERV disponibles")

    if DEBUG_TICKER:
        if DEBUG_TICKER in by_ticker:
            print(f"[CHK] {DEBUG_TICKER} EN universo MERV: {by_ticker[DEBUG_TICKER]}")
        else:
            # buscar parecidos por si vino con otro sufijo/typo
            similares = [tk for tk in by_ticker if DEBUG_TICKER[:4] in tk][:10]
            print(f"[CHK] {DEBUG_TICKER} NO aparece en el universo MERV de ROFEX. "
                  f"Parecidos: {similares}")

    return by_ticker

# ── Upsert en Supabase ────────────────────────────────────
def upsert_price(symbol, last, bid, ask, price_ars, fx_mep, closing_price, change_pct):
    now_iso = datetime.now(timezone.utc).isoformat()
    sb.table("prices").upsert({
        "symbol":        symbol,
        "last":          last,
        "bid":           bid,
        "ask":           ask,
        "price_ars":     price_ars,
        "fx_mep":        fx_mep,
        "closing_price": closing_price,
        "change_pct":    change_pct,
        "ts":            now_iso
    }).execute()

# ── Estado compartido ─────────────────────────────────────
_lock   = threading.Lock()
_stop   = threading.Event()
_latest = {}
_ref    = {"AL30": None, "AL30D": None}
_pushed = {}

# ── WebSocket handlers ────────────────────────────────────
def market_data_handler(msg: dict):
    full = (msg.get("instrumentId") or {}).get("symbol")
    md   = msg.get("marketData") or {}
    last = (md.get("LA") or {}).get("price")
    bid  = md.get("BI")[0].get("price") if isinstance(md.get("BI"), list) and md["BI"] else None
    ask  = md.get("OF")[0].get("price") if isinstance(md.get("OF"), list) and md["OF"] else None
    cl   = md.get("CL")
    closing = cl.get("price") if isinstance(cl, dict) else (cl if isinstance(cl, (int,float)) else None)

    if not full or last is None:
        # DEBUG: ver si el ticker rastreado llega pero SIN last (solo punta o cierre)
        if DEBUG_TICKER and full and extract_ticker(full) == DEBUG_TICKER:
            print(f"[MD] {DEBUG_TICKER} llega SIN last | keys={list(md.keys())} "
                  f"bid={bid} ask={ask} closing={closing}")
        return

    tk = extract_ticker(full)
    if DEBUG_TICKER and tk == DEBUG_TICKER:
        print(f"[MD] {DEBUG_TICKER} CON last={last} bid={bid} ask={ask} closing={closing}")

    with _lock:
        prev = _latest.get(full, {})
        _latest[full] = {
            "ticker": tk, "last_ars": float(last),
            "bid": float(bid) if bid is not None else None,
            "ask": float(ask) if ask is not None else None,
            "closing": float(closing) if closing is not None else prev.get("closing"),
            "seen_ts": time.time()
        }
        if tk in _ref:
            _ref[tk] = float(last)

def error_handler(msg):     print("WS Error:", msg)
def exception_handler(e):   print("WS Exception:", getattr(e,"msg",str(e)))

# ── Push loop ─────────────────────────────────────────────
def pusher_loop():
    while not _stop.is_set():
        now = time.time()
        with _lock:
            items = list(_latest.items())
        for full, data in items:
            tk  = data["ticker"]
            ars = data["last_ars"]
            bid = data.get("bid")
            ask = data.get("ask")
            clp = data.get("closing")
            chg = None
            if clp and float(clp) > 0:
                try: chg = (ars / float(clp)) - 1.0
                except: pass

            if (now - _pushed.get(tk, 0)) >= PUSH_INTERVAL_SEC:
                al30  = _ref.get("AL30")
                al30d = _ref.get("AL30D")
                fx = al30d / al30 if al30 and al30d and al30 > 0 else None
                try:
                    upsert_price(tk, ars, bid, ask, ars, fx, clp, chg)
                    _pushed[tk] = now
                    chg_txt = f"{chg:.2%}" if chg is not None else "N/A"
                    print(f"[PUSH] {tk:8s} ARS={ars:,.4f}  CHG={chg_txt}  FX={fx}")
                except Exception as e:
                    print(f"[ERR] upsert {tk}: {e}")
        _stop.wait(1.0)

# ── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    running = True
    def stop_handler(sig, frame):
        global running
        print("\nDeteniendo...")
        running = False
        _stop.set()
    signal.signal(signal.SIGINT, stop_handler)

    init_eco()
    instruments = load_instruments()
    by_ticker   = fetch_merv_symbols()

    symbols_ws = []
    not_found  = []
    for row in instruments:
        tk  = row["symbol"]
        seg = row.get("segment") or DEFAULT_SEG
        candidates = by_ticker.get(tk, [])
        if not candidates:
            not_found.append(tk); continue
        pick = next((c for c in candidates if c.endswith(f" - {seg}")), None) or \
               next((c for c in candidates if c.endswith(f" - {DEFAULT_SEG}")), None) or \
               candidates[0]
        symbols_ws.append(pick)

    print(f"[WS] Suscribiendo {len(symbols_ws)} símbolos")
    if not_found:
        print(f"[WARN] Sin símbolo MERV ({len(not_found)}): {not_found}")  # lista completa

    # DEBUG: resumen del ticker rastreado
    if DEBUG_TICKER:
        en_inst = any((r.get("symbol") or "").upper() == DEBUG_TICKER for r in instruments)
        en_merv = DEBUG_TICKER in by_ticker
        suscripto = [s for s in symbols_ws if extract_ticker(s) == DEBUG_TICKER]
        print(f"[CHK] === {DEBUG_TICKER} === instruments={en_inst} | "
              f"MERV={en_merv} {by_ticker.get(DEBUG_TICKER)} | suscripto={suscripto}")
        if not suscripto:
            print(f"[CHK] {DEBUG_TICKER} NO quedó suscripto -> no vas a ver [PUSH] {DEBUG_TICKER}. "
                  f"Revisá los dos puntos de arriba (instruments / universo MERV).")

    entries = [pyRofex.MarketDataEntry.LAST, pyRofex.MarketDataEntry.BIDS,
               pyRofex.MarketDataEntry.OFFERS, pyRofex.MarketDataEntry.CLOSING_PRICE]
    pyRofex.init_websocket_connection(
        market_data_handler=market_data_handler,
        error_handler=error_handler,
        exception_handler=exception_handler
    )
    pyRofex.market_data_subscription(tickers=symbols_ws, entries=entries)

    t = threading.Thread(target=pusher_loop, daemon=True)
    t.start()
    try:
        while running: time.sleep(0.5)
    finally:
        _stop.set()
        t.join(timeout=2.0)
        pyRofex.close_websocket_connection()
        print("WS cerrado.")