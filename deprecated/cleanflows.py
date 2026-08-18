# cleanup_flows.py
# -*- coding: utf-8 -*-
"""
Limpia flujos vencidos, instrumentos vencidos y sus precios.
Correr manualmente cada tanto (ej: una vez por mes).
"""

import os
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_KEY  = os.getenv("SERVICE_KEY")

sb = create_client(SUPABASE_URL, SERVICE_KEY)

def cleanup_flows(hoy: str) -> int:
    count_res = sb.table("instrument_flows_v2") \
                  .select("id", count="exact") \
                  .lt("fecha_pago", hoy) \
                  .execute()
    total = count_res.count or 0

    if total == 0:
        print(f"[FLOWS] Sin flujos vencidos para borrar.")
        return 0

    print(f"[FLOWS] {total} flujos con fecha_pago < {hoy} — borrando...")
    sb.table("instrument_flows_v2").delete().lt("fecha_pago", hoy).execute()
    print(f"[FLOWS] ✓ {total} flujos eliminados.")
    return total

def cleanup_instruments(hoy: str) -> int:
    rows = sb.table("instruments_v2") \
             .select("symbol, vencimiento") \
             .lt("vencimiento", hoy) \
             .execute().data or []

    if not rows:
        print(f"[INSTR] Sin instrumentos vencidos para borrar.")
        return 0

    symbols = [r["symbol"] for r in rows]
    print(f"[INSTR] {len(symbols)} instrumentos vencidos — borrando prices y instruments...")

    sb.table("prices").delete().in_("symbol", symbols).execute()
    print(f"[INSTR] Prices eliminados.")

    sb.table("instruments_v2").delete().in_("symbol", symbols).execute()
    muestra = symbols[:5]
    extra = f"... y {len(symbols)-5} más" if len(symbols) > 5 else ""
    print(f"[INSTR] ✓ {len(symbols)} instrumentos eliminados: {muestra}{extra}")
    return len(symbols)

if __name__ == "__main__":
    hoy = datetime.now(timezone.utc).date().isoformat()
    print(f"\n{'='*50}")
    print(f"CLEANUP — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    f = cleanup_flows(hoy)
    print()
    i = cleanup_instruments(hoy)

    print(f"\n{'='*50}")
    print(f"Resumen: {f} flujos + {i} instrumentos eliminados.")
    print(f"{'='*50}\n")