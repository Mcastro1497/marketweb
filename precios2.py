# precios_v2.py
# -*- coding: utf-8 -*-
"""
Ingestor de precios desde ECO/BYMA vía WebSocket (versión v2).

Cambios respecto de precios.py:
- Lee de instruments_v2 (symbol, segment, ticker_usd).
- Suscribe DOS símbolos por bono: el de pesos (symbol) y el USD (ticker_usd).
- MEP global = last(AL30) / last(AL30D), único para todos. Si falta en un ciclo,
  usa el último MEP conocido.
- Escribe UNA fila por bono en `prices`, bajo el symbol en pesos, con:
    last/bid/ask  -> del ticker USD (ticker_usd)
    price_ars     -> del ticker en pesos (symbol)
    fx_mep        -> MEP global
    price_ars_usd -> price_ars / fx_mep   (columna nueva)
    closing_price, change_pct -> como antes (sobre el precio en pesos)

OJO: no correr junto con precios.py (ambos escriben en `prices`).
"""

import argparse
import os, time, signal, threading
from datetime import datetime, timezone, time as dtime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None
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
INSTRUMENTS_TABLE = os.getenv("INSTRUMENTS_TABLE", "instruments")
PRICES_TABLE      = os.getenv("PRICES_TABLE", "prices")

MEP_PESOS = "AL30"
MEP_USD   = "AL30D"

sb = create_client(SUPABASE_URL, SERVICE_KEY)

# ── Helpers ───────────────────────────────────────────────
def extract_ticker(full_symbol: str) -> str:
    parts = [p.strip() for p in (full_symbol or "").split(" - ")]
    return parts[2] if len(parts) >= 3 and parts[2] else (parts[0] if parts else full_symbol)

def init_eco():
    pyRofex._set_environment_parameter("url", ECO_BASE + "/", pyRofex.Environment.LIVE)
    pyRofex._set_environment_parameter("ws",  ECO_WS   + "/", pyRofex.Environment.LIVE)
    r = requests.post(ECO_BASE + "/login",
                      json={"username": PRIMARY_USER, "password": PRIMARY_PASS}, timeout=8)
    r.raise_for_status()
    pyRofex.initialize(user=PRIMARY_USER, password=PRIMARY_PASS,
                       account=PRIMARY_ACCT, environment=pyRofex.Environment.LIVE)
    print("[OK] Conectado a ECO")

# ── Cargar instrumentos (con ticker_usd) ──────────────────
def load_instruments():
    rows = (sb.table(INSTRUMENTS_TABLE)
              .select("symbol, segment, ticker_usd, instrument_type")
              .eq("is_active", True).execute().data) or []
    print(f"[DB] {len(rows)} instrumentos activos en {INSTRUMENTS_TABLE}")
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
    return by_ticker

def pick_symbol(by_ticker, tk, seg):
    """Elige el símbolo completo MERV para un ticker, preferyendo el segmento."""
    candidates = by_ticker.get(tk, [])
    if not candidates:
        return None
    return (next((c for c in candidates if c.endswith(f" - {seg}")), None)
            or next((c for c in candidates if c.endswith(f" - {DEFAULT_SEG}")), None)
            or candidates[0])

# ── Upsert ────────────────────────────────────────────────
def upsert_price(symbol, last, bid, ask, price_ars, fx_mep, closing_price, change_pct, price_ars_usd):
    sb.table(PRICES_TABLE).upsert({
        "symbol":        symbol,
        "last":          last,
        "bid":           bid,
        "ask":           ask,
        "price_ars":     price_ars,
        "fx_mep":        fx_mep,
        "closing_price": closing_price,
        "change_pct":    change_pct,
        "price_ars_usd": price_ars_usd,
        "ts":            datetime.now(timezone.utc).isoformat()
    }).execute()

# ── Estado compartido ─────────────────────────────────────
_lock   = threading.Lock()
_stop   = threading.Event()
_latest = {}                       # ticker_limpio -> dict de datos
_ref    = {MEP_PESOS: None, MEP_USD: None}
_pushed = {}
_last_mep = {"value": None}        # último MEP conocido (fallback)

# pares: symbol(pesos) -> ticker_usd. Se llena en main().
_pairs = {}
# símbolos HD/ON: solo ellos calculan variación en USD. Se llena en main().
_hd_on = set()

# ── WS handler ────────────────────────────────────────────
def market_data_handler(msg: dict):
    full = (msg.get("instrumentId") or {}).get("symbol")
    md   = msg.get("marketData") or {}
    last = (md.get("LA") or {}).get("price")
    bid  = md.get("BI")[0].get("price") if isinstance(md.get("BI"), list) and md["BI"] else None
    ask  = md.get("OF")[0].get("price") if isinstance(md.get("OF"), list) and md["OF"] else None
    cl   = md.get("CL")
    closing = cl.get("price") if isinstance(cl, dict) else (cl if isinstance(cl, (int,float)) else None)
    if not full:
        return
    tk = extract_ticker(full)
    with _lock:
        prev = _latest.get(tk, {})
        _latest[tk] = {
            "last": float(last) if last is not None else prev.get("last"),
            "bid":  float(bid)  if bid  is not None else None,
            "ask":  float(ask)  if ask  is not None else None,
            "closing": float(closing) if closing is not None else prev.get("closing"),
        }
        if tk in _ref and last is not None:
            _ref[tk] = float(last)

def error_handler(msg):   print("WS Error:", msg)
def exception_handler(e): print("WS Exception:", getattr(e, "msg", str(e)))

# ── Push loop ─────────────────────────────────────────────
def pusher_loop():
    while not _stop.is_set():
        now = time.time()
        with _lock:
            # MEP global del ciclo (con fallback al último conocido)
            al30, al30d = _ref.get(MEP_PESOS), _ref.get(MEP_USD)
            if al30 and al30d and al30d > 0:
                mep = al30 / al30d
                _last_mep["value"] = mep
            else:
                mep = _last_mep["value"]      # último conocido
            snapshot = dict(_latest)
            pairs = dict(_pairs)

        for sym_pesos, tk_usd in pairs.items():
            d_pesos = snapshot.get(sym_pesos)
            d_usd   = snapshot.get(tk_usd) if tk_usd else None
            if not d_pesos:
                continue
            if (now - _pushed.get(sym_pesos, 0)) < PUSH_INTERVAL_SEC:
                continue

            price_ars = d_pesos.get("last")
            # last/bid/ask: del USD si lo tenemos; si no, queda None
            last = d_usd.get("last") if d_usd else None
            bid  = d_usd.get("bid")  if d_usd else None
            ask  = d_usd.get("ask")  if d_usd else None
            # Variación: SOLO HD/ON la calculan en USD (last ticker D / closing ticker D),
            # porque su precio mostrado es el USD. El resto (CER/FIJA/TAMAR/DLK) en pesos.
            closing_usd  = d_usd.get("closing") if d_usd else None
            closing_peso = d_pesos.get("closing")
            chg = None
            closing = None
            if sym_pesos in _hd_on and last is not None and closing_usd and float(closing_usd) > 0:
                closing = float(closing_usd)
                try: chg = (last / closing) - 1.0
                except: pass
            elif price_ars is not None and closing_peso and float(closing_peso) > 0:
                closing = float(closing_peso)
                try: chg = (price_ars / closing) - 1.0
                except: pass
            price_ars_usd = (price_ars / mep) if (price_ars is not None and mep) else None

            try:
                upsert_price(sym_pesos, last, bid, ask, price_ars, mep, closing, chg, price_ars_usd)
                _pushed[sym_pesos] = now
                pu = f"{price_ars_usd:,.2f}" if price_ars_usd is not None else "N/A"
                print(f"[PUSH] {sym_pesos:8s} ARS={price_ars}  USD_last={last}  ARSusd={pu}  MEP={mep}")
            except Exception as e:
                print(f"[ERR] upsert {sym_pesos}: {e}")
        _stop.wait(1.0)

# ── Main ──────────────────────────────────────────────────
def _tz():
    """Zona horaria local, de LOCAL_TZ en el .env. Misma convención que dlk.py."""
    nombre = os.getenv("LOCAL_TZ", "America/Argentina/Cordoba")
    if ZoneInfo:
        try:
            return ZoneInfo(nombre)
        except Exception:
            pass
    return timezone.utc


def _tz_name():
    return os.getenv("LOCAL_TZ", "UTC")


def _now_local():
    return datetime.now(_tz())


def _parse_hora(txt):
    """'17:15' -> time(17, 15). La hora es LOCAL (LOCAL_TZ del .env)."""
    h, m = txt.split(":")
    return dtime(int(h), int(m))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Precios en tiempo real desde ECO")
    ap.add_argument("--hasta", metavar="HH:MM",
                    help="apagarse solo a esta hora local. Convierte el proceso en un "
                         "job programable: arranca a la apertura, vive la rueda y "
                         "termina al cierre, sin quedar como daemon permanente.")
    _args = ap.parse_args()
    _limite = _parse_hora(_args.hasta) if _args.hasta else None

    running = True
    def stop_handler(sig, frame):
        global running
        print("\nDeteniendo..."); running = False; _stop.set()
    signal.signal(signal.SIGINT, stop_handler)
    # SIGTERM además de SIGINT: es la señal que manda un scheduler o un
    # contenedor al parar, y sin esto el websocket quedaría sin cerrar.
    signal.signal(signal.SIGTERM, stop_handler)

    init_eco()
    instruments = load_instruments()
    by_ticker   = fetch_merv_symbols()

    symbols_ws = set()
    # asegurar AL30 / AL30D para el MEP
    for tk in (MEP_PESOS, MEP_USD):
        s = pick_symbol(by_ticker, tk, DEFAULT_SEG)
        if s: symbols_ws.add(s)

    for row in instruments:
        sym = (row.get("symbol") or "").strip()
        seg = row.get("segment") or DEFAULT_SEG
        tk_usd = (row.get("ticker_usd") or "").strip() or None
        _pairs[sym] = tk_usd
        if (row.get("instrument_type") or "").upper() in ("HD", "ON"):
            _hd_on.add(sym)
        s_pesos = pick_symbol(by_ticker, sym, seg)
        if s_pesos: symbols_ws.add(s_pesos)
        if tk_usd:
            s_usd = pick_symbol(by_ticker, tk_usd, seg)
            if s_usd: symbols_ws.add(s_usd)

    symbols_ws = list(symbols_ws)
    print(f"[WS] Suscribiendo {len(symbols_ws)} símbolos (pesos + USD)")

    entries = [pyRofex.MarketDataEntry.LAST, pyRofex.MarketDataEntry.BIDS,
               pyRofex.MarketDataEntry.OFFERS, pyRofex.MarketDataEntry.CLOSING_PRICE]
    pyRofex.init_websocket_connection(market_data_handler=market_data_handler,
                                      error_handler=error_handler,
                                      exception_handler=exception_handler)
    pyRofex.market_data_subscription(tickers=symbols_ws, entries=entries)

    t = threading.Thread(target=pusher_loop, daemon=True); t.start()
    if _limite:
        print(f"[WS] Se apaga solo a las {_limite.strftime('%H:%M')} ({_tz_name()})")
    try:
        while running:
            if _limite and _now_local().time() >= _limite:
                print(f"\n[WS] Hora de cierre ({_limite.strftime('%H:%M')}), terminando.")
                break
            time.sleep(0.5)
    finally:
        _stop.set(); t.join(timeout=2.0)
        pyRofex.close_websocket_connection()
        print("WS cerrado.")