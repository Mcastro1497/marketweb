#!/usr/bin/env python
"""Mantiene la tabla `holidays` con el calendario bancario argentino.

POR QUÉ NO ALCANZA CON LA API
-----------------------------
argentinadatos.com publica el calendario NACIONAL (fuente: La Nación). Los
bancos cierran además días que no son feriado nacional, y esos días el BCRA no
publica A3500 ni TAMAR. Verificado el 2026-08-21 contra las dos series: en 2025
faltaban 04-17 (Jueves Santo), 11-06 (Día del Bancario), 12-24 y 12-31 (asueto
bancario) — cuatro días que la API no trae.

No es cosmético. motor_tamar promedia las TNA publicadas en la ventana del bono;
un día hábil sin dato se contaba como futuro y se proyectaba a la tasa esperada.
Con 16 feriados de 2025 sin cargar, a TTS26 le sacaba 13 puntos de TEA.

CÓMO SE ARMA
------------
1. Feriados nacionales de la API, por año.
2. Los bancarios, que son regla fija: 6/11 (Día del Bancario), 24/12 y 31/12
   (asueto), y el Jueves Santo (Pascua menos 3 días).
3. Se contrasta contra los días hábiles en que NI a3500 NI tamar_tna tienen dato.
   Si aparece uno que no está en el calendario, se avisa: o es un feriado nuevo
   o es un bache del sync de series, y las dos cosas hay que mirarlas.

    python feriados_sync.py                # del año pasado a dentro de 3
    python feriados_sync.py 2025 2030
    python feriados_sync.py --dry-run
"""
import sys
import urllib.request
import json
from datetime import date, timedelta

from lib.db import cliente

API = "https://api.argentinadatos.com/v1/feriados/{anio}"
TIMEOUT = 15


def pascua(anio: int) -> date:
    """Domingo de Pascua (algoritmo gregoriano anónimo)."""
    a = anio % 19
    b, c = divmod(anio, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(anio, mes, dia + 1)


def bancarios(anio: int) -> list[tuple[date, str]]:
    """Los que cierran bancos y NO son feriado nacional."""
    return [
        (pascua(anio) - timedelta(days=3), "Jueves Santo"),
        (date(anio, 11, 6), "Día del Bancario"),
        (date(anio, 12, 24), "Asueto bancario"),
        (date(anio, 12, 31), "Asueto bancario"),
    ]


def desde_api(anio: int) -> list[tuple[date, str]]:
    req = urllib.request.Request(API.format(anio=anio),
                                 headers={"User-Agent": "marketweb/1.0",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        datos = json.loads(r.read().decode("utf-8"))
    out = []
    for x in datos:
        try:
            out.append((date.fromisoformat(x["fecha"]), x.get("nombre") or "Feriado"))
        except (KeyError, ValueError):
            continue
    return out


def _series_sin_dato(desde: date, hasta: date, cal: set) -> list[date]:
    """Días hábiles (según `cal`) sin dato en a3500 NI en tamar_tna."""
    sb = cliente()
    tiene = {}
    for serie in ("a3500", "tamar_tna"):
        vistos, off = set(), 0
        while True:
            filas = (sb.table("series").select("fecha").eq("serie", serie)
                     .gte("fecha", desde.isoformat()).lte("fecha", hasta.isoformat())
                     .order("fecha").range(off, off + 999).execute().data or [])
            vistos |= {date.fromisoformat(f["fecha"]) for f in filas}
            if len(filas) < 1000:
                break
            off += 1000
        tiene[serie] = vistos
    faltan, d = [], desde
    while d <= hasta:
        if d.weekday() < 5 and d not in cal:
            if d not in tiene["a3500"] and d not in tiene["tamar_tna"]:
                faltan.append(d)
        d += timedelta(days=1)
    return faltan


def sincronizar(desde_anio: int, hasta_anio: int, dry_run: bool = False) -> int:
    sb = cliente()
    ya = {f["holiday_date"] for f in
          (sb.table("holidays").select("holiday_date").execute().data or [])}

    cal: dict = {}
    for anio in range(desde_anio, hasta_anio + 1):
        try:
            api = desde_api(anio)
        except Exception as e:
            print(f"  [!] {anio}: la API falló ({str(e)[:70]}). Se cargan sólo los bancarios.")
            api = []
        for f, nombre in api + bancarios(anio):
            cal.setdefault(f, nombre)          # el nacional gana sobre el bancario
        print(f"  {anio}: {len(api)} de la API + {len(bancarios(anio))} bancarios")

    nuevos = [{"holiday_date": f.isoformat(), "name": n, "country": "AR"}
              for f, n in sorted(cal.items()) if f.isoformat() not in ya]

    print(f"\n  en la tabla: {len(ya)}  ·  calculados: {len(cal)}  ·  a insertar: {len(nuevos)}")
    for x in nuevos:
        print(f"     + {x['holiday_date']}  {x['name']}")

    if nuevos and not dry_run:
        sb.table("holidays").insert(nuevos).execute()
        print(f"  insertados {len(nuevos)}.")
    elif dry_run:
        print("  (dry-run: no se escribió nada)")

    # Contraste contra la realidad observada del BCRA.
    completo = {date.fromisoformat(d) for d in ya} | set(cal)
    hoy = date.today()
    ini = max(date(desde_anio, 1, 1), date(2024, 10, 1))
    sin = _series_sin_dato(ini, min(hoy, date(hasta_anio, 12, 31)), completo)
    if sin:
        print(f"\n  [!] {len(sin)} días hábiles sin dato en NINGUNA serie y sin feriado cargado:")
        for d in sin:
            print(f"     ? {d} {d.strftime('%a')}")
        print("      O es un feriado que falta, o el sync de series tiene un bache.")
    else:
        print("\n  Contraste OK: todo día hábil del período tiene dato en alguna serie.")
    return len(nuevos)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    hoy = date.today()
    d = int(args[0]) if args else hoy.year - 1
    h = int(args[1]) if len(args) > 1 else hoy.year + 3
    print(f"Feriados {d}–{h}{'  (dry-run)' if dry else ''}\n")
    sincronizar(d, h, dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
