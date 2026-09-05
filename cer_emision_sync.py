#!/usr/bin/env python
"""Completa `cer_emision` en instruments: el CER de 10 días hábiles antes de emitir.

QUÉ ES
------
Los bonos CER ajustan por el cociente CER(hoy) / CER(emisión). El prospecto no
usa el CER del día de emisión sino el de DIEZ DÍAS HÁBILES ANTES, porque el
INDEC publica el CER con ese rezago: el día que se emite, el coeficiente de ese
día todavía no existe. Es la misma ventana de 10 hábiles que ya usan cerv2 y
tamar para el extremo de arriba del cociente.

POR QUÉ HACE FALTA UN PASO
--------------------------
El uploader del panel de admin no lo puede calcular: sale del cruce de la serie
CER con el calendario bancario, y esa lógica vive acá (lib/calendario, que lee
holidays y contempla los feriados que la API nacional no trae). Duplicarla en el
front es exactamente el bug que documenta el docstring de lib/calendario.py.

Y no completarlo no falla ruidosamente, que es lo peor: cerv2.es_cer() pide
referencias='CER' Y cer_emision>0, así que un CER sin este dato se valúa como si
fuera a tasa fija, sin un solo mensaje de error.

QUÉ NO PISA
-----------
Un valor ya cargado no se toca salvo que se pida --corregir. Los bonos viejos
(CUAP, DICP, PARP, TX26, TX28, TX31) emitieron antes de que arranque la serie en
la base y su cer_emision se cargó a mano: recalcularlos daría None y borraría un
dato bueno. Las diferencias se reportan siempre, se escriban o no.

    python cer_emision_sync.py                      # completa los que faltan
    python cer_emision_sync.py --dry-run            # muestra qué haría
    python cer_emision_sync.py --tabla instruments_test
    python cer_emision_sync.py --corregir           # además pisa los que difieren
"""
import argparse
import sys
from datetime import date

from lib.calendario import habil_anterior
from lib.db import cliente
from lib.series import cer, valor_en

VENTANA = 10          # días hábiles de rezago del prospecto
TOLERANCIA = 1e-6     # relativa: los guardados vienen redondeados a 4-6 decimales


def necesita_cer(r: dict) -> bool:
    """Quién usa cer_emision: los CER, y los duales por su pata CER (patas.py:491)."""
    return (r.get("referencias") or "").strip().upper() == "CER" \
        or r.get("instrument_type") in ("CER", "DUAL")


def sincronizar(tabla: str, dry_run: bool, corregir: bool) -> int:
    sb = cliente()
    serie = cer()
    if not serie:
        print("[X] La serie CER está vacía. Corré series_sync.py primero.")
        return 1
    print(f"serie CER: {len(serie)} puntos, {min(serie)} … {max(serie)}\n")

    filas = sb.table(tabla).select(
        "symbol, instrument_type, referencias, emision, cer_emision").execute().data or []
    bonos = [r for r in filas if necesita_cer(r)]
    print(f"{tabla}: {len(filas)} instrumentos, {len(bonos)} ajustan por CER\n")

    completar, difieren, sin_serie, sin_emision = [], [], [], []
    for r in sorted(bonos, key=lambda x: x["symbol"]):
        if not r.get("emision"):
            sin_emision.append(r); continue
        emision = date.fromisoformat(str(r["emision"])[:10])
        ref = habil_anterior(emision, VENTANA)
        calculado = valor_en(serie, ref)
        guardado = float(r["cer_emision"]) if r.get("cer_emision") is not None else None

        if calculado is None:
            # Emitido antes de que arranque la serie. Si ya hay dato, es bueno.
            sin_serie.append((r, ref, guardado)); continue
        if guardado is None:
            completar.append((r, ref, calculado)); continue
        if abs(guardado - calculado) / calculado > TOLERANCIA:
            difieren.append((r, ref, guardado, calculado))

    if completar:
        print(f"── A completar ({len(completar)}) ──")
        for r, ref, v in completar:
            print(f"  + {r['symbol']:8} emisión {r['emision']}  −{VENTANA}h → {ref}  CER = {v:.6f}")
    if difieren:
        print(f"\n── Ya cargados que NO coinciden ({len(difieren)}) ──")
        for r, ref, g, v in difieren:
            print(f"  ! {r['symbol']:8} emisión {r['emision']}  −{VENTANA}h → {ref}  "
                  f"guardado {g:.6f}  vs  calculado {v:.6f}  ({g / v:.4%} del correcto)")
        if not corregir:
            print("    (no se pisan; con --corregir se sobrescriben)")
    if sin_serie:
        print(f"\n── Sin serie en esa fecha ({len(sin_serie)}) ──")
        for r, ref, g in sin_serie:
            estado = f"conserva {g:.6f}" if g is not None else "QUEDA EN NULL — se valúa como FIJA"
            print(f"  · {r['symbol']:8} {ref} anterior al inicio de la serie: {estado}")
    if sin_emision:
        print(f"\n── Sin fecha de emisión ({len(sin_emision)}) ──")
        for r in sin_emision:
            print(f"  · {r['symbol']:8} no se puede calcular")

    escribir = [(r, v) for r, _, v in completar]
    if corregir:
        escribir += [(r, v) for r, _, _, v in difieren]
    if not escribir:
        print("\nNada para escribir.")
        return 0
    if dry_run:
        print(f"\n(dry-run: se escribirían {len(escribir)} filas)")
        return 0
    for r, v in escribir:
        sb.table(tabla).update({"cer_emision": v}).eq("symbol", r["symbol"]).execute()
    print(f"\nEscritas {len(escribir)} filas en {tabla}.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tabla", default="instruments")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--corregir", action="store_true",
                   help="además pisa los cer_emision ya cargados que no coinciden")
    a = p.parse_args()
    print(f"cer_emision ← CER(emisión − {VENTANA} hábiles)"
          f"{'  (dry-run)' if a.dry_run else ''}\n")
    return sincronizar(a.tabla, a.dry_run, a.corregir)


if __name__ == "__main__":
    sys.exit(main())
