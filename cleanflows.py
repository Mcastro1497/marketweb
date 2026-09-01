# -*- coding: utf-8 -*-
"""Borra flujos vencidos, instrumentos vencidos y sus precios.

Reemplaza a deprecated/cleanflows.py, que quedó apuntando a instruments_v2 e
instrument_flows_v2: esas tablas se renombraron sin el sufijo de versión y el
script nunca se actualizó, así que hoy fallaría al primer llamado. Además era
manual ("correr cada tanto, ej: una vez por mes") y en la práctica no corría
nunca — se acumularon instrumentos vencidos hace más de una semana que la web
seguía listando como activos.

El borrado es IRREVERSIBLE. --dry-run muestra qué tocaría sin escribir, y
--backup deja una copia en JSON de todo lo que se va a borrar.
"""

import argparse
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SERVICE_KEY"))

PAGE = 1000


def _todas(tabla, select, filtro):
    """PostgREST corta en 1000 filas por defecto; hay que paginar o el conteo
    miente y el borrado se hace por partes sin que nadie se entere."""
    filas, start = [], 0
    while True:
        q = filtro(sb.table(tabla).select(select))
        data = q.range(start, start + PAGE - 1).execute().data or []
        filas.extend(data)
        if len(data) < PAGE:
            return filas
        start += PAGE


def relevar(hoy: str) -> dict:
    flujos = _todas("instrument_flows", "*", lambda q: q.lt("fecha_pago", hoy))
    instr = _todas("instruments", "*", lambda q: q.lt("vencimiento", hoy))
    symbols = [r["symbol"] for r in instr]
    precios = []
    if symbols:
        for i in range(0, len(symbols), 100):
            lote = symbols[i:i + 100]
            precios += sb.table("prices").select("*").in_("symbol", lote).execute().data or []
    return {"flujos": flujos, "instrumentos": instr, "precios": precios, "symbols": symbols}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="mostrar sin borrar")
    ap.add_argument("--backup", metavar="RUTA", help="guardar en JSON lo que se borra")
    ap.add_argument("--backup-dir", metavar="DIR",
                    help="igual que --backup pero el archivo se nombra por fecha; "
                         "es lo que usa el pipeline, que corre sin supervisión")
    args = ap.parse_args()

    hoy = datetime.now(timezone.utc).date().isoformat()
    print(f"\n{'=' * 56}\nCLEANUP — corte {hoy}\n{'=' * 56}")

    d = relevar(hoy)
    print(f"[FLOWS] {len(d['flujos'])} flujos con fecha_pago < {hoy}")
    print(f"[INSTR] {len(d['instrumentos'])} instrumentos con vencimiento < {hoy}")
    for r in sorted(d["instrumentos"], key=lambda x: x["vencimiento"], reverse=True):
        print(f"          {r['symbol']:8s} vto={r['vencimiento']}")
    print(f"[PRICES] {len(d['precios'])} filas de precios de esos instrumentos")

    destino = args.backup
    if args.backup_dir:
        os.makedirs(args.backup_dir, exist_ok=True)
        destino = os.path.join(args.backup_dir, f"cleanup_{hoy}.json")
    if destino and os.path.exists(destino):
        # Nunca pisar un backup que ya está: si el mismo día corre un --dry-run
        # después del borrado real, el relevamiento da vacío y sobrescribir
        # dejaría un archivo inútil donde estaba la única copia de lo borrado.
        raiz, ext = os.path.splitext(destino)
        n = 2
        while os.path.exists(f"{raiz}_{n}{ext}"):
            n += 1
        destino = f"{raiz}_{n}{ext}"
    if destino:
        with open(destino, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2, default=str)
        print(f"[BACKUP] copia en {destino}")

    if args.dry_run:
        print("\n[DRY-RUN] no se borró nada.\n")
        return

    if d["flujos"]:
        sb.table("instrument_flows").delete().lt("fecha_pago", hoy).execute()
        print(f"[FLOWS] ✓ {len(d['flujos'])} eliminados")
    if d["symbols"]:
        for i in range(0, len(d["symbols"]), 100):
            lote = d["symbols"][i:i + 100]
            sb.table("prices").delete().in_("symbol", lote).execute()
            sb.table("instruments").delete().in_("symbol", lote).execute()
        print(f"[INSTR] ✓ {len(d['symbols'])} instrumentos y sus precios eliminados")

    print(f"\nResumen: {len(d['flujos'])} flujos + {len(d['symbols'])} instrumentos.\n")


if __name__ == "__main__":
    main()
