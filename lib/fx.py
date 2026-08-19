"""Tipo de cambio oficial mayorista, con cadena de fuentes explícita.

EL PROBLEMA
dlk.py pedía el spot a MAE y, si fallaba, usaba el último valor guardado. Desde
un runner de GitHub MAE devuelve 403 —su WAF bloquea el rango, y no se arregla
con headers: probamos User-Agent de navegador y sigue igual—, así que el fallback
se activaba en cada corrida en la nube y el FX quedaba congelado.

Y no es un detalle. Un error de 0,19% en el tipo de cambio, sobre un dólar linked
al que le quedan 12 días, se anualiza a casi 6 puntos:

    D31G6  (vence en 12 días)   14,51% con el FX bueno   7,44% con el de ayer
    D30S6  (vence en 42 días)    8,27%                   6,43%
    TZV28  (vence en 2026)      10,28%                  10,17%

Cuanto más corto el bono, más brutal. Publicar la mitad de la TIR de un bono
corto es peor que no publicarla.

LA CADENA
  1. MAE        intradiario y autoritativo, pero sólo responde desde Argentina.
  2. dolarapi   API pública, gratis y sin key, que expone el mayorista. Hoy da
                1497,00 contra los 1497,00 de MAE: coincidencia exacta. Responde
                desde cualquier lado, así que es la que salva la nube. Trae
                fechaActualizacion, o sea que se puede medir cuán viejo es.
  3. A3500      cierre diario del BCRA, de la tabla series. Último recurso.

Se prefiere una API pública documentada antes que scrapear HTML: no se rompe con
un cambio de maquetado y está pensada para consumirse.

Siempre se devuelve de dónde salió el dato y de cuándo es, para que el que lo usa
pueda decidir y para que quede asentado.
"""
import json
import urllib.request
from datetime import date, datetime, timezone
from typing import NamedTuple, Optional

from . import series

DOLARAPI_URL = "https://dolarapi.com/v1/dolares/mayorista"
TIMEOUT = 10


class Spot(NamedTuple):
    valor: float
    fuente: str                    # 'MAE' | 'dolarapi' | 'A3500'
    momento: Optional[datetime]    # cuándo se actualizó el dato en el origen
    fecha: Optional[date]          # sólo para A3500, que es un cierre diario

    @property
    def antiguedad_min(self) -> Optional[float]:
        if self.momento is None:
            return None
        return (datetime.now(timezone.utc) - self.momento).total_seconds() / 60


def desde_dolarapi() -> Optional[Spot]:
    """Mayorista de dolarapi.com. Es la fuente que funciona desde la nube."""
    try:
        req = urllib.request.Request(
            DOLARAPI_URL, headers={"User-Agent": "marketweb/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.load(r)
        v = d.get("venta")
        if not v or float(v) <= 0:
            return None
        momento = None
        fa = d.get("fechaActualizacion")
        if fa:
            try:
                momento = datetime.fromisoformat(str(fa).replace("Z", "+00:00"))
            except ValueError:
                pass
        return Spot(float(v), "dolarapi", momento, None)
    except Exception as e:
        print(f"[FX] dolarapi falló: {str(e)[:80]}")
        return None


def desde_a3500() -> Optional[Spot]:
    """Último A3500 publicado por el BCRA, de la tabla series."""
    f, v = series.ultimo(series.a3500())
    if not v:
        return None
    momento = datetime.combine(f, datetime.min.time(), tzinfo=timezone.utc) if f else None
    return Spot(v, "A3500", momento, f)


def spot(mae_fn=None) -> Optional[Spot]:
    """Cadena completa. `mae_fn` consulta MAE; se pasa desde afuera para no
    duplicar acá las credenciales y el endpoint que ya tiene dlk.py."""
    if mae_fn is not None:
        try:
            v = mae_fn()
            if v:
                return Spot(float(v), "MAE", datetime.now(timezone.utc), None)
        except Exception as e:
            print(f"[FX] MAE falló: {str(e)[:80]}")

    s = desde_dolarapi()
    if s:
        edad = s.antiguedad_min
        extra = f", actualizado hace {edad:.0f} min" if edad is not None else ""
        print(f"[FX] MAE no disponible — dolarapi mayorista = {s.valor:,.4f}{extra}")
        return s

    s = desde_a3500()
    if s:
        print(f"[FX] Sin fuente intradiaria — A3500 del BCRA del {s.fecha} = {s.valor:,.4f}. "
              f"OJO: es un cierre diario y en los dólar linked cortos eso distorsiona "
              f"la TIR varios cientos de puntos básicos.")
        return s
    return None
