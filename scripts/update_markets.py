"""Fetch daily market data and write data/markets.json.

Runs in GitHub Actions every morning, using only the Python standard library.
Prices come from Yahoo Finance's chart data where it is fresher (FRED's oil and
FX series lag by several days), with the official FRED API as the fallback.
FRED needs a free API key in the FRED_API_KEY environment variable (stored as
a GitHub Actions secret).
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SERIES = [
    # group, FRED id, name, unit, decimals, frequency
    ("Energy", "DCOILBRENTEU", "Brent crude", "US$/bbl", 2, "daily"),
    ("Energy", "DCOILWTICO", "WTI crude", "US$/bbl", 2, "daily"),
    ("Energy", "DHHNGSP", "Henry Hub natural gas", "US$/MMBtu", 2, "daily"),
    ("Stock markets", "SP500", "S&P 500", "index", 0, "daily"),
    ("Stock markets", "DJIA", "Dow Jones Industrial Average", "index", 0, "daily"),
    ("Stock markets", "NASDAQCOM", "Nasdaq Composite", "index", 0, "daily"),
    ("Stock markets", "NIKKEI225", "Nikkei 225", "index", 0, "daily"),
    ("Stock markets", "VIXCLS", "VIX (US volatility)", "index", 2, "daily"),
    ("FX & rates", "DEXSIUS", "USD/SGD", "SGD per USD", 4, "daily"),
    ("FX & rates", "DEXCHUS", "USD/CNY", "CNY per USD", 4, "daily"),
    ("FX & rates", "DGS10", "US 10-year Treasury yield", "%", 2, "daily"),
]

# Fresher daily source for each series, used when it returns newer data than FRED.
YAHOO = {
    "DCOILBRENTEU": "BZ=F", "DCOILWTICO": "CL=F", "DHHNGSP": "NG=F",
    "SP500": "^GSPC", "DJIA": "^DJI", "NASDAQCOM": "^IXIC", "NIKKEI225": "^N225", "VIXCLS": "^VIX",
    "DEXSIUS": "SGD=X", "DEXCHUS": "CNY=X",
}
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3y&interval=1d"
URL = "https://api.stlouisfed.org/fred/series/observations?series_id={id}&observation_start={start}&file_type=json&api_key={key}"
OUT = Path(__file__).resolve().parent.parent / "data" / "markets.json"


def fetch(series_id, start, key):
    req = urllib.request.Request(URL.format(id=series_id, start=start, key=key), headers={"User-Agent": "personal-site-market-updater"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.load(r)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    rows = []
    for obs in payload.get("observations", []):
        try:
            rows.append((date.fromisoformat(obs["date"]), float(obs["value"])))
        except (KeyError, ValueError):
            continue  # missing values are reported as "."
    return rows


def fetch_yahoo(symbol):
    req = urllib.request.Request(YAHOO_URL.format(sym=urllib.parse.quote(symbol)), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)["chart"]["result"][0]
    offset = res["meta"].get("gmtoffset", 0)
    closes = res["indicators"]["quote"][0]["close"]
    rows = {}
    for ts, v in zip(res["timestamp"], closes):
        if v is None:
            continue
        rows[datetime.fromtimestamp(ts + offset, timezone.utc).date()] = float(v)
    return sorted(rows.items())


def value_on_or_before(rows, target):
    best = None
    for d, v in rows:
        if d <= target:
            best = (d, v)
        else:
            break
    return best


def pct(new, old):
    if old is None or old[1] == 0:
        return None
    return round((new / old[1] - 1) * 100, 2)


def main():
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise SystemExit("FRED_API_KEY is not set. Add it under Settings > Secrets and variables > Actions.")
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=3 * 365)).isoformat()
    out = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "source": "Yahoo Finance; FRED, Federal Reserve Bank of St. Louis", "series": []}
    for group, sid, name, unit, decimals, freq in SERIES:
        rows, src = [], "FRED"
        try:
            rows = fetch(sid, start, key)
        except Exception as e:
            print(f"FRED failed for {sid}: {type(e).__name__}")
        if sid in YAHOO:
            try:
                y = fetch_yahoo(YAHOO[sid])
                if len(y) >= 2 and (not rows or y[-1][0] >= rows[-1][0]):
                    rows, src = y, "Yahoo Finance"
            except Exception as e:
                print(f"Yahoo failed for {sid}: {type(e).__name__}")
        if len(rows) < 2:
            print(f"skip {sid}: not enough data")
            continue
        last_d, last_v = rows[-1]
        back = timedelta(days=31) if freq == "daily" else timedelta(days=95)
        prior = value_on_or_before(rows, last_d - back)
        ytd_base = value_on_or_before(rows, date(last_d.year - 1, 12, 31))
        yoy = value_on_or_before(rows, last_d - timedelta(days=365))
        history_start = last_d - timedelta(days=365 if freq == "daily" else 730)
        history = [[d.isoformat(), round(v, decimals + 2)] for d, v in rows if d >= history_start]
        out["series"].append({
            "group": group, "id": sid, "name": name, "unit": unit, "decimals": decimals, "frequency": freq,
            "latest": last_v, "date": last_d.isoformat(), "source": src,
            "change_short": pct(last_v, prior), "change_short_label": "1M" if freq == "daily" else "3M",
            "change_ytd": pct(last_v, ytd_base), "change_1y": pct(last_v, yoy),
            "history": history,
        })
        print(f"ok {sid} ({src}): {last_d} {last_v}")
    if not out["series"]:
        raise SystemExit("No series fetched; keeping the previous file.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main()
