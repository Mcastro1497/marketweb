#!/usr/bin/env python3
"""
load_instrument_flows_v2.py
Carga el calendario de pagos del terminal a Supabase `instrument_flows_v2`.
Estrategia: TRUNCATE & LOAD (borra todo y carga de cero en cada corrida).

Uso:
    python load_instrument_flows_v2.py calendario-de-pagos.csv --dry-run
    python load_instrument_flows_v2.py calendario-de-pagos.csv
"""
import os, re, argparse, datetime as dt
import pandas as pd

HEADER_ROW = 1  # doble encabezado: fila 0 grupo, fila 1 nombre real

# (nombre EXACTO en el Excel, campo destino, tipo de parseo).
# OJO flujos: usar el bloque "Flujo de fondos c/100 vn" (base 100, NO dividir).
SPEC = [
    ("Ticker",        "symbol",         "text"),
    ("Efectiva",      "fecha_pago",     "date"),
    ("Interés",       "interes",        "num"),    # col 13 (c/100 vn) - ver nota dedup abajo
    ("Amortización",  "amortizacion",   "num"),    # col 14 (c/100 vn)
    ("Total",         "total",          "num"),    # col 15 (c/100 vn)
    ("Mon. pago",     "moneda_pago",    "text"),
    ("Días",          "dias",           "int"),
    ("Tasa de int.",  "cupon",          "pct"),    # tasa del período -> fracción
    ("Valor residual","valor_residual", "pct"),
]

def _na(v): return v is None or (isinstance(v,float) and pd.isna(v)) or str(v).strip()==""
def parse_pct(v):
    if _na(v): return None
    s=str(v).strip()
    return round(float(s.rstrip('%').replace(',',''))/100,8) if s.endswith('%') else round(float(s.replace(',','')),8)
def parse_num(v):
    if _na(v): return None
    return float(str(v).strip().replace(',',''))
def parse_int(v):
    if _na(v): return None
    return int(float(str(v).strip().replace(',','')))
def parse_date(v):
    if _na(v): return None
    return str(v).strip()[:10]
def parse_text(v):
    if _na(v): return None
    return str(v).strip()
PARSERS={"pct":parse_pct,"num":parse_num,"int":parse_int,"date":parse_date,"text":parse_text}


def build_rows(df):
    # El export tiene "Interés/Amortización/Total" repetidos en 2 bloques.
    # pandas los renombra Interés, Interés.1, etc. Tomamos el bloque c/100 vn
    # por POSICIÓN de columna (índices 13,14,15) para no agarrar el bloque "total".
    cols = list(df.columns)
    col_int   = cols[13]   # Flujo c/100 vn · Interés
    col_amort = cols[14]   # Flujo c/100 vn · Amortización
    col_total = cols[15]   # Flujo c/100 vn · Total

    rows, descartados = [], []
    for _, r in df.iterrows():
        sym = parse_text(r["Ticker"])
        if not sym:
            continue
        if "@" in sym:                      # valuaciones raras de 1816
            descartados.append(sym); continue
        rec = {
            "symbol":        sym,
            "fecha_pago":    parse_date(r["Efectiva"]),
            "interes":       parse_num(r[col_int]),
            "amortizacion":  parse_num(r[col_amort]),
            "total":         parse_num(r[col_total]),
            "moneda_pago":   parse_text(r["Mon. pago"]),
            "dias":          parse_int(r["Días"]),
            "cupon":         parse_pct(r["Tasa de int."]),
            "valor_residual":parse_pct(r["Valor residual"]),
        }
        rec = {k:v for k,v in rec.items() if v is not None}
        if not rec.get("fecha_pago"):       # sin fecha de pago no sirve
            continue
        rows.append(rec)
    return rows, descartados


def chequeo_huerfanos(sb, rows, instruments_table="instruments"):
    """Compara los símbolos de los flujos contra instruments_v2 e informa
    qué símbolos del calendario NO existen como bono (flujos huérfanos),
    y qué bonos de instruments_v2 se quedan sin ningún flujo."""
    inst = sb.table(instruments_table).select("symbol").execute().data or []
    inst_syms = set((r.get("symbol") or "").strip() for r in inst if r.get("symbol"))
    flow_syms = set(r["symbol"] for r in rows)

    huerfanos = sorted(flow_syms - inst_syms)   # flujos sin bono en v2
    sin_flujos = sorted(inst_syms - flow_syms)  # bonos en v2 sin ningún flujo

    print(f"\n=== CHEQUEO DE CRUCE contra {instruments_table} ===")
    print(f"Símbolos en instruments_v2: {len(inst_syms)}")
    print(f"Símbolos en el calendario:  {len(flow_syms)}")
    print(f"Cruzan OK:                  {len(flow_syms & inst_syms)}")
    print(f"\n[HUÉRFANOS] {len(huerfanos)} símbolos del calendario NO están en {instruments_table}:")
    print(f"  (sus flujos igual se cargan, pero no van a cruzar con ningún bono)")
    print(f"  {huerfanos}")
    print(f"\n[SIN FLUJOS] {len(sin_flujos)} bonos en {instruments_table} no tienen ningún flujo:")
    print(f"  {sin_flujos}")
    return huerfanos, sin_flujos


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("archivo")
    ap.add_argument("--table", default="instrument_flows")
    ap.add_argument("--instruments-table", default="instruments")
    ap.add_argument("--dry-run", action="store_true")
    args=ap.parse_args()

    if args.archivo.lower().endswith((".xlsx",".xls")):
        df=pd.read_excel(args.archivo, header=HEADER_ROW, dtype=str)
    else:
        df=pd.read_csv(args.archivo, header=HEADER_ROW, dtype=str)

    rows, descartados = build_rows(df)
    print(f"Filas a cargar: {len(rows)}")
    if descartados:
        print(f"Descartados por '@': {len(descartados)} -> {descartados}")
    print("Símbolos únicos:", len(set(r['symbol'] for r in rows)))

    from dotenv import load_dotenv
    from supabase import create_client
    load_dotenv()
    sb=create_client(os.environ["SUPABASE_URL"], os.environ["SERVICE_KEY"])

    # Chequeo de cruce ANTES de cargar (sirve también en dry-run)
    chequeo_huerfanos(sb, rows, args.instruments_table)

    if args.dry_run:
        print("\n[dry-run] no se subió nada.")
        print("Muestra fila 0:", rows[0] if rows else "—")
        return

    # TRUNCATE & LOAD: borrar todo lo existente y cargar de cero
    print("\nBorrando datos existentes...")
    sb.table(args.table).delete().neq("symbol", "__none__").execute()

    BATCH=500
    for i in range(0,len(rows),BATCH):
        chunk=rows[i:i+BATCH]
        sb.table(args.table).insert(chunk).execute()
        print(f"  insertado {i+len(chunk)}/{len(rows)}")
    print("Listo.")


if __name__=="__main__":
    main()