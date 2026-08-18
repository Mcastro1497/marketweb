"""Cliente de Supabase, único para todo el proceso, y lectura paginada."""
import os
from functools import lru_cache

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

PAGINA = 1000


@lru_cache(maxsize=1)
def cliente() -> Client:
    """Un solo cliente por proceso. Antes cada script creaba el suyo."""
    return create_client(os.environ["SUPABASE_URL"], os.environ["SERVICE_KEY"])


def leer_todo(tabla: str, cols: str = "*", filtros=None) -> list:
    """SELECT paginado.

    PostgREST devuelve como máximo 1.000 filas por defecto y NO avisa: entrega las
    primeras 1.000 del orden pedido como si fueran todas. Eso ya causó un error
    silencioso — al pasar la serie de CER de 60 a 1.100 datos, el "último dato
    observado" quedó tres meses atrás y la TIR real salió ~275 bps abajo sin
    ningún mensaje. Toda lectura de una tabla que pueda crecer va por acá.

    filtros: lista de (metodo, args), p. ej. [("eq", ("serie", "cer"))].
    """
    sb, out, desde = cliente(), [], 0
    while True:
        q = sb.table(tabla).select(cols)
        for metodo, args in (filtros or []):
            q = getattr(q, metodo)(*args)
        datos = q.range(desde, desde + PAGINA - 1).execute().data or []
        out += datos
        if len(datos) < PAGINA:
            return out
        desde += PAGINA
