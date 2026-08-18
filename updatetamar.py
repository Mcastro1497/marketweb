# sync_tamar.py
# -*- coding: utf-8 -*-
"""
Sincroniza la TAMAR TNA (ID 44) desde la API v4 del BCRA hacia Supabase.
- Primera ejecución: trae los últimos 2 años (730 días)
- Ejecuciones siguientes: solo agrega los nuevos registros (no borra los viejos)
- Corre cada INTERVAL_SEC segundos (default: cada 1 hora)
"""

import os, time, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()

from supabase import create_client

SUPABASE_URL  = os.getenv("SUPABASE_URL")
SERVICE_KEY   = os.getenv("SERVICE_KEY")
INTERVAL_SEC  = float(os.getenv("TAMAR_INTERVAL_SEC", "3600"))  # cada 1 hora
TAMAR_ID      = 44
TAMAR_INICIAL = int(os.getenv("TAMAR_DIAS_INICIAL", "730"))     # 2 años para primera carga
BCRA_URL      = f"https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/{TAMAR_ID}"

sb = create_client(SUPABASE_URL, SERVICE_KEY)

def get_last_fecha_in_db():
    """Devuelve la última fecha que tenemos en la tabla, o None si está vacía."""
    rows = (sb.table("tamar_historico")
              .select("fecha")
              .order("fecha", desc=True)
              .limit(1)
              .execute().data or [])
    return rows[0]["fecha"] if rows else None

def fetch_tamar_from_bcra(limit: int) -> list:
    """Trae los últimos `limit` registros de TAMAR desde la API del BCRA."""
    try:
        resp = requests.get(
            BCRA_URL,
            params={"Limit": limit},
            timeout=10,
            headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        detalle = resp.json().get("results", [{}])[0].get("detalle", [])
    except Exception as e:
        print(f"[WARN] BCRA TAMAR error: {e}")
        return []
    return detalle

def calcular_tem(tna_pct: float) -> float:
    """Convierte TAMAR TNA a TEM usando la fórmula:
    TEM = ((1 + TNA / (365/32))^(365/32))^(1/12) - 1
    """
    tna = tna_pct / 100
    periodo = 365 / 32
    tem = ((1 + tna / periodo) ** periodo) ** (1 / 12) - 1
    return round(tem * 100, 6)

def sync_tamar() -> int:
    last_fecha = get_last_fecha_in_db()

    if last_fecha is None:
        print(f"[TAMAR] Primera carga — trayendo {TAMAR_INICIAL} días de historia...")
        limit = TAMAR_INICIAL
    else:
        print(f"[TAMAR] Última fecha en DB: {last_fecha} — buscando nuevos registros...")
        limit = 30

    detalle = fetch_tamar_from_bcra(limit)
    if not detalle:
        print("[WARN] TAMAR: sin datos del BCRA")
        return 0

    rows = []
    for item in detalle:
        fecha = str(item.get("fecha", ""))[:10]
        valor = item.get("valor")
        if not fecha or valor is None:
            continue
        if last_fecha and fecha <= last_fecha:
            continue
        try:
            tna = float(valor)
            tem = calcular_tem(tna)
            rows.append({"fecha": fecha, "valor_tna": tna, "valor_tem": tem})
        except:
            pass

    if not rows:
        print("[TAMAR] Sin registros nuevos.")
        return 0

    sb.table("tamar_historico").upsert(rows).execute()
    rows.sort(key=lambda x: x["fecha"], reverse=True)
    print(f"[TAMAR] Sync OK: {len(rows)} registros — último: {rows[0]['fecha']}  TNA={rows[0]['valor_tna']}%  TEM={rows[0]['valor_tem']}%")
    return len(rows)

def main():
    print(f"[TAMAR] Iniciando sync (intervalo: {INTERVAL_SEC/3600:.1f}h)")
    while True:
        try:
            sync_tamar()
        except Exception as e:
            print(f"[ERROR] {e}")
        print(f"[TAMAR] Próxima sync en {INTERVAL_SEC/3600:.1f}h")
        time.sleep(INTERVAL_SEC)

if __name__ == "__main__":
    main()