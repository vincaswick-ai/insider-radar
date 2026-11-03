"""
Backtest: do insiders who made money before keep making money?

Data
  SEC "Insider Transactions Data Sets" (every Form 4 since 2006, one zip per quarter)
  Yahoo daily prices (split-adjusted, dividends included) + SPY as the market.

Method (no peeking into the future)
  1. Every open-market purchase (code P) of $25K+ by an insider.
  2. Buys by the same insider in the same company <= 14 days apart = one "buy event".
  3. Entry = close of the first trading day AFTER the Form 4 was filed (what the public could do).
  4. Result = return over 3 and 6 months minus SPY over the same days ("excess").
  5. For every event we look only at the insider's EARLIER events whose 6-month result was
     already known at that moment, and group them:
        first buy · past winner (avg excess > 0, 2+ results) · past loser · one earlier result
  6. Compare the groups. If past winners beat the others, the "smart insiders" idea works.
  Also: by role, by price drop, cluster buys, and by time period (robustness).

Known weakness: companies that went bankrupt or were bought often have no Yahoo prices,
so they drop out (survivorship bias). The same bias hits every group, so the comparison
between groups is fairer than the absolute numbers.

Env: START_YEAR (default 2006), END_YEAR, SEC_USER_AGENT, WEBHOOK_PROOF (optional).
Outputs: backtest_results.md, insider_ranking.csv
"""

import csv
import io
import json
import os
import re
import statistics as st
import sys
import time
import zipfile
from bisect import bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests

ZIP_URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip"
PRICE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}"
UA_BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MIN_VALUE = 25_000
SPREE_DAYS = 14
H3, H6 = 63, 126          # trading days
HERE = os.path.dirname(os.path.abspath(__file__))
TOP_ROLES = ("chief executive", "ceo", "chief financial", "cfo", "president", "chair")


def log(msg):
    print(msg, flush=True)


def pday(s):
    s = (s or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s[:11], fmt).date()
        except ValueError:
            continue
    return None


def fnum(s):
    try:
        return float((s or "").replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- 1. SEC data

def read_tsv(z, name):
    for n in z.namelist():
        if n.upper().endswith(name):
            with z.open(n) as f:
                text = io.TextIOWrapper(f, encoding="latin-1", newline="")
                return list(csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE))
    return []


def load_quarter(session, y, q):
    url = ZIP_URL.format(y=y, q=q)
    for i in range(4):
        r = session.get(url, timeout=120)
        if r.status_code == 200:
            break
        if r.status_code == 404:
            return []
        time.sleep(3 + 5 * i)
    else:
        log(f"  {y}q{q}: HTTP {r.status_code}")
        return []
    z = zipfile.ZipFile(io.BytesIO(r.content))
    subs = {s["ACCESSION_NUMBER"]: s for s in read_tsv(z, "SUBMISSION.TSV")
            if (s.get("DOCUMENT_TYPE") or "").startswith("4")}
    owners = {}
    for o in read_tsv(z, "REPORTINGOWNER.TSV"):
        owners.setdefault(o["ACCESSION_NUMBER"], o)          # first owner of joint filings
    out = []
    for t in read_tsv(z, "NONDERIV_TRANS.TSV"):
        acc = t["ACCESSION_NUMBER"]
        if (t.get("TRANS_CODE") or "").strip() != "P" or acc not in subs or acc not in owners:
            continue
        shares, price = fnum(t.get("TRANS_SHARES")), fnum(t.get("TRANS_PRICEPERSHARE"))
        if not shares or not price or price <= 0:
            continue
        s, o = subs[acc], owners[acc]
        ticker = (s.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{1,5}([.-][A-Z])?", ticker) or ticker in ("NONE", "NA", "N/A"):
            continue
        out.append({
            "acc": acc, "filed": pday(s.get("FILING_DATE")), "traded": pday(t.get("TRANS_DATE")),
            "issuer": s.get("ISSUERCIK"), "company": s.get("ISSUERNAME"), "ticker": ticker,
            "owner": o.get("RPTOWNERCIK"), "name": o.get("RPTOWNERNAME"),
            "rel": (o.get("RPTOWNER_RELATIONSHIP") or ""), "title": (o.get("RPTOWNER_TITLE") or ""),
            "shares": shares, "price": price, "value": shares * price,
            "plan": (s.get("AFF10B5ONE") or "").strip() in ("1", "true", "Y"),
        })
    log(f"  {y}q{q}: {len(out)} purchase lines")
    return out


def buy_events(rows):
    """Group purchases into events: same insider, same company, trades <= 14 days apart."""
    by = defaultdict(list)
    for r in rows:
        if r["filed"] and r["traded"] and r["owner"] and r["issuer"]:
            by[(r["owner"], r["issuer"])].append(r)
    events = []
    for (owner, issuer), rs in by.items():
        rs.sort(key=lambda r: r["traded"])
        cur = None
        for r in rs:
            if cur and (r["traded"] - cur["last"]).days <= SPREE_DAYS:
                cur["value"] += r["value"]
                cur["shares"] += r["shares"]
                cur["last"] = r["traded"]
                cur["plan"] = cur["plan"] and r["plan"]
                continue
            cur = {"owner": owner, "issuer": issuer, "name": r["name"], "ticker": r["ticker"],
                   "company": r["company"], "rel": r["rel"], "title": r["title"], "filed": r["filed"],
                   "traded": r["traded"], "last": r["traded"], "value": r["value"], "shares": r["shares"],
                   "plan": r["plan"]}
            events.append(cur)
    for e in events:
        e["price"] = e["value"] / e["shares"]
        e["value_ok"] = e["value"] >= MIN_VALUE
    events = [e for e in events if e["value_ok"]]
    events.sort(key=lambda e: e["filed"])
    return events


def role(e):
    t = (e["title"] + " " + e["rel"]).lower()
    if any(k in t for k in TOP_ROLES) and "vice" not in t:
        return "CEO/CFO/President/Chair"
    if "officer" in t:
        return "Other officer"
    if "director" in t:
        return "Director"
    return "10% owner / other"


# ---------------------------------------------------------------- 2. prices

def fetch(ticker):
    t = ticker.replace(".", "-")
    for i in range(3):
        try:
            r = requests.get(PRICE_URL.format(t=t), params={"period1": 1104537600, "period2": int(time.time()), "interval": "1d",
                                     "events": "split"},
                             headers={"User-Agent": UA_BROWSER}, timeout=30)
            if r.status_code == 429:
                time.sleep(5 + 10 * i)
                continue
            if r.status_code != 200:
                return None
            res = (r.json().get("chart", {}).get("result") or [None])[0]
            if not res:
                return None
            ts = res.get("timestamp") or []
            ind = res.get("indicators", {})
            close = (ind.get("quote") or [{}])[0].get("close") or []
            adj = (ind.get("adjclose") or [{}])[0].get("adjclose") or close
            days, c, a = [], [], []
            for k, x in enumerate(ts):
                if k < len(close) and close[k] and k < len(adj) and adj[k]:
                    days.append(datetime.fromtimestamp(x, timezone.utc).date())
                    c.append(close[k])
                    a.append(adj[k])
            splits = []
            for s in ((res.get("events") or {}).get("splits") or {}).values():
                if s.get("numerator") and s.get("denominator"):
                    splits.append((datetime.fromtimestamp(s["date"], timezone.utc).date(),
                                   s["numerator"] / s["denominator"]))
            return {"d": days, "c": c, "a": a, "splits": splits} if len(days) > 20 else None
        except Exception:
            time.sleep(2)
    return None


def load_prices(tickers):
    out = {}
    tickers = sorted(tickers)
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, (t, p) in enumerate(zip(tickers, ex.map(fetch, tickers))):
            if p:
                out[t] = p
            if i % 500 == 0:
                log(f"  prices {i}/{len(tickers)}, found {len(out)}")
    return out


def measure(e, p, spy):
    """Fill e['r3'], e['r6'] excess returns vs SPY, or leave None. Checks the price matches the filing."""
    d = p["d"]
    k = bisect_right(d, e["traded"]) - 1
    if k < 0:
        return
    factor = 1.0
    for when, ratio in p["splits"]:
        if when > e["traded"]:
            factor *= ratio
    if not (0.6 <= (e["price"] / factor) / p["c"][k] <= 1.6):     # wrong company behind a reused ticker etc.
        e["bad_price"] = True
        return
    i = bisect_right(d, e["filed"])            # first trading day after the filing date
    if i >= len(d):
        return
    sd = spy["d"]
    si = bisect_right(sd, d[i] - timedelta(days=1))
    if si >= len(sd):
        return
    year_ago = bisect_right(d, d[i] - timedelta(days=365))
    window = p["c"][year_ago:i + 1]
    e["drop"] = 1 - p["c"][i] / max(window) if window else None
    for h, key in ((H3, "r3"), (H6, "r6")):
        if i + h < len(d) and si + h < len(sd):
            r = p["a"][i + h] / p["a"][i] - 1
            s = spy["a"][si + h] / spy["a"][si] - 1
            e[key] = r - s
            e[key + "_raw"] = r
    e["exit6"] = d[i + H6] if i + H6 < len(d) else None


# ---------------------------------------------------------------- 3. analysis

def stats(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 20:
        return None
    vals_c = [max(min(v, 3.0), -1.0) for v in vals]          # cap crazy +1000% outliers
    return {"n": len(vals), "mean": st.mean(vals_c), "median": st.median(vals),
            "win": sum(1 for v in vals if v > 0) / len(vals)}


def fmt(s):
    if not s:
        return "| – | – | – | – |"
    return f"| {s['n']:,} | {s['mean'] * 100:+.1f}% | {s['median'] * 100:+.1f}% | {s['win'] * 100:.0f}% |"


def history_groups(events):
    """Label each event by what the insider's EARLIER, already finished results looked like."""
    by_owner = defaultdict(list)
    for e in events:
        by_owner[e["owner"]].append(e)
    for evs in by_owner.values():
        evs.sort(key=lambda e: e["filed"])
        for k, e in enumerate(evs):
            known = [x["r6"] for x in evs[:k] if x.get("r6") is not None and x.get("exit6")
                     and x["exit6"] < e["filed"]]
            any_before = k > 0
            e["prior_n"] = len(known)
            e["prior_avg"] = st.mean(known) if known else None
            if not any_before:
                e["group"] = "First buy (no history)"
            elif not known:
                e["group"] = "Earlier buys, results not known yet"
            elif len(known) == 1:
                e["group"] = "1 earlier result: won" if known[0] > 0 else "1 earlier result: lost"
            else:
                avg, wins = st.mean(known), sum(1 for v in known if v > 0) / len(known)
                if avg > 0.10 and wins >= 0.6:
                    e["group"] = "Proven winner (2+, avg > +10%, 60%+ wins)"
                elif avg > 0:
                    e["group"] = "Past winner (2+, avg > 0)"
                else:
                    e["group"] = "Past loser (2+, avg <= 0)"


def clusters(events):
    by_issuer = defaultdict(list)
    for e in events:
        by_issuer[e["issuer"]].append(e)
    for evs in by_issuer.values():
        evs.sort(key=lambda e: e["filed"])
        for e in evs:
            e["cluster"] = len({x["owner"] for x in evs if abs((x["filed"] - e["filed"]).days) <= 30}) >= 3


def table(title, groups, events, order=None):
    lines = [f"\n### {title}", "| Group | 6M buys | 6M avg vs S&P | 6M median | 6M beat S&P | 3M avg vs S&P |",
             "|---|---|---|---|---|---|"]
    keys = order or sorted(groups)
    for g in keys:
        evs = groups.get(g) or []
        s6, s3 = stats([e.get("r6") for e in evs]), stats([e.get("r3") for e in evs])
        if not s6:
            continue
        lines.append(f"| {g} {fmt(s6)} {s3['mean'] * 100:+.1f}% |" if s3 else f"| {g} {fmt(s6)} – |")
    return "\n".join(lines)


def group_by(events, fn):
    g = defaultdict(list)
    for e in events:
        k = fn(e)
        if k:
            g[k].append(e)
    return g


def main():
    start = int(os.environ.get("START_YEAR") or 2006)
    end = int(os.environ.get("END_YEAR") or date.today().year)
    ua = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT required")
    session = requests.Session()
    session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})

    log(f"1/4 Downloading SEC insider data {start}-{end}")
    rows = []
    for y in range(start, end + 1):
        for q in range(1, 5):
            try:
                rows += load_quarter(session, y, q)
            except Exception as e:
                log(f"  {y}q{q} failed: {e}")
            time.sleep(0.5)
    events = buy_events(rows)
    del rows
    log(f"  buy events $25K+: {len(events):,}")

    log("2/4 Downloading prices")
    prices = load_prices({e["ticker"] for e in events} | {"SPY"})
    spy = prices.get("SPY")
    if not spy:
        sys.exit("No SPY prices")

    log("3/4 Measuring results")
    for e in events:
        p = prices.get(e["ticker"])
        if p:
            measure(e, p, spy)
    measured = [e for e in events if e.get("r6") is not None or e.get("r3") is not None]
    bad = sum(1 for e in events if e.get("bad_price"))
    log(f"  measured {len(measured):,} of {len(events):,} (no prices: "
        f"{sum(1 for e in events if e['ticker'] not in prices):,}, price mismatch: {bad:,})")
    history_groups(events)
    clusters(events)
    ev = [e for e in events if e.get("r6") is not None or e.get("r3") is not None]

    log("4/4 Writing results")
    order = ["First buy (no history)", "Earlier buys, results not known yet", "1 earlier result: lost",
             "1 earlier result: won", "Past loser (2+, avg <= 0)", "Past winner (2+, avg > 0)",
             "Proven winner (2+, avg > +10%, 60%+ wins)"]
    out = [f"# Insider Radar backtest ({start}–{end})",
           f"Buy events ($25K+ open-market purchases, same insider + company within 14 days = one event): "
           f"**{len(events):,}**, with prices: **{len(ev):,}**.",
           "Entry: close of the first trading day after the Form 4 filing. Results are returns minus SPY "
           "over the same days (dividends included). Averages cap single results at +300% so one "
           "moonshot can't fake a result.",
           table("ALL buys", {"All buy events": ev}, ev),
           table("Main question: does the insider's track record predict the next buy?",
                 group_by(ev, lambda e: e.get("group")), ev, order)]
    for a, b in ((2006, 2012), (2013, 2018), (2019, 2026)):
        part = [e for e in ev if a <= e["filed"].year <= b]
        out.append(table(f"Same question, only {a}–{b} (does it hold in every period?)",
                         group_by(part, lambda e: e.get("group")), part, order))
    out.append(table("By role", group_by(ev, role), ev))
    out.append(table("By price drop from 1-year high", group_by(
        ev, lambda e: None if e.get("drop") is None else "40%+ below high" if e["drop"] >= .4 else
        "20-40% below" if e["drop"] >= .2 else "within 20% of high"), ev))
    out.append(table("By size", group_by(
        ev, lambda e: "$1M+" if e["value"] >= 1e6 else "$250K-1M" if e["value"] >= 2.5e5 else
        "$100-250K" if e["value"] >= 1e5 else "$25-100K"), ev))
    out.append(table("Cluster buys (3+ insiders within 30 days)", group_by(
        ev, lambda e: "Cluster" if e.get("cluster") else "Single"), ev))
    out.append(table("Strategy ideas", {
        "CEO/CFO buy after 30%+ drop": [e for e in ev if role(e).startswith("CEO") and (e.get("drop") or 0) >= .3],
        "Cluster + 30%+ drop": [e for e in ev if e.get("cluster") and (e.get("drop") or 0) >= .3],
        "Proven winner buys again": [e for e in ev if e.get("group", "").startswith("Proven")],
        "Proven winner + $250K+": [e for e in ev if e.get("group", "").startswith("Proven") and e["value"] >= 2.5e5],
    }, ev))
    out.append("\n_Survivorship bias: bankrupt/acquired companies often have no prices and drop out. "
               "Compare groups with each other rather than trusting the absolute numbers._")
    md = "\n".join(out)
    with open(os.path.join(HERE, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(md)
    log(md)

    # insider ranking: everyone with 3+ finished results
    by_owner = defaultdict(list)
    for e in ev:
        if e.get("r6") is not None:
            by_owner[e["owner"]].append(e)
    rank = []
    for owner, evs in by_owner.items():
        if len(evs) >= 3:
            r = [x["r6"] for x in evs]
            last = max(evs, key=lambda x: x["filed"])
            rank.append([owner, last["name"], last["ticker"], role(last), len(r),
                         round(st.mean([max(min(v, 3), -1) for v in r]), 4),
                         round(sum(1 for v in r if v > 0) / len(r), 3), last["filed"].isoformat()])
    rank.sort(key=lambda x: (-x[6], -x[5]))
    with open(os.path.join(HERE, "insider_ranking.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["owner_cik", "name", "last_ticker", "role", "results", "avg_excess_6m", "win_rate", "last_buy"])
        w.writerows(rank)
    log(f"Ranking: {len(rank)} insiders with 3+ results")

    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        try:
            requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                          "content": "📊 Backtest finished, results attached."})},
                          files={"files[0]": ("backtest_results.md", md.encode(), "text/markdown")}, timeout=60)
        except Exception as e:
            log(f"Discord post failed: {e}")


if __name__ == "__main__":
    main()
