"""Tipo de cambio oficial, con cadena de fuentes explícita.

El problema que resuelve: dlk.py pedía el spot a MAE y, si fallaba, caía a "el
último valor guardado en prices.UST". Desde un runner de GitHub MAE devuelve 403
—bloquea IPs no argentinas—, así que el fallback se activaba siempre y el FX
quedaba congelado en el último valor que hubiera dejado una corrida local. El paso
figuraba en verde y nada avisaba.

Un fallback a un valor viejo no es un fallback: es un dato incorrecto disfrazado
de dato. Acá la cadena cae a OTRA FUENTE, no a una copia rancia:

    1. MAE           intradiario, es el que más se mueve. Sólo desde Argentina.
    2. A3500 (BCRA)  cierre diario, responde desde cualquier lado, y es el que
                     los prospectos definen como "tipo de cambio aplicable".
    3. error         nunca se devuelve un valor sin fecha conocida.

Siempre se devuelve de dónde salió y de qué fecha es, para que quien lo use pueda
decidir y para que quede asentado.
"""
from datetime import date
from typing import NamedTuple, Optional

from . import series


class Spot(NamedTuple):
    valor: float
    fuente: str          # 'MAE' | 'A3500'
    fecha: Optional[date]  # None en MAE: es intradiario


def desde_a3500() -> Optional[Spot]:
    """Último A3500 publicado por el BCRA, de la tabla series."""
    serie = series.a3500()
    f, v = series.ultimo(serie)
    return Spot(v, "A3500", f) if v else None


def spot(mae_fn=None) -> Optional[Spot]:
    """Cadena completa. `mae_fn` es la función que consulta MAE; se pasa desde
    afuera para no duplicar acá las credenciales y el endpoint que ya tiene dlk.py.
    """
    if mae_fn is not None:
        try:
            v = mae_fn()
            if v:
                return Spot(float(v), "MAE", None)
        except Exception as e:
            print(f"[FX] MAE falló: {str(e)[:80]}")
    s = desde_a3500()
    if s:
        print(f"[FX] MAE no disponible — se usa A3500 del BCRA "
              f"del {s.fecha} = {s.valor:,.4f}")
        return s
    return None
