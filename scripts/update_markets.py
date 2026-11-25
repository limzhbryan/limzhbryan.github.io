"""Fetch daily market data from FRED (St. Louis Fed) and write data/markets.json.

Runs in GitHub Actions every morning. Uses only the Python standard library and
FRED's public CSV download, so no API key is needed.
"""
import csv
import io
import json
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SERIES = [
    # group, FRED id, name, unit, decimals, frequency
    ("Energy", "DCOILBRENTEU", "Brent crude", "US$/bbl", 2, "daily"),
    ("Energy", "DCOILWTICO", "WTI crude", "US$/bbl", 2, "daily"),
    ("Energy", "DHHNGSP", "Henry Hub natural gas", "US$/MMBtu", 2, "daily"),
    ("Metals", "PIORECRUSDM", "Iron ore (CFR China)", "US$/t", 1, "monthly"),
    ("Metals", "PCOPPUSDM", "Copper", "US$/t", 0, "monthly"),
    ("Metals", "PALUMUSDM", "Aluminium", "US$/t", 0, "monthly"),
    ("FX & rates", "DEXSIUS", "USD/SGD", "SGD per USD", 4, "daily"),
    ("FX & rates", "DEXCHUS", "USD/CNY", "CNY per USD", 4, "daily"),
    ("FX & rates", "DGS10", "US 10-year Treasury yield", "%", 2, "daily"),
]

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}&cosd={start}"
OUT = Path(__file__).resolve().parent.parent / "data" / "markets.json"


def fetch(series_id, start):
    req = urllib.request.Request(URL.format(id=series_id, start=start), headers={"User-Agent": "personal-site-market-updater"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                text = r.read().decode("utf-8")
            break
        except Exception:
            if attempt == 1:
                raise
            time.sleep(5 * (attempt + 1))
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        try:
            d = date.fromisoformat(row[0])
            v = float(row[1])
        except ValueError:
            continue  # header row or missing value (".")
        rows.append((d, v))
    return rows


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
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=3 * 365)).isoformat()
    out = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "source": "FRED, Federal Reserve Bank of St. Louis", "series": []}
    for group, sid, name, unit, decimals, freq in SERIES:
        try:
            rows = fetch(sid, start)
        except Exception as e:
            print(f"skip {sid}: {e}")
            continue
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
            "latest": last_v, "date": last_d.isoformat(),
            "change_short": pct(last_v, prior), "change_short_label": "1M" if freq == "daily" else "3M",
            "change_ytd": pct(last_v, ytd_base), "change_1y": pct(last_v, yoy),
            "history": history,
        })
        print(f"ok {sid}: {last_d} {last_v}")
    if not out["series"]:
        raise SystemExit("No series fetched; keeping the previous file.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main()
