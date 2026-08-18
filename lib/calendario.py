"""Feriados y días hábiles.

Estaba duplicado en cinco archivos, y en tamar.py estaba MAL: buscaba la columna
como 'fecha'/'date'/'dia' cuando en la tabla se llama holiday_date, así que el
set de feriados salía vacío y la ventana de "10 días hábiles" corría ignorándolos.
Tener una sola implementación es justamente para que un bug así no pueda vivir en
una copia y no en las otras.
"""
from datetime import date, timedelta
from functools import lru_cache

from .db import cliente


@lru_cache(maxsize=1)
def feriados() -> frozenset:
    """Feriados de la tabla holidays. Cacheado: no cambian durante una corrida."""
    filas = cliente().table("holidays").select("holiday_date").execute().data or []
    out = set()
    for r in filas:
        v = r.get("holiday_date")
        if not v:
            continue
        try:
            out.add(date.fromisoformat(str(v)[:10]))
        except ValueError:
            pass
    return frozenset(out)


def es_habil(d: date) -> bool:
    return d.weekday() < 5 and d not in feriados()


def habil_anterior(d: date, n: int) -> date:
    """n días hábiles hacia atrás. n=10 es la ventana de los prospectos."""
    c = 0
    while c < n:
        d -= timedelta(days=1)
        if es_habil(d):
            c += 1
    return d


def habil_siguiente(d: date) -> date:
    d += timedelta(days=1)
    while not es_habil(d):
        d += timedelta(days=1)
    return d


def rango_habiles(ini: date, fin: date) -> list:
    out, d = [], ini
    while d <= fin:
        if es_habil(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def fecha_liquidacion(hoy: date | None = None) -> date:
    """T+1 hábil. Es la fecha de valuación real: el CER y el FX aplicables se
    cuentan desde acá, no desde hoy."""
    return habil_siguiente(hoy or date.today())
