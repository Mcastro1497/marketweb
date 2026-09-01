"""
run.py — Corre todo el pipeline, una pasada, en el orden correcto.

Reemplaza a los diez comandos sueltos. Cada paso es un subproceso: un fallo o un
cuelgue en uno no arrastra a los demás, la memoria se libera entre pasos, y es
exactamente lo que haría un scheduler.

POR QUÉ EL ORDEN IMPORTA
El pipeline es una cadena de dependencias reales, no una lista arbitraria:

    series_sync ─┐
    rem_sync    ─┼─> cerv2 ──┐
    (precios)   ─┘           ├─> patas
                  dlk/tir/tamar ┘

  · Los motores valúan contra el precio, así que necesitan que precios2 ya lo
    haya escrito.
  · cerv2 proyecta con el CER, que trae series_sync.
  · patas va SIEMPRE al final: consume el precio, la serie TAMAR, el CER y el
    REM. Correrlo antes deja la TIR en NULL o la calcula con datos viejos.

QUÉ NO ESTÁ ACÁ: precios2.py
Es un websocket contra ECO: suscribe una vez y después el proceso sólo espera
mientras el handler recibe los ticks. Sí es programable —arranca a la apertura,
vive la rueda y con --hasta se apaga solo al cierre—, pero no es un job que corre
y sale en segundos como estos siete, así que va en su propio workflow.

POR QUÉ ESTO EVITA EL PROBLEMA DE LOS DAEMONS
Con seis procesos de larga vida, cada cambio de esquema obliga a acordarse de
reiniciar los seis, y el que te olvidás corre código viejo contra tablas nuevas
—en silencio o, peor, borrando datos. Con un entrypoint que corre una pasada y
sale, cada ciclo levanta el código actual y ese problema no existe.

Uso:
    python run.py                    # pipeline completo
    python run.py --dry-run          # muestra el plan, no ejecuta
    python run.py --solo patas       # un paso
    python run.py --desde cerv2      # desde ese paso en adelante
    python run.py --saltear rem      # omite pasos
    python run.py --rueda            # sólo lo que depende del precio (~26s)
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent

# En local los scripts corren con el intérprete de .venv; en un runner de CI ese
# directorio no existe y hay que usar el python que ya está ejecutando esto.
_VENV = RAIZ / ".venv" / "bin" / "python"
PY = str(_VENV) if _VENV.exists() else sys.executable

# (nombre, comando, descripción, opcional, tramo)
#
# opcional=True: si falla se reporta pero no tumba la corrida.
#
# tramo distingue QUÉ TAN SEGUIDO tiene sentido correr cada paso:
#   "diario"  Datos de referencia que NO cambian dentro del día. El CER del día
#             se publica una vez, el REM una vez por mes. Volver a pedirlos en
#             cada ciclo son 12 segundos tirados y carga inútil sobre el BCRA.
#   "rueda"   Depende del precio, así que cambia todo el tiempo. Es lo único que
#             tiene sentido recalcular seguido.
PASOS = [
    # Va primero y no al final: si un instrumento venció, no tiene sentido que
    # cerv2/dlk/tir gasten una valuación en él ni que la web lo siga listando.
    # Antes esto era deprecated/cleanflows.py, manual y "cada tanto", así que en
    # los hechos no corría: se juntaron instrumentos vencidos hacía diez días.
    # BORRA, no desactiva, y por eso deja backup de lo que saca.
    ("limpieza", [PY, "cleanflows.py", "--backup-dir", "backups"],
     "Borra instrumentos y flujos ya vencidos (deja backup en backups/)", False, "diario"),
    ("series", [PY, "series_sync.py"],
     "Series del BCRA (cer, tamar_tna, a3500, ipc_mensual)", False, "diario"),
    ("rem",    [PY, "rem_sync.py"],
     "REM del BCRA. Es mensual: casi siempre no hay nada nuevo", True, "diario"),
    ("cerv2",  [PY, "cerv2.py", "--once"],
     "Proyección CER de los flujos + valuación de soberanos ARS", False, "rueda"),
    ("dlk",    [PY, "dlk.py", "--once"],
     "Dólar linked: TIR en dólares", False, "rueda"),
    ("tir",    [PY, "tir.py", "--once"],
     "ON y hard dollar: TIR en dólares", False, "rueda"),
    ("tamar",  [PY, "tamar.py", "--once"],
     "Bonos TAMAR: columnas TAMAR de prices", False, "rueda"),
    # patas queda en "diario" y no en "rueda": durante la rueda lo hace
    # valuar_loop.py, que cachea la referencia y revalúa cada pocos segundos en
    # vez de cada 15 minutos. Acá sigue para dejar el cierre consolidado y por si
    # el bucle no corrió.
    ("patas",  [PY, "patas.py"],
     "Patas y duales: valuations + prices.ytm_ars. SIEMPRE AL FINAL", False, "diario"),
]


def correr(paso, args) -> tuple:
    nombre, cmd, desc, opcional, _tramo = paso
    print(f"\n{'─' * 70}\n▶ {nombre}  —  {desc}")
    if args.dry_run:
        print(f"  (dry-run) {' '.join(cmd)}")
        return nombre, True, 0.0

    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=RAIZ, timeout=args.timeout,
                           capture_output=not args.verbose, text=True)
        dt = time.monotonic() - t0
        if r.returncode != 0:
            print(f"  ✗ salió con código {r.returncode} en {dt:.0f}s")
            if not args.verbose and r.stdout:
                print("  ── últimas líneas ──")
                for ln in r.stdout.strip().splitlines()[-12:]:
                    print(f"    {ln}")
            if not args.verbose and r.stderr:
                for ln in r.stderr.strip().splitlines()[-8:]:
                    print(f"    {ln}")
            return nombre, opcional, dt
        # En verde: la última línea suele traer el resumen del script.
        if not args.verbose and r.stdout:
            ultima = [l for l in r.stdout.strip().splitlines() if l.strip()]
            if ultima:
                print(f"  {ultima[-1].strip()}")
        print(f"  ✓ {dt:.0f}s")
        return nombre, True, dt
    except subprocess.TimeoutExpired:
        dt = time.monotonic() - t0
        print(f"  ✗ timeout a los {dt:.0f}s")
        return nombre, opcional, dt


def main():
    ap = argparse.ArgumentParser(description="Pipeline completo de marketweb")
    ap.add_argument("--dry-run", action="store_true", help="muestra el plan, no ejecuta")
    ap.add_argument("--solo", help="correr sólo este paso")
    ap.add_argument("--desde", help="empezar desde este paso")
    ap.add_argument("--saltear", default="", help="pasos a omitir, separados por coma")
    ap.add_argument("--rueda", action="store_true",
                    help="sólo los pasos que dependen del precio; saltea las series y el "
                         "REM, que no cambian dentro del día. Es el modo para correr seguido.")
    ap.add_argument("--verbose", action="store_true", help="muestra la salida completa de cada paso")
    ap.add_argument("--timeout", type=int, default=1800, help="segundos por paso (default 1800)")
    args = ap.parse_args()

    pasos = PASOS
    if args.rueda:
        pasos = [p for p in pasos if p[4] == "rueda"]
    if args.solo:
        pasos = [p for p in pasos if p[0] == args.solo]
        if not pasos:
            print(f"paso desconocido: {args.solo}. Disponibles: {', '.join(p[0] for p in PASOS)}")
            return 2
    if args.desde:
        nombres = [p[0] for p in pasos]
        if args.desde not in nombres:
            print(f"paso desconocido: {args.desde}")
            return 2
        pasos = pasos[nombres.index(args.desde):]
    saltear = {s.strip() for s in args.saltear.split(",") if s.strip()}
    pasos = [p for p in pasos if p[0] not in saltear]

    inicio = datetime.now(timezone.utc)
    print(f"═══ marketweb · {inicio:%Y-%m-%d %H:%M:%S} UTC · {len(pasos)} pasos ═══")
    if saltear:
        print(f"  salteados: {', '.join(sorted(saltear))}")

    resultados = [correr(p, args) for p in pasos]

    total = (datetime.now(timezone.utc) - inicio).total_seconds()
    fallidos = [n for n, ok, _ in resultados if not ok]
    print(f"\n{'═' * 70}")
    for n, ok, dt in resultados:
        print(f"  {'✓' if ok else '✗'} {n:10} {dt:6.0f}s")
    print(f"  total {total:.0f}s")

    if fallidos:
        print(f"\n✗ fallaron: {', '.join(fallidos)}")
        return 1
    print("\n✓ pipeline completo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
