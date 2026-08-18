"""
rem_sync.py — Baja el REM del BCRA y lo carga en la tabla `rem`.

El REM (Relevamiento de Expectativas de Mercado) es la fuente de proyección de
la pata CER, y sirve de respaldo para las patas TAMAR y DLK. El BCRA lo publica
mensualmente como XLSX con mediana y percentiles.

Cubre tres bloques que nos interesan, cada uno con detalle mensual a ~7 meses y
cifras anuales más allá:
    ipc         var. % mensual / var. % i.a.   -> pata CER
    tamar       TNA %                          -> escenarios alternativos de TAMAR
    tcn         $/USD                          -> pata DLK

La API de estadísticas del BCRA sólo expone la mediana del IPC i.a. a 12 meses
(idVariable 29): un único horizonte, sin percentiles y sin TAMAR ni dólar. Por
eso se parsea el XLSX y no la API.

Uso:
    python rem_sync.py --dry-run          # parsea e imprime, no escribe
    python rem_sync.py                    # parsea y upsertea en `rem`
    python rem_sync.py --mes jul-2026     # un informe puntual
    python rem_sync.py --archivo rem.xlsx # un archivo ya bajado
"""
import argparse
import io
import os
import re
import unicodedata
from datetime import date, datetime, timezone

import pandas as pd
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

URL_TPL = ("https://www.bcra.gob.ar/archivos/Pdfs/PublicacionesEstadisticas/informes/"
           "tablas-relevamiento-expectativas-mercado-{mes}.xlsx")

MESES = ["ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic"]

# Título del bloque en el XLSX -> slug de variable. Se matchea por substring
# sobre el título normalizado (sin acentos, en minúsculas).
BLOQUES = {
    "ipc nivel general":  "ipc",
    "ipc nucleo":         "ipc_nucleo",
    "tasa de interes (tamar)": "tamar",
    "tipo de cambio nominal":  "tcn",
}

COLS = ["periodo", "referencia", "mediana", "promedio", "desvio", "maximo",
        "minimo", "p90", "p75", "p25", "p10", "n_participantes"]

# El XLSX tiene una columna espaciadora vacía: los títulos, el encabezado y los
# datos arrancan todos en la columna 1.
COL0 = 1


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _num(x):
    try:
        v = float(x)
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return None


def clasificar(periodo: str):
    """Devuelve (tipo, fecha_ref). El periodo puede ser una fecha, un año, o un
    horizonte relativo tipo 'próx. 12 meses'."""
    p = _norm(periodo)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(periodo))
    if m:
        return "mensual", date(int(m[1]), int(m[2]), int(m[3]))
    if re.fullmatch(r"\d{4}", p):
        return "anual", date(int(p), 12, 31)
    if "prox" in p:
        return "horizonte", None
    return "otro", None


def parsear(contenido: bytes, fecha_rem: date) -> list:
    df = pd.read_excel(io.BytesIO(contenido), sheet_name="Cuadros de resultados",
                       header=None, dtype=object)
    filas, variable = [], None

    for i in range(len(df)):
        celdas = df.iloc[i].tolist()[COL0:COL0 + len(COLS)]
        c0 = celdas[0] if celdas else None
        vacia = c0 is None or pd.isna(c0)
        txt = "" if vacia else _norm(c0)
        solo_titulo = all(c is None or pd.isna(c) for c in celdas[1:])

        if txt and solo_titulo:
            # Fila de título: abre un bloque de interés o cierra el anterior.
            variable = next((v for k, v in BLOQUES.items() if k in txt), None)
            continue
        if txt.startswith("periodo"):          # fila de encabezado, se saltea
            continue
        if variable is None or vacia:
            continue

        d = dict(zip(COLS, celdas))
        tipo, fecha_ref = clasificar(d["periodo"])
        if tipo == "otro":
            continue

        n = _num(d["n_participantes"])
        filas.append({
            "fecha_rem":  str(fecha_rem),
            "variable":   variable,
            "periodo":    str(d["periodo"])[:10] if tipo == "mensual" else str(d["periodo"]).strip(),
            "tipo":       tipo,
            "referencia": None if pd.isna(d["referencia"]) else str(d["referencia"]).strip(),
            "fecha_ref":  str(fecha_ref) if fecha_ref else None,
            "mediana":    _num(d["mediana"]),
            "promedio":   _num(d["promedio"]),
            "desvio":     _num(d["desvio"]),
            "maximo":     _num(d["maximo"]),
            "minimo":     _num(d["minimo"]),
            "p90":        _num(d["p90"]),
            "p75":        _num(d["p75"]),
            "p25":        _num(d["p25"]),
            "p10":        _num(d["p10"]),
            "n_participantes": int(n) if n is not None else None,
        })
    return filas


def mes_slug(d: date) -> str:
    return f"{MESES[d.month - 1]}-{d.year}"


def bajar(mes: str) -> bytes:
    url = URL_TPL.format(mes=mes)
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.content


def main():
    ap = argparse.ArgumentParser(description="Sync del REM del BCRA")
    ap.add_argument("--mes", help="informe a bajar, formato 'jul-2026' (default: el último disponible)")
    ap.add_argument("--archivo", help="usa un xlsx local en vez de bajarlo")
    ap.add_argument("--dry-run", action="store_true", help="no escribe en la DB")
    args = ap.parse_args()

    if args.archivo:
        contenido = open(args.archivo, "rb").read()
        mes = args.mes or mes_slug(date.today())
    else:
        # Si no se pide un mes, se prueba el actual y se cae al anterior: el
        # informe del mes sale recién promediando el mes siguiente.
        candidatos = []
        hoy = date.today()
        if args.mes:
            candidatos = [args.mes]
        else:
            for k in range(0, 3):
                m = hoy.month - k
                y = hoy.year + (0 if m > 0 else -1)
                candidatos.append(mes_slug(date(y, m if m > 0 else m + 12, 1)))
        contenido, mes = None, None
        for c in candidatos:
            try:
                contenido, mes = bajar(c), c
                print(f"[OK] bajado informe {c}")
                break
            except Exception as e:
                print(f"[--] {c}: {str(e)[:60]}")
        if contenido is None:
            raise SystemExit("no se pudo bajar ningún informe del REM")

    m = re.match(r"([a-z]{3})-(\d{4})", mes)
    fecha_rem = date(int(m[2]), MESES.index(m[1]) + 1, 1)

    filas = parsear(contenido, fecha_rem)
    print(f"[REM {mes}] {len(filas)} filas parseadas")
    for v in sorted({f["variable"] for f in filas}):
        sub = [f for f in filas if f["variable"] == v]
        print(f"\n  ── {v} ({len(sub)} filas)")
        for f in sub:
            n = f["n_participantes"]
            flag = "  ⚠ pocos participantes" if n is not None and n < 10 else ""
            med = f["mediana"]
            print(f"     {f['tipo']:9} {f['periodo']:16} {str(f['referencia'] or ''):18} "
                  f"mediana={med:>10.2f}  p25={f['p25'] or float('nan'):>8.2f} "
                  f"p75={f['p75'] or float('nan'):>8.2f}  n={n}{flag}")

    if args.dry_run:
        print("\n(dry-run, no se escribió nada)")
        return

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SERVICE_KEY"])
    ts = datetime.now(timezone.utc).isoformat()
    for f in filas:
        f["ts"] = ts
    sb.table("rem").upsert(filas).execute()
    print(f"\n[OK] {len(filas)} filas upserteadas en `rem`")


if __name__ == "__main__":
    main()
