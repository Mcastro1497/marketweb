# explora_acciones.py — vuelca el universo de ECO para ver cómo vienen acciones y CEDEARs
import os, json, requests
from collections import Counter
from dotenv import load_dotenv
load_dotenv("/Users/manuelcastro/python_proyects/marketweb/.env")
import pyRofex

ECO_BASE = os.getenv("ECO_BASE", "https://api.eco.xoms.com.ar")
ECO_WS   = os.getenv("ECO_WS",   "wss://api.eco.xoms.com.ar")
U, P, A  = os.getenv("ECO_USER"), os.getenv("ECO_PASS"), os.getenv("ECO_ACCT")

pyRofex._set_environment_parameter("url", ECO_BASE + "/", pyRofex.Environment.LIVE)
pyRofex._set_environment_parameter("ws",  ECO_WS   + "/", pyRofex.Environment.LIVE)
requests.post(ECO_BASE + "/login", json={"username": U, "password": P}, timeout=8).raise_for_status()
pyRofex.initialize(user=U, password=P, account=A, environment=pyRofex.Environment.LIVE)
print("[OK] conectado")

det = (pyRofex.get_detailed_instruments() or {}).get("instruments") or []
print("TOTAL detailed instruments:", len(det))
if det:
    print("\n=== campos de un instrumento ===")
    print(list(det[0].keys()))
    print("\n=== 2 ejemplos crudos ===")
    for it in det[:2]:
        print(json.dumps(it, ensure_ascii=False)[:500])

# distribución de cfiCode
def g(it, k):
    return it.get(k) or (it.get("instrument") or {}).get(k)
cfi = Counter()
for it in det:
    code = it.get("cfiCode") or it.get("cficode") or (it.get("securityType"))
    cfi[code] += 1
print("\n=== cfiCode / tipo (top 25) ===")
for c, n in cfi.most_common(25):
    print(f"  {str(c):<12} {n}")
