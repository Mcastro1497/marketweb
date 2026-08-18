# -*- coding: utf-8 -*-
"""
sync_ticker_usd.py
Arma la equivalencia ticker en pesos -> ticker USD-MEP (sufijo D) consultando
la lista de instrumentos en vivo desde ECO, y actualiza la columna ticker_usd
en instruments_v2 (Supabase).

Regla (derivada de los datos reales del MERV):
  - La especie en USD-MEP termina en 'D' y tiene currency='USD'.
  - La especie en pesos tiene currency='ARS'. Su raíz se obtiene sacando una
    'O' final si la tiene (YMCXO -> YMCX); si no termina en O, la raíz es el
    ticker tal cual (AL30 -> AL30).
  - Para cada bono de instruments_v2, ticker_usd = raíz + 'D'  SI esa especie D
    existe en la lista real de la API. Si no existe, queda NULL.

Solo segmento MERV. La especie D se confirma contra la API (no se inventa).
"""
import os
import pyRofex
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# --- Credenciales ECO (solo desde .env; no hardcodear) ---
ECO_USER = os.getenv("ECO_USER")
ECO_PASS = os.getenv("ECO_PASS")
ECO_ACCT = os.getenv("ECO_ACCT")
if not (ECO_USER and ECO_PASS and ECO_ACCT):
    raise SystemExit("Faltan credenciales ECO en .env (ECO_USER / ECO_PASS / ECO_ACCT)")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_KEY  = os.getenv("SERVICE_KEY")
TABLE        = os.getenv("INSTRUMENTS_TABLE", "instruments")


def ticker_limpio(full):
    parts = [p.strip() for p in (full or "").split(" - ")]
    return parts[2] if len(parts) >= 3 and parts[2] else (parts[0] if parts else full)


def raiz_pesos(ticker):
    """Raíz del ticker en pesos: saca una 'O' final si la tiene."""
    t = ticker.strip().upper()
    return t[:-1] if t.endswith("O") else t


def init_eco():
    pyRofex._set_environment_parameter("url", "https://api.eco.xoms.com.ar/", pyRofex.Environment.LIVE)
    pyRofex._set_environment_parameter("ws",  "wss://api.eco.xoms.com.ar/",  pyRofex.Environment.LIVE)
    pyRofex.initialize(user=ECO_USER, password=ECO_PASS, account=ECO_ACCT,
                       environment=pyRofex.Environment.LIVE)
    print("[OK] Conectado a ECO")


def build_usd_set():
    """Devuelve el set de tickers USD que terminan en 'D' disponibles en MERV."""
    detailed = pyRofex.get_detailed_instruments().get("instruments", [])
    usd_d = set()
    for it in detailed:
        seg = (it.get("segment") or {}).get("marketSegmentId", "")
        if seg != "MERV":
            continue
        tk = ticker_limpio((it.get("instrumentId") or {}).get("symbol", "")).upper()
        if it.get("currency") == "USD" and tk.endswith("D"):
            usd_d.add(tk)
    print(f"[instr] {len(usd_d)} especies USD-D en MERV")
    return usd_d


def main():
    init_eco()
    usd_d = build_usd_set()

    sb = create_client(SUPABASE_URL, SERVICE_KEY)
    rows = sb.table(TABLE).select("symbol").execute().data or []
    print(f"[DB] {len(rows)} instrumentos en {TABLE}")

    updates, sin_equiv = 0, []
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        candidato = raiz_pesos(sym) + "D"      # AL30 -> AL30D ; YMCXO -> YMCX + D = YMCXD
        if candidato in usd_d:
            sb.table(TABLE).update({"ticker_usd": candidato}).eq("symbol", sym).execute()
            updates += 1
        else:
            sin_equiv.append(sym)

    print(f"\n[OK] {updates} equivalencias asignadas")
    if sin_equiv:
        print(f"[INFO] {len(sin_equiv)} sin especie D (quedan NULL): {sin_equiv}")


if __name__ == "__main__":
    main()