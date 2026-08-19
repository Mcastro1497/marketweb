"""
series_sync.py — Sincroniza todas las series temporales desde el BCRA.

Reemplaza a updatetamar.py y al sync de CER que vivía adentro de cerv2.py. En vez
de un script por serie, recorre el catálogo `series_defs` y trae lo que haya.

Sumar una serie nueva no requiere tocar este archivo:

    insert into series_defs (serie, descripcion, fuente, fuente_id, unidad)
    values ('badlar', 'BADLAR bancos privados', 'BCRA', '7', 'pct_tna');

Se guarda el valor CRUDO como lo publica el BCRA. La unidad la declara el
catálogo y la conversión la hace quien lee. Guardar ya convertido esconde el
origen y hace imposible auditar contra la fuente.

Uso:
    python series_sync.py                    # incremental: desde el último dato
    python series_sync.py --dry-run
    python series_sync.py --serie cer
    python series_sync.py --full             # rehace el histórico completo
    python series_sync.py --desde 2024-01-01
    python series_sync.py --loop             # ciclo continuo
"""
import argparse
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SERVICE_KEY"])

BCRA_URL = "https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/{id}"
INTERVAL_SEC = int(os.getenv("SERIES_INTERVAL_SEC", "3600"))
# Cuánto histórico traer la primera vez que se ve una serie.
DIAS_INICIAL = int(os.getenv("SERIES_DIAS_INICIAL", "1100"))   # ~3 años
# Solapamiento al sincronizar incremental: el BCRA a veces corrige días previos.
DIAS_SOLAPE = 5
# El CER se publica POR ADELANTADO: al 19-08-2026 el BCRA ya tenía valores hasta
# el 15-09. Cortar en "hoy" los perdía, y no son decorativos: cerv2 mira el CER de
# 10 días hábiles antes del vencimiento para decidir si un bono ya quedó
# determinístico, así que para los que vencen pronto ese tramo futuro hace falta.
DIAS_ADELANTE = 60


def catalogo(solo=None):
    q = sb.table("series_defs").select("*").eq("activa", True).eq("fuente", "BCRA")
    if solo:
        q = q.eq("serie", solo)
    return sorted(q.execute().data or [], key=lambda r: r["serie"])


def ultima_fecha(serie: str):
    r = (sb.table("series").select("fecha").eq("serie", serie)
           .order("fecha", desc=True).limit(1).execute().data or [])
    return date.fromisoformat(str(r[0]["fecha"])[:10]) if r else None


def bajar(fuente_id: str, desde: date, hasta: date) -> list:
    """La API v4 devuelve results[0].detalle[] con {fecha, valor}."""
    r = requests.get(BCRA_URL.format(id=fuente_id),
                     params={"desde": desde.isoformat(), "hasta": hasta.isoformat(),
                             "limit": 3000},
                     timeout=60, headers={"User-Agent": "Mozilla/5.0",
                                          "Accept": "application/json"})
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        return []
    out = []
    for x in res[0].get("detalle") or []:
        f, v = x.get("fecha"), x.get("valor")
        if f is None or v is None:
            continue
        try:
            out.append({"fecha": str(f)[:10], "valor": float(v)})
        except (TypeError, ValueError):
            pass
    return out


def sync_una(d: dict, args) -> int:
    serie, fid = d["serie"], d["fuente_id"]
    hoy = date.today()
    # Se pide más allá de hoy para no perder lo que la fuente publica adelantado.
    # Si no hay nada futuro, la API simplemente devuelve hasta donde tiene.
    hasta = hoy + timedelta(days=DIAS_ADELANTE)

    if args.desde:
        desde = date.fromisoformat(args.desde)
    elif args.full:
        desde = hoy - timedelta(days=DIAS_INICIAL)
    else:
        ult = ultima_fecha(serie)
        # El solape cubre las correcciones que el BCRA publica sobre días previos.
        # Se resta también DIAS_ADELANTE porque `ult` puede ser una fecha futura.
        base = min(ult, hoy) if ult else hoy - timedelta(days=DIAS_INICIAL)
        desde = base - timedelta(days=DIAS_SOLAPE)

    try:
        datos = bajar(fid, desde, hasta)
    except Exception as e:
        print(f"  [ERROR] {serie}: {str(e)[:80]}")
        return 0
    if not datos:
        print(f"  {serie:12} sin datos nuevos desde {desde}")
        return 0

    filas = [{"serie": serie, "fecha": x["fecha"], "valor": x["valor"],
              "ts": datetime.now(timezone.utc).isoformat()} for x in datos]
    if not args.dry_run:
        # upsert: el solape reescribe los días ya conocidos con el valor corregido.
        for i in range(0, len(filas), 500):
            sb.table("series").upsert(filas[i:i + 500]).execute()

    ult = max(datos, key=lambda x: x["fecha"])
    print(f"  {serie:12} {len(filas):>5} filas desde {desde}  |  último {ult['fecha']} = {ult['valor']}"
          f"  [{d['unidad']}]")
    return len(filas)


def once(args) -> int:
    defs = catalogo(args.serie)
    if not defs:
        print("[SERIES] No hay series activas en series_defs"
              f"{' con serie=' + args.serie if args.serie else ''}")
        return 0
    print(f"[SERIES] {len(defs)} series{' (dry-run)' if args.dry_run else ''}")
    return sum(sync_una(d, args) for d in defs)


def main():
    ap = argparse.ArgumentParser(description="Sync de series temporales del BCRA")
    ap.add_argument("--serie", help="sincronizar sólo esta")
    ap.add_argument("--full", action="store_true", help="rehacer el histórico completo")
    ap.add_argument("--desde", help="fecha de inicio YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="no escribe")
    ap.add_argument("--loop", action="store_true", help=f"ciclo cada {INTERVAL_SEC}s")
    args = ap.parse_args()

    if not args.loop:
        once(args)
        return
    print(f"[SERIES] ciclo cada {INTERVAL_SEC/60:.0f} min")
    while True:
        try:
            once(args)
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
