"""Series temporales, desde la tabla `series`.

Una fila por (serie, fecha, valor). El valor se guarda CRUDO como lo publica la
fuente y la unidad la declara series_defs: la TAMAR viene en % (23.25 = 23,25%
TNA), el CER como coeficiente, el A3500 en $/USD. La conversión la hace quien
lee, para que el dato guardado siga siendo auditable contra el origen.
"""
from datetime import date

from .db import cliente, leer_todo


def cargar(serie: str, escala: float = 1.0) -> dict:
    """{date: valor}. `escala` divide el valor crudo: 100 para pasar un % a decimal.

    Va paginado sí o sí: PostgREST corta en 1.000 filas sin avisar y la serie de
    CER ya pasó ese límite.
    """
    filas = leer_todo("series", "fecha, valor", [("eq", ("serie", serie)), ("order", ("fecha",))])
    out = {}
    for r in filas:
        v = r.get("valor")
        if v is None:
            continue
        try:
            out[date.fromisoformat(str(r["fecha"])[:10])] = float(v) / escala
        except (ValueError, TypeError):
            pass
    return out


def tamar_tna() -> dict:
    """{date: TNA decimal}."""
    return cargar("tamar_tna", escala=100)


def cer() -> dict:
    """{date: coeficiente CER}."""
    return cargar("cer")


def a3500() -> dict:
    """{date: $/USD}. Histórico del tipo de cambio mayorista de referencia."""
    return cargar("a3500")


def valor_en(serie: dict, d: date):
    """Valor de una fecha; si no hay dato ese día (fin de semana, feriado), el
    último anterior publicado."""
    if d in serie:
        return serie[d]
    previas = [x for x in serie if x <= d]
    return serie[max(previas)] if previas else None


def ultimo(serie: dict, hasta: date | None = None):
    """(fecha, valor) del último dato publicado hasta `hasta` (default: hoy)."""
    tope = hasta or date.today()
    fechas = [d for d in serie if d <= tope]
    if not fechas:
        return None, None
    f = max(fechas)
    return f, serie[f]
