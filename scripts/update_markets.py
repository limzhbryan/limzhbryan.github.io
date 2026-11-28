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
    ("FX & rates", "DEXSIUS", "USD/SGD", "S$ per US$1", 4, "daily"),
    ("FX & rates", "DEXCHUS", "USD/CNY", "yuan per US$1", 4, "daily"),
    ("FX & rates", "DGS10", "US 10-year Treasury yield", "%", 2, "daily"),
]

# Fresher daily source for each series, used when it returns newer data than FRED.
YAHOO = {
    "DCOILBRENTEU": "BZ=F", "DCOILWTICO": "CL=F", "DHHNGSP": "NG=F",
    "SP500": "^GSPC", "DJIA": "^DJI", "NASDAQCOM": "^IXIC", "NIKKEI225": "^N225", "VIXCLS": "^VIX",
    "DEXSIUS": "SGD=X", "DEXCHUS": "CNY=X",
}
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={range}&interval=1d"
# Yahoo's continuous futures (BZ=F etc.) switch to the next contract before the old one
# expires, which makes the price jump and the daily change compare two different
# contracts. For these we use the nearest live contract itself (e.g. BZF27.NYM) for
# recent prices, and scale the older continuous history to join it smoothly.
FUTURES_ROOT = {"BZ=F": "BZ", "CL=F": "CL", "NG=F": "NG"}
MONTH_CODES = "FGHJKMNQUVXZ"
URL = "https://api.stlouisfed.org/fred/series/observations?series_id={id}&observation_start={start}&file_type=json&api_key={key}"
# Market risk monitor: FRED series with fixed, published-style thresholds.
# kind "level": higher is worse; "below": lower is worse; "change": 1-month % change, higher is worse.
RISK = [
    # id, name, what it measures, unit, decimals, kind, amber, red
    ("BAMLH0A0HYM2", "Credit stress", "US high-yield bond spread over Treasuries", "%", 2, "level", 4.0, 6.0),
    ("VIXCLS", "Fear gauge", "VIX, expected S&P 500 volatility", "index", 1, "level", 20.0, 30.0),
    ("T10Y2Y", "Recession signal", "10-year minus 2-year Treasury yield", "pts", 2, "below", 0.25, 0.0),
    ("STLFSI4", "Financial stress", "St. Louis Fed Financial Stress Index (0 = average)", "index", 2, "level", 0.0, 1.0),
    ("DTWEXBGS", "Dollar squeeze", "Broad US dollar index, change over the past month", "%", 1, "change", 1.5, 3.0),
]
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


def fetch_yahoo(symbol, range_="3y"):
    req = urllib.request.Request(YAHOO_URL.format(sym=urllib.parse.quote(symbol), range=range_), headers={"User-Agent": "Mozilla/5.0"})
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


def contract_symbols(root, today, n=4):
    """Yahoo symbols for the next n contract months, e.g. BZF27.NYM."""
    out = []
    y, m = today.year, today.month
    for i in range(n):
        mm = (m - 1 + i) % 12
        yy = y + (m - 1 + i) // 12
        out.append(f"{root}{MONTH_CODES[mm]}{yy % 100:02d}.NYM")
    return out


def front_contract(cont_symbol, cont_rows, today):
    """Use the nearest live contract for recent prices. Returns (rows, symbol, day_change)."""
    root = FUTURES_ROOT[cont_symbol]
    last_day = cont_rows[-1][0]
    for sym in contract_symbols(root, today):
        try:
            rows = fetch_yahoo(sym, "6mo")
        except Exception as e:
            print(f"  {sym}: {type(e).__name__}")
            continue
        if len(rows) < 2 or rows[-1][0] < last_day:
            continue
        c = dict(rows)
        # Last date on which the continuous series was this same contract (prices match).
        match = None
        for d, v in cont_rows:
            if d in c and c[d] and abs(v / c[d] - 1) < 0.003:
                match = d
        if match is None:
            match = cont_rows[-2][0]
        spliced = [(d, v) for d, v in cont_rows if d <= match] + [(d, v) for d, v in rows if d > match]
        day = round((rows[-1][1] / rows[-2][1] - 1) * 100, 2) if rows[-2][1] else None
        print(f"  front contract {sym}: {rows[-1][0]} {rows[-1][1]:.2f} (prev {rows[-2][1]:.2f}, continuous {cont_rows[-1][1]:.2f}, joined after {match})")
        return spliced, sym, day
    return cont_rows, None, None


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
    day_change = {}
    for group, sid, name, unit, decimals, freq in SERIES:
        rows, src = [], "FRED"
        try:
            rows = fetch(sid, start, key)
        except Exception as e:
            print(f"FRED failed for {sid}: {type(e).__name__}")
        if sid in YAHOO:
            try:
                y = fetch_yahoo(YAHOO[sid])
                contract, day = None, None
                if YAHOO[sid] in FUTURES_ROOT and len(y) >= 2:
                    try:
                        y, contract, day = front_contract(YAHOO[sid], y, today)
                    except Exception as e:
                        print(f"  front contract failed for {sid}: {type(e).__name__}: {e}")
                if len(y) >= 2 and (not rows or y[-1][0] >= rows[-1][0]):
                    rows, src = y, "Yahoo Finance" + (f" ({contract})" if contract else "")
                    if day is not None:
                        day_change[sid] = day
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
            "change_1d": day_change.get(sid, pct(last_v, rows[-2])),
            "change_short": pct(last_v, prior), "change_short_label": "1M" if freq == "daily" else "3M",
            "change_ytd": pct(last_v, ytd_base), "change_1y": pct(last_v, yoy),
            "history": history,
        })
        print(f"ok {sid} ({src}): {last_d} {last_v}")
    out["risk"] = []
    rstart = (today - timedelta(days=10 * 365)).isoformat()
    for sid, name, desc, unit, dec, kind, amber, red in RISK:
        try:
            rows = fetch(sid, rstart, key)
        except Exception as e:
            print(f"risk {sid} failed: {type(e).__name__}")
            continue
        if len(rows) < 30:
            continue
        last_d, last_v = rows[-1]
        if kind == "change":
            prior = value_on_or_before(rows, last_d - timedelta(days=30))
            value = pct(last_v, prior)
            if value is None:
                continue
            hist_vals = []
            for d, v in rows:
                p = value_on_or_before(rows, d - timedelta(days=30))
                if p and p[1]:
                    hist_vals.append((v / p[1] - 1) * 100)
        else:
            value, hist_vals = last_v, [v for _, v in rows]
        if kind == "below":
            status = "red" if value < red else "amber" if value < amber else "green"
            share = sum(1 for v in hist_vals if v >= value) / len(hist_vals)   # share of days with a less worrying reading
        else:
            status = "red" if value >= red else "amber" if value >= amber else "green"
            share = sum(1 for v in hist_vals if v <= value) / len(hist_vals)
        spark = [[d.isoformat(), round(v, dec + 2)] for d, v in rows if d >= last_d - timedelta(days=365)]
        out["risk"].append({"id": sid, "name": name, "desc": desc, "unit": unit, "decimals": dec, "kind": kind,
                            "value": round(value, dec + 2), "level": round(last_v, dec + 2), "date": last_d.isoformat(),
                            "status": status, "amber": amber, "red": red, "pctile_10y": round(share * 100),
                            "history": spark})
        print(f"risk {sid}: {last_d} {value:.2f} {status} (worse than {share:.0%} of the past 10 years)")
    if not out["series"]:
        raise SystemExit("No series fetched; keeping the previous file.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main()
