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


YAHOO_TIME = {}   # symbol -> time of Yahoo's latest quote (unix seconds)


def fetch_yahoo(symbol, range_="3y"):
    req = urllib.request.Request(YAHOO_URL.format(sym=urllib.parse.quote(symbol), range=range_), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)["chart"]["result"][0]
    YAHOO_TIME[symbol] = res["meta"].get("regularMarketTime")
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


# ---------- Accuracy checks ----------
# Largest believable one-day move by group (%); bigger moves are held back as likely bad data.
MAX_MOVE = {"Energy": 15, "Stock markets": 10, "FX & rates": 4}
MAX_MOVE_ID = {"VIXCLS": 80, "DHHNGSP": 25, "DGS10": 15}
# How closely Yahoo must agree with FRED on the same dates (median gap, %). Oil and gas are
# skipped because FRED publishes spot prices while Yahoo gives futures.
AGREE_TOL = {"SP500": 0.5, "DJIA": 0.5, "NASDAQCOM": 0.5, "NIKKEI225": 1.0, "VIXCLS": 4.0, "DEXSIUS": 0.6, "DEXCHUS": 0.6}


def agreement(yrows, frows):
    """Median absolute % gap between Yahoo and FRED over their last 10 shared dates."""
    f = dict(frows)
    gaps = [abs(v / f[d] - 1) * 100 for d, v in yrows if d in f and f[d]][-10:]
    if len(gaps) < 3:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]


def business_days_between(a, b):
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


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


# ---------- Commodities tab ----------
def aligned(a, b, f, start):
    """Combine two (date, value) series on shared dates with f(a, b)."""
    bd = dict(b)
    return [(d, f(v, bd[d])) for d, v in a if d in bd and d >= start]


def card(cid, name, desc, unit, dec, rows, lo, hi, extra=None):
    """A commodities card with history, 1-day and 1-month change, and a range check."""
    if len(rows) < 2:
        return None
    last_d, last_v = rows[-1]
    checks = []
    if not (lo <= last_v <= hi):
        checks.append(f"Latest reading {last_v:.2f} was outside the expected range and was held back")
        rows = rows[:-1]
        last_d, last_v = rows[-1]
    prev = rows[-2][1]
    month = value_on_or_before(rows, last_d - timedelta(days=31))
    c = {"id": cid, "name": name, "desc": desc, "unit": unit, "decimals": dec, "value": round(last_v, dec + 2),
         "date": last_d.isoformat(), "change_1d": round(last_v - prev, dec + 2),
         "change_1m": round(last_v - month[1], dec + 2) if month else None, "checks": checks,
         "history": [[d.isoformat(), round(v, dec + 2)] for d, v in rows if d >= last_d - timedelta(days=365)]}
    c.update(extra or {})
    return c


def build_commodities(series_rows, today):
    out, notes = [], []
    year_ago = today - timedelta(days=370)
    def get(sym, rng="2y"):
        try:
            return fetch_yahoo(sym, rng)
        except Exception as e:
            notes.append(f"{sym} unavailable")
            print(f"  commodities: {sym} failed ({type(e).__name__})")
            return []
    # 1. Brent-WTI spread, from the same front contracts shown on the price board.
    b, w = series_rows.get("DCOILBRENTEU", []), series_rows.get("DCOILWTICO", [])
    rows = aligned(b, w, lambda x, y: x - y, year_ago)
    c = card("brent_wti", "Brent–WTI spread", "Brent minus WTI, front-month futures", "US$/bbl", 2, rows, -5, 25,
             {"note": "A wider spread makes US crude more attractive to export"})
    if c: out.append(c)
    # 2. Brent futures curve: the next seven monthly contracts.
    curve = []
    for sym in contract_symbols("BZ", today, n=10):
        r = get(sym, "1mo")
        if r and r[-1][0] >= today - timedelta(days=6):
            curve.append({"contract": sym.replace(".NYM", ""), "price": round(r[-1][1], 2), "date": r[-1][0].isoformat()})
        if len(curve) == 7:
            break
    if len(curve) >= 6:
        m1, m6 = curve[0]["price"], curve[5]["price"]
        spread = m1 - m6
        shape = "backwardation" if spread > 0.25 else "contango" if spread < -0.25 else "flat"
        if abs(spread) < 40:
            out.append({"id": "brent_curve", "name": "Brent curve", "desc": "Front month minus sixth month (M1–M6)",
                        "unit": "US$/bbl", "decimals": 2, "value": round(spread, 2), "date": curve[0]["date"],
                        "shape": shape, "curve": curve, "checks": [],
                        "note": "Backwardation (near months dearer) signals a tight market; contango signals surplus"})
    # 3. 3-2-1 crack spread: 2 bbl gasoline + 1 bbl heating oil minus 3 bbl crude, per barrel.
    rb, ho, cl = get("RB=F"), get("HO=F"), get("CL=F")
    if rb and ho and cl:
        hod = dict(ho)
        rbho = [(d, (2 * v * 42 + hod[d] * 42)) for d, v in rb if d in hod]
        rows = aligned(rbho, cl, lambda prod, crude: (prod - 3 * crude) / 3, year_ago)
        c = card("crack_321", "3-2-1 crack spread", "US refining margin: RBOB gasoline and heating oil vs WTI, front month",
                 "US$/bbl", 2, rows, -10, 100, {"note": "A proxy for how profitable it is to refine a barrel of crude"})
        if c: out.append(c)
    # 4. LNG: JKM (Asia) vs TTF (Europe), both in US$/MMBtu.
    jkm, ttf, eur = get("JKM=F"), get("TTF=F"), get("EURUSD=X")
    if jkm and ttf and eur:
        ttf_usd = aligned(ttf, eur, lambda t, fx: t * fx / 3.412, year_ago)    # EUR/MWh -> US$/MMBtu
        rows = aligned(jkm, ttf_usd, lambda j, t: j - t, year_ago)
        c = card("jkm_ttf", "JKM–TTF spread", "Asian LNG (JKM) minus European gas (TTF), front month", "US$/MMBtu", 2,
                 rows, -15, 15, {"note": "When Asia pays more than Europe, flexible LNG cargoes head east",
                                 "jkm": round(jkm[-1][1], 2), "ttf": round(ttf_usd[-1][1], 2) if ttf_usd else None})
        if c: out.append(c)
    # 5. Freight proxies: Breakwave ETFs that hold freight futures.
    for sym, cid, name, desc in [("BDRY", "freight_dry", "Dry bulk freight", "Breakwave Dry Bulk Shipping ETF (proxy for Capesize, Panamax and Supramax rates)"),
                                 ("BWET", "freight_tanker", "Tanker freight", "Breakwave Tanker Shipping ETF (proxy for crude and product tanker rates)")]:
        r = get(sym)
        rows = [(d, v) for d, v in r if d >= year_ago]
        c = card(cid, name, desc, "US$ per share", 2, rows, 0.5, 5000,
                 {"proxy": True, "note": "An ETF proxy, as the Baltic indices are not freely available"})
        if c:
            c["change_1d"] = pct(rows[-1][1], rows[-2]) if len(rows) > 1 else None
            m = value_on_or_before(rows, rows[-1][0] - timedelta(days=31))
            c["change_1m"] = pct(rows[-1][1], m)
            c["change_is_pct"] = True
            out.append(c)
    for c in out:
        print(f"commodity {c['id']}: {c['value']} {c.get('shape', '')} {c['checks'] or ''}")
    return {"items": out, "not_covered": ["Brent–Dubai spread (no free data source)", "Baltic Dry and tanker indices (licensed; ETF proxies shown instead)"]}


EIA_ROWS = {  # table4.csv row label -> (id, display name)
    "Commercial (Excluding SPR)": ("crude", "US commercial crude"),
    "Cushing": ("cushing", "Cushing, Oklahoma"),
    "SPR": ("spr", "Strategic Petroleum Reserve"),
    "Total Motor Gasoline": ("gasoline", "Gasoline"),
    "Distillate Fuel Oil": ("distillate", "Distillates (diesel, heating oil)"),
    "Kerosene-Type Jet Fuel": ("jet", "Jet fuel"),
}


def eia_weekly():
    """US weekly stocks straight from the EIA Weekly Petroleum Status Report tables (public CSV)."""
    import csv, io
    def table(n):
        req = urllib.request.Request(f"https://ir.eia.gov/wpsr/table{n}.csv", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return list(csv.reader(io.StringIO(r.read().decode("latin1"))))
    num = lambda x: float(x.replace(",", ""))
    t4 = table(4)
    head = t4[0]
    week = datetime.strptime(head[1], "%m/%d/%y").date().isoformat()
    prev = datetime.strptime(head[2], "%m/%d/%y").date().isoformat()
    items = []
    for row in t4[1:]:
        key = row[0].strip()
        if key in EIA_ROWS and not any(i["id"] == EIA_ROWS[key][0] for i in items):
            iid, name = EIA_ROWS[key]
            items.append({"id": iid, "name": name, "level": num(row[1]), "prior": num(row[2]),
                          "change": num(row[3]), "year_ago": num(row[4]), "yoy_pct": num(row[5])})
    util = None
    for row in table(9):
        if len(row) > 4 and row[1].strip() == "Percent Utilization":
            util = {"level": num(row[2]), "prior": num(row[3]), "year_ago": num(row[4])}
            break
    if len(items) < 5:
        raise ValueError(f"only {len(items)} EIA rows parsed")
    return {"week_ending": week, "prior_week": prev, "unit": "million barrels", "items": items,
            "refinery_utilization": util, "source": "EIA Weekly Petroleum Status Report",
            "url": "https://www.eia.gov/petroleum/supply/weekly/"}


def main():
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise SystemExit("FRED_API_KEY is not set. Add it under Settings > Secrets and variables > Actions.")
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=3 * 365)).isoformat()
    out = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "source": "Yahoo Finance; FRED, Federal Reserve Bank of St. Louis", "series": []}
    day_change, series_rows = {}, {}
    for group, sid, name, unit, decimals, freq in SERIES:
        rows, src, checks, as_of = [], "FRED", [], None
        try:
            rows = fetch(sid, start, key)
        except Exception as e:
            print(f"FRED failed for {sid}: {type(e).__name__}")
        fred_rows = rows
        if sid in YAHOO:
            try:
                y = fetch_yahoo(YAHOO[sid])
                contract, day = None, None
                if YAHOO[sid] in FUTURES_ROOT and len(y) >= 2:
                    try:
                        y, contract, day = front_contract(YAHOO[sid], y, today)
                    except Exception as e:
                        print(f"  front contract failed for {sid}: {type(e).__name__}: {e}")
                gap = agreement(y, fred_rows) if sid in AGREE_TOL and fred_rows else None
                if gap is not None and gap > AGREE_TOL[sid]:
                    checks.append(f"Yahoo and FRED differed by {gap:.2f}%, so FRED is shown")
                    print(f"  CHECK {sid}: Yahoo vs FRED gap {gap:.2f}% > {AGREE_TOL[sid]}%, using FRED")
                elif len(y) >= 2 and (not rows or y[-1][0] >= rows[-1][0]):
                    rows, src = y, "Yahoo Finance" + (f" ({contract})" if contract else "")
                    t = YAHOO_TIME.get(contract or YAHOO[sid])
                    if t and datetime.fromtimestamp(t, timezone.utc).date() >= y[-1][0] - timedelta(days=1):
                        as_of = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                    if day is not None:
                        day_change[sid] = day
                    if gap is not None:
                        print(f"  {sid}: Yahoo agrees with FRED (median gap {gap:.2f}%)")
            except Exception as e:
                print(f"Yahoo failed for {sid}: {type(e).__name__}")
        if len(rows) < 2:
            print(f"skip {sid}: not enough data")
            continue
        # Hold back an implausible latest move rather than publish it.
        limit = MAX_MOVE_ID.get(sid, MAX_MOVE.get(group, 15))
        move = day_change.get(sid, pct(rows[-1][1], rows[-2]))
        if move is not None and abs(move) > limit and len(rows) > 2:
            checks.append(f"A {move:+.1f}% move on {rows[-1][0]} failed the range check and was held back")
            print(f"  CHECK {sid}: {move:+.1f}% move held back")
            rows = rows[:-1]
            day_change.pop(sid, None)
            as_of = None
        series_rows[sid] = rows
        last_d, last_v = rows[-1]
        stale = business_days_between(last_d, today) > (4 if freq == "daily" else 45)
        if stale:
            checks.append(f"No new data since {last_d}")
        back = timedelta(days=31) if freq == "daily" else timedelta(days=95)
        prior = value_on_or_before(rows, last_d - back)
        ytd_base = value_on_or_before(rows, date(last_d.year - 1, 12, 31))
        yoy = value_on_or_before(rows, last_d - timedelta(days=365))
        history_start = last_d - timedelta(days=365 if freq == "daily" else 730)
        history = [[d.isoformat(), round(v, decimals + 2)] for d, v in rows if d >= history_start]
        out["series"].append({
            "group": group, "id": sid, "name": name, "unit": unit, "decimals": decimals, "frequency": freq,
            "latest": last_v, "date": last_d.isoformat(), "as_of": as_of, "source": src,
            "stale": stale, "checks": checks,
            "change_1d": day_change.get(sid, pct(last_v, rows[-2])),
            "change_short": pct(last_v, prior), "change_short_label": "1M" if freq == "daily" else "3M",
            "change_ytd": pct(last_v, ytd_base), "change_1y": pct(last_v, yoy),
            "history": history,
        })
        print(f"ok {sid} ({src}): {last_d} {last_v}" + (f" as of {as_of}" if as_of else "") + (f" [{'; '.join(checks)}]" if checks else ""))
    try:
        out["commodities"] = build_commodities(series_rows, today)
    except Exception as e:
        print(f"commodities failed: {type(e).__name__}: {e}")
    try:
        out["eia_weekly"] = eia_weekly()
        print("ok EIA weekly stocks, week ending", out["eia_weekly"]["week_ending"])
    except Exception as e:
        print(f"EIA weekly failed: {type(e).__name__}: {e}")
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
                            "status": status, "amber": amber, "red": red, "pctile": round(share * 100), "since": rows[0][0].year,
                            "history": spark})
        print(f"risk {sid}: {last_d} {value:.2f} {status} (worse than {share:.0%} of readings since {rows[0][0].year})")
    if not out["series"]:
        raise SystemExit("No series fetched; keeping the previous file.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main()
