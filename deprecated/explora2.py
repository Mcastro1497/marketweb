import os, json, requests
from collections import Counter
from dotenv import load_dotenv
load_dotenv("/Users/manuelcastro/python_proyects/marketweb/.env")
import pyRofex
ECO_BASE=os.getenv("ECO_BASE","https://api.eco.xoms.com.ar"); ECO_WS=os.getenv("ECO_WS","wss://api.eco.xoms.com.ar")
U,P,A=os.getenv("ECO_USER"),os.getenv("ECO_PASS"),os.getenv("ECO_ACCT")
pyRofex._set_environment_parameter("url",ECO_BASE+"/",pyRofex.Environment.LIVE)
pyRofex._set_environment_parameter("ws",ECO_WS+"/",pyRofex.Environment.LIVE)
requests.post(ECO_BASE+"/login",json={"username":U,"password":P},timeout=8).raise_for_status()
pyRofex.initialize(user=U,password=P,account=A,environment=pyRofex.Environment.LIVE)
det=(pyRofex.get_detailed_instruments() or {}).get("instruments") or []

def sym(it): return ((it.get("instrumentId") or {}).get("symbol")) or ""
def tk(s):
    p=[x.strip() for x in s.split(" - ")]; return p[2] if len(p)>=3 else s

for code,label in [("ESXXXX","ACCIONES"),("EMXXXX","CEDEARS")]:
    items=[it for it in det if (it.get("cficode")==code)]
    print(f"\n===== {label} ({code}) — {len(items)} instrumentos =====")
    tickers=sorted({tk(sym(it)) for it in items if sym(it).startswith("MERV - ")})
    print(f"tickers distintos: {len(tickers)}")
    print("ejemplos tickers:", tickers[:25])
    print("--- 4 instrumentos crudos (symbol/currency/settl/desc) ---")
    for it in items[:4]:
        print(f"  {sym(it):<28} cur={it.get('currency')} settl={it.get('settlType')} desc={it.get('securityDescription')}")
