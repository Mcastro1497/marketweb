# acciones.py
# -*- coding: utf-8 -*-
"""
Precios en vivo de ACCIONES argentinas y CEDEARs (BYMA, vía ECO/pyRofex).
Solo especie 24hs en ARS (una fila por ticker).

Escribe en la tabla `acciones_cedears`:
    ticker, tipo (ACCION/CEDEAR), last, closing_price,
    var_diaria = last/closing_price - 1, ts

Uso:
    .venv/bin/python acciones.py     # daemon: stream-ea precios + variación diaria

Clasificación (ISO 10962 / cficode): ESXXXX = acción, EMXXXX = CEDEAR.
"""
import os, time, threading, signal, requests
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

import pyRofex
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_KEY  = os.getenv("SERVICE_KEY")
ECO_USER = os.getenv("ECO_USER"); ECO_PASS = os.getenv("ECO_PASS"); ECO_ACCT = os.getenv("ECO_ACCT")
ECO_BASE = os.getenv("ECO_BASE", "https://api.eco.xoms.com.ar")
ECO_WS   = os.getenv("ECO_WS",   "wss://api.eco.xoms.com.ar")
PUSH_INTERVAL_SEC = float(os.getenv("PUSH_INTERVAL_SEC", "10"))
TABLE = "acciones_cedears"

sb = create_client(SUPABASE_URL, SERVICE_KEY)

CFI = {"ESXXXX": "ACCION", "EMXXXX": "CEDEAR"}

_latest: dict = {}            # ticker -> {last, closing}
_lock = threading.Lock()
_stop = threading.Event()
_pushed: dict = {}


def extract_ticker(full_symbol: str) -> str:
    parts = [p.strip() for p in (full_symbol or "").split(" - ")]
    return parts[2] if len(parts) >= 3 and parts[2] else (parts[0] if parts else full_symbol)


def init_eco():
    pyRofex._set_environment_parameter("url", ECO_BASE + "/", pyRofex.Environment.LIVE)
    pyRofex._set_environment_parameter("ws",  ECO_WS   + "/", pyRofex.Environment.LIVE)
    requests.post(ECO_BASE + "/login", json={"username": ECO_USER, "password": ECO_PASS}, timeout=8).raise_for_status()
    pyRofex.initialize(user=ECO_USER, password=ECO_PASS, account=ECO_ACCT, environment=pyRofex.Environment.LIVE)
    print("[OK] Conectado a ECO")


def universo():
    """Acciones + CEDEARs, solo 24hs en ARS. Devuelve [(full_symbol, ticker, tipo)]."""
    det = (pyRofex.get_detailed_instruments() or {}).get("instruments") or []
    seen, uniq = set(), []
    for it in det:
        tipo = CFI.get(it.get("cficode"))
        if not tipo or (it.get("currency") or "").upper() != "ARS":
            continue
        full = ((it.get("instrumentId") or {}).get("symbol")) or ""
        if not full.startswith("MERV - ") or not full.endswith(" - 24hs"):
            continue
        tk = extract_ticker(full)
        if tk in seen:
            continue
        seen.add(tk); uniq.append((full, tk, tipo))
    n_acc = sum(1 for _, _, t in uniq if t == "ACCION")
    print(f"[universo] {len(uniq)} tickers 24hs ARS — {n_acc} acciones, {len(uniq)-n_acc} CEDEARs")
    return uniq


# ── WS ────────────────────────────────────────────────────
def market_data_handler(msg: dict):
    full = (msg.get("instrumentId") or {}).get("symbol")
    md   = msg.get("marketData") or {}
    last = (md.get("LA") or {}).get("price")
    cl   = md.get("CL")
    closing = cl.get("price") if isinstance(cl, dict) else (cl if isinstance(cl, (int, float)) else None)
    if not full:
        return
    tk = extract_ticker(full)
    with _lock:
        prev = _latest.get(tk, {})
        _latest[tk] = {
            "last": float(last) if last is not None else prev.get("last"),
            "closing": float(closing) if closing is not None else prev.get("closing"),
        }

def error_handler(msg):   print("WS Error:", msg)
def exception_handler(e): print("WS Exception:", getattr(e, "msg", str(e)))


def subscribe(symbols):
    entries = [pyRofex.MarketDataEntry.LAST, pyRofex.MarketDataEntry.CLOSING_PRICE]
    pyRofex.init_websocket_connection(market_data_handler=market_data_handler,
                                      error_handler=error_handler, exception_handler=exception_handler)
    B = 250
    for i in range(0, len(symbols), B):
        pyRofex.market_data_subscription(tickers=symbols[i:i+B], entries=entries)
        time.sleep(0.3)
    print(f"[WS] Suscriptos {len(symbols)} símbolos")


# ── Push ──────────────────────────────────────────────────
def pusher_loop(tipo_map):
    while not _stop.is_set():
        now = time.time()
        with _lock:
            snap = dict(_latest)
        batch = []
        for tk, d in snap.items():
            if (now - _pushed.get(tk, 0)) < PUSH_INTERVAL_SEC:
                continue
            last = d.get("last"); closing = d.get("closing")
            if last is None:
                continue
            var_d = (last / closing - 1.0) if (closing and closing > 0) else None
            batch.append({
                "ticker": tk, "tipo": tipo_map.get(tk),
                "last": last, "closing_price": closing, "var_diaria": var_d,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            _pushed[tk] = now
        try:
            if batch:
                sb.table(TABLE).upsert(batch).execute()
                print(f"[PUSH] {len(batch)} tickers ({datetime.now():%H:%M:%S})")
        except Exception as e:
            print(f"[ERR] upsert: {e}")
        _stop.wait(1.0)


def main():
    init_eco()
    uni = universo()
    tipo_map = {tk: tipo for _, tk, tipo in uni}
    subscribe([full for full, _, _ in uni])
    t = threading.Thread(target=pusher_loop, args=(tipo_map,), daemon=True)
    t.start()
    running = {"v": True}
    def stop(sig, frame):
        print("\nDeteniendo..."); running["v"] = False; _stop.set()
    signal.signal(signal.SIGINT, stop)
    try:
        while running["v"]:
            time.sleep(0.5)
    finally:
        _stop.set(); t.join(timeout=2.0)


if __name__ == "__main__":
    main()
