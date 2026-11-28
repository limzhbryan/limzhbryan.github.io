"""Temporary: check which Yahoo symbols return data from GitHub Actions."""
import json, urllib.request, urllib.parse
SYMS = ["RB=F", "HO=F", "TTF=F", "JKM=F", "LNG=F", "^BDI", "BDI", "BDRY", "BWET", "DBI=F", "DME=F", "DUB=F",
        "BZF27.NYM", "BZG27.NYM", "BZH27.NYM", "BZJ27.NYM", "BZK27.NYM", "BZM27.NYM", "BZN27.NYM",
        "CLG27.NYM", "CLM27.NYM", "JKMF27.NYM", "JKMG27.NYM", "TTFF27.NYM", "MTFF27.NYM", "TFMF27.NYM"]
for s in SYMS:
    try:
        u = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?range=1mo&interval=1d"
        with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            res = json.load(r)["chart"]["result"][0]
        m = res["meta"]; c = [x for x in res["indicators"]["quote"][0]["close"] if x is not None]
        print(f"OK {s}: {m.get('longName') or m.get('shortName')} | {m.get('currency')} | n={len(c)} last={c[-1] if c else None}")
    except Exception as e:
        print(f"NO {s}: {type(e).__name__}")
