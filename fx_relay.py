#!/usr/bin/env python
"""Publica el dólar mayorista de MAE para que lo consuma la nube.

MAE responde 200 desde una IP residencial y 403 desde cualquier datacenter: el
bot-manager de Akamai contesta antes de que la API mire la key. Se verificó con
los runners de GitHub y con una Edge Function de Supabase. Por eso la consulta
la hace esta máquina y el valor se deja en `prices` (symbol UST_MAE), donde
lib/fx.py lo lee como primera fuente de la cadena.

El recorrido intradiario NO se guarda: MAE lo sirve entero por
/api/mercado/datosgrafico, con una entrada por operación y para cualquier fecha
pasada. Lo consume el dashboard directo.

Corre una vez y sale: la repetición la maneja launchd. Fuera del horario del
mayorista no hace nada, para no gastar llamadas ni escribir ruido.

    python fx_relay.py            # una consulta
    python fx_relay.py --forzar   # ignorar el horario (para probar)
"""
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

# dlk lee sys.argv al importarse, así que hay que guardarlo ANTES de blanquearlo
# o el flag se pierde.
FORZAR = "--forzar" in sys.argv
sys.argv = [sys.argv[0]]
import dlk                        # noqa: E402  reusa fetch_fx_mae y el cliente

SYMBOL = "UST_MAE"
APERTURA_H, CIERRE_H = 10, 16     # ART; el mayorista opera 10:00-15:00, con margen


def en_horario() -> bool:
    ahora = datetime.now(dlk.LOCAL_TZ)
    return ahora.weekday() < 5 and APERTURA_H <= ahora.hour < CIERRE_H


def main() -> int:
    if not FORZAR and not en_horario():
        return 0

    # El sitio público trae la rueda entera —apertura, máximo, mínimo, monto
    # negociado—, no sólo el último. La API con key devuelve nada más el precio,
    # así que queda de respaldo por si el público no contesta.
    from lib.fx import desde_marketdata
    spot = desde_marketdata()
    if spot:
        valor, detalle = spot.valor, dict(spot.detalle or {})
    else:
        valor, detalle = dlk.fetch_fx_mae(), {}
    if not valor or valor <= 0:
        print(f"[{datetime.now():%H:%M:%S}] MAE no devolvió precio; no se escribe nada.")
        return 1

    ahora = datetime.now(timezone.utc).isoformat()
    fila = {"symbol": SYMBOL, "last": float(valor), "ts": ahora}
    fila.update(detalle)
    dlk.sb.table("prices").upsert(fila).execute()

    monto = detalle.get("monto_operado")
    extra = f"  monto acum. {monto:,.0f}" if monto else ""
    print(f"[{datetime.now():%H:%M:%S}] MAE UST$T = {valor:,.4f} -> prices.{SYMBOL}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
