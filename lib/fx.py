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
  1. dolarapi   API pública, gratis y sin key, que expone el mayorista. Da el
                MISMO valor que MAE (1497,00 contra 1497,00) y responde desde
                cualquier lado. Trae fechaActualizacion, o sea que la antigüedad
                se mide en vez de suponerse.
  2. MAE        respaldo. Es la fuente autoritativa, pero sólo contesta desde
                Argentina y necesita API key.
  3. A3500      cierre diario del BCRA, de la tabla series. Último recurso.

VA PRIMERO DOLARAPI, Y NO MAE, A PROPÓSITO. Si cada entorno usa una fuente
distinta —MAE en tu máquina, dolarapi en la nube—, los dos pueden discrepar y
nadie se entera. Con dolarapi primero, local y nube calculan sobre el mismo dato
y el resultado es reproducible. Cuando MAE está disponible se consulta igual, a
modo de control: si difieren, se avisa.

El mayorista cierra a las 15:00, así que después de esa hora el dato deja de
moverse y una antigüedad de varias horas es lo normal, no una falla.

Se prefiere una API pública documentada antes que scrapear HTML: no se rompe con
un cambio de maquetado y está pensada para consumirse.

Siempre se devuelve de dónde salió el dato y de cuándo es, para que el que lo usa
pueda decidir y para que quede asentado.
"""
import json
import os
import urllib.request
from datetime import date, datetime, timezone
from typing import NamedTuple, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from . import series


def _tz_ar():
    """Zona horaria argentina, de LOCAL_TZ. NO se usa la del sistema: en un
    runner de GitHub es UTC, y el chequeo de "¿todavía opera el mayorista?"
    daría mal por tres horas."""
    nombre = os.getenv("LOCAL_TZ", "America/Argentina/Cordoba")
    if ZoneInfo:
        try:
            return ZoneInfo(nombre)
        except Exception:
            pass
    return timezone.utc

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


# El mayorista opera hasta las 15:00 hora argentina. Pasada esa hora el último
# valor es el cierre y no se mueve más, así que "viejo" sólo es sospechoso
# durante la rueda.
CIERRE_MAYORISTA_H = 15
ANTIGUEDAD_ALERTA_MIN = 45


def spot(mae_fn=None) -> Optional[Spot]:
    """Cadena completa. `mae_fn` consulta MAE; se pasa desde afuera para no
    duplicar acá las credenciales y el endpoint que ya tiene dlk.py."""
    s = desde_dolarapi()

    # MAE, cuando está disponible, se usa de CONTROL: si difiere del mayorista
    # público es señal de que una de las dos fuentes se quedó.
    if mae_fn is not None:
        try:
            v = mae_fn()
            if v:
                v = float(v)
                if s and abs(v / s.valor - 1) > 0.002:
                    print(f"[FX] OJO: MAE {v:,.4f} y dolarapi {s.valor:,.4f} difieren "
                          f"{(v / s.valor - 1) * 100:+.2f}%. Se usa MAE, que es la fuente "
                          f"autoritativa.")
                    return Spot(v, "MAE", datetime.now(timezone.utc), None)
                if not s:
                    return Spot(v, "MAE", datetime.now(timezone.utc), None)
        except Exception as e:
            print(f"[FX] MAE no respondió ({str(e)[:60]}); se sigue con dolarapi.")

    if s:
        edad = s.antiguedad_min
        extra = f", hace {edad:.0f} min" if edad is not None else ""
        print(f"[FX] dolarapi mayorista = {s.valor:,.4f}{extra}")
        if edad is not None and edad > ANTIGUEDAD_ALERTA_MIN:
            ahora_ar = datetime.now(_tz_ar())
            if ahora_ar.hour < CIERRE_MAYORISTA_H:
                print(f"[FX] AVISO: el dato tiene {edad:.0f} min y el mayorista todavía "
                      f"opera. Puede estar retrasado.")
        return s

    s = desde_a3500()
    if s:
        print(f"[FX] Sin fuente intradiaria — A3500 del BCRA del {s.fecha} = {s.valor:,.4f}. "
              f"OJO: es un cierre diario y en los dólar linked cortos eso distorsiona "
              f"la TIR varios cientos de puntos básicos.")
        return s
    return None
