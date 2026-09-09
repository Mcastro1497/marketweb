#!/usr/bin/env python
"""Revisa que los instrumentos activos tengan los datos que su tipo necesita.

POR QUÉ EXISTE
D10Y7 entró desde el panel con la fila del screener incompleta —sin referencias,
sin VR vigente y sin valor residual— y nadie se enteró hasta que rompió tres
cosas distintas, de a una y con días de diferencia: mostró el VN en la columna
del residual, apareció en la solapa FIJA de soberanos ARS, y quedó a un precio
de que cerv2 lo valuara en pesos mientras dlk.py lo valuaba en dólares, los dos
escribiendo sobre la misma columna.

Ninguna de las tres era difícil de ver el día que se cargó. Lo difícil era
saber que había que mirar.

NO FALLA LA CORRIDA
Reporta y sale con 0. Un hueco de datos no es motivo para tumbar el pipeline: lo
que hace falta es que quede escrito en el log de la corrida, no que nadie pueda
valuar hasta que alguien complete un ISIN.

    python auditar.py            # informe completo
    python auditar.py --breve    # sólo el conteo por chequeo
"""
import argparse
import collections
import sys
from datetime import date

from lib.db import leer_todo

# Campos que todo instrumento necesita, más los que agrega cada tipo.
COMUNES = ["vencimiento", "emision", "moneda_denom", "moneda_pago", "tipo_cupon",
           "valor_residual", "vn_vigente", "vr_vigente", "lamina_min", "emisor",
           "legislacion", "jurisdiccion_pago", "denominacion", "isin",
           "convencion_int", "periodicidad_int"]
POR_TIPO = {"CER": ["cer_emision"], "HD": ["ticker_usd"], "ON": ["ticker_usd"],
            "TAMAR": ["tasa_ref"]}

# La referencia que le corresponde a cada tipo. Es la que mira cerv2 para decidir
# a quién ajusta por CER y a quién saltea, así que un desacuerdo entre esta
# columna y instrument_type se paga en una valuación mal hecha, no en un cartel.
REFERENCIA = {"CER": "CER", "DLK": "A3500", "TAMAR": "Tamar", "DUAL": "Dual"}

# Los duales no tienen flujos en la tabla: se los arma patas.py en memoria.
SIN_FLUJOS_OK = {"DUAL"}


def revisar():
    inst = [r for r in leer_todo("instruments", "*") if r.get("is_active")]
    hoy = date.today().isoformat()
    con_flujo = collections.Counter(
        f["symbol"] for f in leer_todo("instrument_flows", "symbol, fecha_pago",
                                       [("gt", ("fecha_pago", hoy))]))
    con_precio = {p["symbol"] for p in leer_todo("prices", "symbol")}

    huecos = collections.defaultdict(list)      # campo -> [(symbol, tipo)]
    referencia_mal, sin_flujos, sin_precio = [], [], []

    for r in inst:
        t = r.get("instrument_type")
        esperado = list(COMUNES) + POR_TIPO.get(t, [])
        if (r.get("periodicidad_int") or "Nula") != "Nula":
            esperado.append("tasa_int")
        for c in esperado:
            if r.get(c) in (None, ""):
                huecos[c].append((r["symbol"], t))

        esperada = REFERENCIA.get(t)
        if esperada and (r.get("referencias") or "") != esperada:
            referencia_mal.append((r["symbol"], t, r.get("referencias")))

        if t not in SIN_FLUJOS_OK and con_flujo.get(r["symbol"], 0) == 0:
            sin_flujos.append((r["symbol"], t, str(r.get("vencimiento"))[:10]))
        if r["symbol"] not in con_precio:
            sin_precio.append((r["symbol"], t))

    return inst, huecos, referencia_mal, sin_flujos, sin_precio


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--breve", action="store_true", help="sólo el conteo por chequeo")
    args = ap.parse_args()

    inst, huecos, ref_mal, sin_flujos, sin_precio = revisar()
    total = sum(len(v) for v in huecos.values()) + len(ref_mal) + len(sin_flujos) + len(sin_precio)
    print(f"{len(inst)} instrumentos activos")

    if args.breve:
        print(f"  campos vacíos: {sum(len(v) for v in huecos.values())} · "
              f"referencia que no coincide: {len(ref_mal)} · "
              f"sin flujos: {len(sin_flujos)} · sin precio: {len(sin_precio)}")
        return 0

    if huecos:
        print("\n── campos vacíos ──")
        for c, v in sorted(huecos.items(), key=lambda x: -len(x[1])):
            porTipo = dict(collections.Counter(t for _, t in v))
            simbolos = " ".join(s for s, _ in sorted(v))
            print(f"  {c:16} {len(v):3}  {porTipo}")
            print(f"                    {simbolos[:150]}")

    if ref_mal:
        print("\n── referencias que no coinciden con el tipo ──")
        print("   (cerv2 decide por esta columna a quién ajusta por CER y a quién saltea)")
        for s, t, ref in sorted(ref_mal):
            print(f"  {s:8} es {t:6} pero referencias={ref!r} — debería ser {REFERENCIA[t]!r}")

    if sin_flujos:
        print("\n── sin flujos futuros: no se pueden valuar ──")
        for s, t, v in sorted(sin_flujos):
            print(f"  {s:8} {str(t):6} vence {v}")

    if sin_precio:
        print("\n── sin fila en prices ──")
        print("   " + " ".join(f"{s}" for s, _ in sorted(sin_precio)))

    print(f"\n{total} observaciones." if total else "\nSin observaciones: todo completo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
