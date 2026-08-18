# cer_test.py
# -*- coding: utf-8 -*-
"""
Valúa UN bono CER/FIJA mostrando todos los pasos, usando la misma lógica que cer.py.
Uso:
    .venv/bin/python cer_test.py            # default X31L6
    .venv/bin/python cer_test.py TX28
    .venv/bin/python cer_test.py X31L6 --sync   # sincroniza CER del BCRA antes (escribe cer_historico)
"""
import sys
from datetime import datetime, timezone
from math import isfinite

import cer  # reutiliza sb, calendario, xirr, macaulay, loaders, etc.

SYM = next((a for a in sys.argv[1:] if not a.startswith("-")), "X31L6")
DO_SYNC = "--sync" in sys.argv

def line(): print("-" * 60)

def main():
    now_utc = datetime.now(timezone.utc)

    if DO_SYNC:
        print("[sync] Trayendo CER del BCRA..."); cer.sync_cer_from_bcra()

    # 1) Calendario / cutoff (igual que cer.py)
    hols = cer.load_holidays()
    cut, t1_date, val_utc = cer.cutoff_and_val(now_utc, hols)
    cer_t10_date = cer.minus_n_bdays(t1_date, 10, hols)
    print(f"\nBONO: {SYM}")
    line()
    print(f"Hoy (UTC)        : {now_utc.date()}")
    print(f"Cutoff flujos    : {cut.date()}  (se toman pagos con fecha > cutoff)")
    print(f"Liquidación (T+1): {t1_date}   -> fecha de valuación del CF inicial")
    print(f"CER t-10 hábiles : {cer_t10_date}")

    # 2) Instrumento + precio
    instr, price_map = cer.load_ars_instruments()
    row = next((r for r in instr if r["symbol"] == SYM), None)
    if row is None:
        print(f"\n[X] {SYM} no aparece como CER/FIJA ARS activo en instruments_v2.")
        return
    tipo   = str(row.get("tipo", "") or "").upper()
    cer_em = row.get("cer_emision")
    base   = price_map.get(SYM)
    line()
    print(f"tipo (de referencias): {tipo}")
    print(f"cer_emision          : {cer_em}")
    print(f"precio base (prices) : {base}   (price_ars, fallback closing_price)")

    if base is None:
        print("\n[X] Sin precio en la tabla prices. No se puede valuar.")
        return

    # 3) Ajuste CER del precio (igual que cer.py)
    cer_t10 = cer.load_cer_value(cer_t10_date) if tipo == "CER" else None
    if tipo == "CER":
        print(f"CER(t-10)            : {cer_t10}")
        if not cer_em or float(cer_em) <= 0 or not cer_t10:
            print("\n[X] Falta cer_emision o CER(t-10): se omitiría (como en cer.py).")
            return
        factor = cer_t10 / float(cer_em)
        price = base / factor
        print(f"factor CER (t10/em)  : {factor:.6f}")
        print(f"precio ajustado      : {price:.4f}   (= base / factor)")
    else:
        price = base
        print(f"precio (FIJA, sin ajuste): {price:.4f}")

    # 4) Flujos futuros
    flows = cer.load_ars_flows(cut, [SYM])
    line()
    print(f"FLUJOS FUTUROS ({len(flows)}):")
    if flows.empty:
        print("  (ninguno tras el cutoff) -> no se puede calcular TIR")
        return
    g = (flows[flows["symbol"] == SYM][["fecha_pago", "total"]]
         .groupby("fecha_pago", as_index=False).agg(total=("total", "sum"))
         .sort_values("fecha_pago"))
    for _, r in g.iterrows():
        print(f"  {r['fecha_pago'].date()}   total = {float(r['total']):.6f}")

    # 5) Cash flow y XIRR/duration (igual que cer.py)
    cf = [(val_utc, -price)]
    for _, r in g.iterrows():
        amt = float(r["total"])
        if amt != 0:
            cf.append((r["fecha_pago"].to_pydatetime(), amt))
    line()
    print("CASH FLOW (fecha, monto):")
    for d, a in cf:
        print(f"  {d.date()}   {a:+.6f}")

    if len(cf) < 2:
        print("\n[X] Menos de 2 flujos: no se puede calcular TIR.")
        return

    ytm = cer.xirr(cf, 0.10)
    line()
    if ytm is None or not isfinite(ytm):
        print("[X] XIRR no convergió.")
        return
    cfs_pos = [(cer._yf(val_utc, r["fecha_pago"].to_pydatetime()), float(r["total"]))
               for _, r in g.iterrows() if float(r["total"]) != 0]
    dur = cer.macaulay(cfs_pos, ytm)
    print(f"TIR (XIRR)   : {ytm:.4%}")
    print(f"Duration (Mac): {round(dur, 4) if dur else None}")
    print("\n(Estos son los valores que cer.py upsertearía en prices.ytm / duration_y)")

if __name__ == "__main__":
    main()
