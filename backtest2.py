"""
Backtest round 2: which KIND of insider buy beats the market?

Same data and rules as backtest.py (SEC Form 4 since 2006, entry = day after the filing,
returns minus the benchmark, dividends included), plus new questions:

  1. Market crashes      buys while the S&P 500 is 10% / 20%+ below its 1-year high
  2. Skin in the game    how much the buy grows the insider's own stake (new position, +100%, ...)
  3. Unusual buys        first buy we have seen, buy after a 1-3+ year pause, buy after selling
  4. Holding period      1, 3, 6, 12 and 24 months
  5. Speed               how much the stock moves on the day the Form 4 comes out
  6. Company size        by average daily dollar volume (tiny / small / mid / large)

Every table is shown for the whole period and for 2016-2026 only (does it still work?).
Env: START_YEAR, END_YEAR, BENCH (SPY default), SEC_USER_AGENT, WEBHOOK_PROOF.
Output: backtest2_results.md
"""

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

from backtest import (MIN_VALUE, PRICE_URL, SPREE_DAYS, UA_BROWSER, ZIP_URL, fnum, log, pday, read_tsv, role,
                      score)

HERE = os.path.dirname(os.path.abspath(__file__))
HORIZONS = [("1M", 21), ("3M", 63), ("6M", 126), ("12M", 252), ("24M", 504)]


# ---------------------------------------------------------------- data

def load_quarter(session, y, q):
    url = ZIP_URL.format(y=y, q=q)
    r = None
    for i in range(4):
        r = session.get(url, timeout=120)
        if r.status_code in (200, 404):
            break
        time.sleep(3 + 5 * i)
    if r is None or r.status_code != 200:
        return [], []
    z = zipfile.ZipFile(io.BytesIO(r.content))
    subs = {s["ACCESSION_NUMBER"]: s for s in read_tsv(z, "SUBMISSION.TSV")
            if (s.get("DOCUMENT_TYPE") or "").startswith("4")}
    owners = {}
    for o in read_tsv(z, "REPORTINGOWNER.TSV"):
        owners.setdefault(o["ACCESSION_NUMBER"], o)
    buys, sells = [], []
    for t in read_tsv(z, "NONDERIV_TRANS.TSV"):
        acc = t["ACCESSION_NUMBER"]
        code = (t.get("TRANS_CODE") or "").strip()
        if code not in ("P", "S") or acc not in subs or acc not in owners:
            continue
        s, o = subs[acc], owners[acc]
        if code == "S":
            d = pday(t.get("TRANS_DATE"))
            if d:
                sells.append((o.get("RPTOWNERCIK"), s.get("ISSUERCIK"), d))
            continue
        shares, price = fnum(t.get("TRANS_SHARES")), fnum(t.get("TRANS_PRICEPERSHARE"))
        if not shares or not price or price <= 0:
            continue
        ticker = (s.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{1,5}([.-][A-Z])?", ticker) or ticker in ("NONE", "NA", "N/A"):
            continue
        buys.append({
            "filed": pday(s.get("FILING_DATE")), "traded": pday(t.get("TRANS_DATE")),
            "issuer": s.get("ISSUERCIK"), "ticker": ticker, "owner": o.get("RPTOWNERCIK"),
            "name": o.get("RPTOWNERNAME"), "rel": o.get("RPTOWNER_RELATIONSHIP") or "",
            "title": o.get("RPTOWNER_TITLE") or "", "shares": shares, "price": price,
            "owned_after": fnum(t.get("SHRS_OWND_FOLWNG_TRANS")),
            "plan": (s.get("AFF10B5ONE") or "").strip() in ("1", "true", "Y"),
        })
    log(f"  {y}q{q}: {len(buys)} buys, {len(sells)} sells")
    return buys, sells


def buy_events(rows):
    by = defaultdict(list)
    for r in rows:
        if r["filed"] and r["traded"] and r["owner"] and r["issuer"]:
            by[(r["owner"], r["issuer"])].append(r)
    events = []
    for (owner, issuer), rs in by.items():
        rs.sort(key=lambda r: (r["traded"], r["owned_after"] or 0))
        cur = None
        for r in rs:
            if cur and (r["traded"] - cur["last"]).days <= SPREE_DAYS:
                cur["value"] += r["shares"] * r["price"]
                cur["shares"] += r["shares"]
                cur["last"] = r["traded"]
                cur["owned_after"] = r["owned_after"] or cur["owned_after"]
                cur["plan"] = cur["plan"] and r["plan"]
                continue
            cur = dict(r, value=r["shares"] * r["price"], last=r["traded"])
            events.append(cur)
    events = [e for e in events if e["value"] >= MIN_VALUE]
    for e in events:
        e["price"] = e["value"] / e["shares"]
        before = (e["owned_after"] or 0) - e["shares"]
        e["stake"] = None if e["owned_after"] is None else ("new" if before <= 0 else e["shares"] / before)
    events.sort(key=lambda e: e["filed"])
    return events


def fetch(ticker):
    t = ticker.replace(".", "-")
    for i in range(3):
        try:
            r = requests.get(PRICE_URL.format(t=t), params={"period1": 1104537600, "period2": int(time.time()),
                                                           "interval": "1d", "events": "split"},
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
            q = (res.get("indicators", {}).get("quote") or [{}])[0]
            close, vol = q.get("close") or [], q.get("volume") or []
            adj = ((res.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")) or close
            d, c, a, v = [], [], [], []
            for k, x in enumerate(ts):
                if k < len(close) and close[k] and k < len(adj) and adj[k]:
                    d.append(datetime.fromtimestamp(x, timezone.utc).date())
                    c.append(close[k])
                    a.append(adj[k])
                    v.append((vol[k] if k < len(vol) and vol[k] else 0) or 0)
            splits = []
            for s in ((res.get("events") or {}).get("splits") or {}).values():
                if s.get("numerator") and s.get("denominator"):
                    splits.append((datetime.fromtimestamp(s["date"], timezone.utc).date(),
                                   s["numerator"] / s["denominator"]))
            return {"d": d, "c": c, "a": a, "v": v, "splits": splits} if len(d) > 20 else None
        except Exception:
            time.sleep(2)
    return None


def load_prices(tickers):
    out, tickers = {}, sorted(tickers)
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, (t, p) in enumerate(zip(tickers, ex.map(fetch, tickers))):
            if p:
                out[t] = p
            if i % 1000 == 0:
                log(f"  prices {i}/{len(tickers)}, found {len(out)}")
    return out


# ---------------------------------------------------------------- measure

def measure(e, p, bench, spy):
    d = p["d"]
    k = bisect_right(d, e["traded"]) - 1
    if k < 0:
        return
    factor = 1.0
    for when, ratio in p["splits"]:
        if when > e["traded"]:
            factor *= ratio
    if not (0.6 <= (e["price"] / factor) / p["c"][k] <= 1.6):
        return
    i = bisect_right(d, e["filed"])                       # first day after the filing date
    pre = bisect_right(d, e["filed"] - timedelta(days=1)) - 1   # last close before the filing date
    if i >= len(d) or pre < 0:
        return
    bi = bisect_right(bench["d"], d[i] - timedelta(days=1))
    bp = bisect_right(bench["d"], d[pre] - timedelta(days=1))
    if bi >= len(bench["d"]):
        return
    for label, h in HORIZONS:
        if i + h < len(d) and bi + h < len(bench["d"]):
            e[label] = (p["a"][i + h] / p["a"][i] - 1) - (bench["a"][bi + h] / bench["a"][bi] - 1)
    # speed: move from the last close before the filing to our entry, vs the benchmark
    if bp < len(bench["d"]) and bp <= bi:
        e["jump"] = (p["a"][i] / p["a"][pre] - 1) - (bench["a"][bi] / bench["a"][bp] - 1)
        if pre + 126 < len(d) and bp + 126 < len(bench["d"]):
            e["6M_instant"] = (p["a"][pre + 126] / p["a"][pre] - 1) - (bench["a"][bp + 126] / bench["a"][bp] - 1)
    lo = max(0, i - 20)
    dv = [p["c"][j] * p["v"][j] for j in range(lo, i) if p["v"][j]]
    e["dollar_vol"] = st.mean(dv) if dv else None
    ya = bisect_right(d, d[i] - timedelta(days=365))
    win = p["c"][ya:i + 1]
    e["drop"] = 1 - p["c"][i] / max(win) if win else None
    si = bisect_right(spy["d"], e["filed"]) - 1
    if si > 0:
        sy = bisect_right(spy["d"], spy["d"][si] - timedelta(days=365))
        e["mkt_dd"] = 1 - spy["c"][si] / max(spy["c"][sy:si + 1])


def tag_history(events, sells):
    sold = defaultdict(list)
    for o, iss, d in sells:
        sold[(o, iss)].append(d)
    by = defaultdict(list)
    for e in events:
        by[(e["owner"], e["issuer"])].append(e)
    start = min(e["filed"] for e in events)
    for key, evs in by.items():
        evs.sort(key=lambda e: e["traded"])
        sd = sorted(sold.get(key, []))
        prev = None
        for e in evs:
            t = e["traded"]
            if (t - start).days < 3 * 365:
                e["history"] = None
            elif prev and (t - prev).days <= 365:
                e["history"] = "Regular buyer (bought in last 12 months)"
            elif any(0 < (t - s).days <= 365 for s in sd):
                e["history"] = "Sold in last 12 months, now buying"
            elif prev is None:
                e["history"] = "First buy we've seen (3+ years of data)"
            elif (t - prev).days <= 3 * 365:
                e["history"] = "Buys again after 1-3 year pause"
            else:
                e["history"] = "Buys again after 3+ year pause"
            prev = t


# ---------------------------------------------------------------- tables

def cap(v):
    return max(min(v, 3.0), -1.0)


def row(label, evs):
    n6 = [e["6M"] for e in evs if e.get("6M") is not None]
    if len(n6) < 30:
        return None
    cells = [label, f"{len(n6):,}"]
    for lab, _ in HORIZONS:
        vals = [cap(e[lab]) for e in evs if e.get(lab) is not None]
        cells.append(f"{st.mean(vals) * 100:+.1f}%" if len(vals) >= 30 else "–")
    cells.append(f"{st.median(n6) * 100:+.1f}%")
    cells.append(f"{sum(v > 0 for v in n6) / len(n6) * 100:.0f}%")
    return "| " + " | ".join(cells) + " |"


def table(title, groups, order=None):
    lines = [f"\n### {title}", "| Group | Buys | 1M | 3M | 6M | 12M | 24M | 6M median | 6M beat |",
             "|---|---|---|---|---|---|---|---|---|"]
    for g in order or sorted(groups):
        r = row(g, groups.get(g, []))
        if r:
            lines.append(r)
    return "\n".join(lines)


def grp(evs, fn):
    g = defaultdict(list)
    for e in evs:
        k = fn(e)
        if k:
            g[k].append(e)
    return g


def both(title, evs, fn, order=None):
    recent = [e for e in evs if e["filed"].year >= 2016]
    return (table(f"{title} (all years)", grp(evs, fn), order) + "\n" +
            table(f"{title} (2016–2026 only)", grp(recent, fn), order))


def main():
    start = int(os.environ.get("START_YEAR") or 2006)
    end = int(os.environ.get("END_YEAR") or date.today().year)
    bench_t = (os.environ.get("BENCH") or "SPY").upper()
    ua = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT required")
    session = requests.Session()
    session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    log("1/4 SEC data")
    buys, sells = [], []
    for y in range(start, end + 1):
        for q in range(1, 5):
            try:
                b, s = load_quarter(session, y, q)
                buys += b
                sells += s
            except Exception as ex:
                log(f"  {y}q{q} failed: {ex}")
    events = buy_events(buys)
    del buys
    log(f"  events: {len(events):,}, sells: {len(sells):,}")
    tag_history(events, sells)
    del sells
    log("2/4 prices")
    prices = load_prices({e["ticker"] for e in events} | {bench_t, "SPY"})
    bench, spy = prices[bench_t], prices["SPY"]
    log("3/4 measuring")
    for e in events:
        p = prices.get(e["ticker"])
        if p:
            measure(e, p, bench, spy)
    ev = [e for e in events if e.get("6M") is not None]
    log(f"  measured {len(ev):,}")

    out = [f"# Insider Radar backtest round 2 ({start}–{end}) · benchmark {bench_t}",
           f"Buy events with a 6-month result: **{len(ev):,}**. Numbers = average return minus {bench_t} "
           "after buying the day after the Form 4 (single results capped at +300%).",
           table("Holding period: all buys", {"All buys": ev})]
    out.append(both("1. Market crashes (S&P 500 distance from its 1-year high when the buy is filed)", ev,
                    lambda e: None if e.get("mkt_dd") is None else "Market 20%+ down (crash)" if e["mkt_dd"] >= .2
                    else "Market 10-20% down (correction)" if e["mkt_dd"] >= .1 else "Market within 10% (normal)",
                    ["Market within 10% (normal)", "Market 10-20% down (correction)", "Market 20%+ down (crash)"]))
    out.append(both("2. Skin in the game (how much the buy grows their own stake)", ev,
                    lambda e: None if e.get("stake") is None else "New position" if e["stake"] == "new" else
                    "+100%+ stake" if e["stake"] >= 1 else "+50-100%" if e["stake"] >= .5 else
                    "+20-50%" if e["stake"] >= .2 else "+5-20%" if e["stake"] >= .05 else "under +5%",
                    ["under +5%", "+5-20%", "+20-50%", "+50-100%", "+100%+ stake", "New position"]))
    out.append(both("3. Unusual buys (what the insider did before)", ev, lambda e: e.get("history"),
                    ["Regular buyer (bought in last 12 months)", "Buys again after 1-3 year pause",
                     "Buys again after 3+ year pause", "First buy we've seen (3+ years of data)",
                     "Sold in last 12 months, now buying"]))
    out.append(both("6. Company size (average daily trading in $)", ev,
                    lambda e: None if not e.get("dollar_vol") else "Tiny (< $1M/day)" if e["dollar_vol"] < 1e6 else
                    "Small ($1-20M/day)" if e["dollar_vol"] < 2e7 else "Mid ($20-200M/day)" if e["dollar_vol"] < 2e8
                    else "Large ($200M+/day)",
                    ["Tiny (< $1M/day)", "Small ($1-20M/day)", "Mid ($20-200M/day)", "Large ($200M+/day)"]))
    # 5. speed
    sp = ["\n### 5. Speed: how much is gone before you can buy?",
          "| Group | Buys | Move on filing day (vs index) | 6M if bought BEFORE the news | 6M buying the day after |",
          "|---|---|---|---|---|"]
    for label, evs in (("All buys", ev),
                       ("Score 8-10", [e for e in ev if score(e) >= 8]),
                       ("CEO/CFO", [e for e in ev if role(e).startswith("CEO")]),
                       ("Tiny companies", [e for e in ev if (e.get("dollar_vol") or 1e9) < 1e6]),
                       ("2016-2026 only", [e for e in ev if e["filed"].year >= 2016])):
        j = [cap(e["jump"]) for e in evs if e.get("jump") is not None]
        a = [cap(e["6M_instant"]) for e in evs if e.get("6M_instant") is not None]
        b = [cap(e["6M"]) for e in evs]
        if len(j) >= 30:
            sp.append(f"| {label} | {len(j):,} | {st.mean(j) * 100:+.2f}% (median {st.median(j) * 100:+.2f}%) | "
                      f"{st.mean(a) * 100:+.1f}% | {st.mean(b) * 100:+.1f}% |")
    out.append("\n".join(sp))
    # best combinations that still worked recently
    combos = {
        "Crash + CEO/CFO": lambda e: (e.get("mkt_dd") or 0) >= .2 and role(e).startswith("CEO"),
        "Crash + stake +50%": lambda e: (e.get("mkt_dd") or 0) >= .2 and e.get("stake") not in (None,) and (e["stake"] == "new" or e["stake"] >= .5),
        "Stake +50% + stock 30%+ down": lambda e: e.get("stake") is not None and (e["stake"] == "new" or e["stake"] >= .5) and (e.get("drop") or 0) >= .3,
        "Pause 1y+ + CEO/CFO": lambda e: (e.get("history") or "").startswith(("Buys again", "First")) and role(e).startswith("CEO"),
        "Pause 1y+ + stake +50%": lambda e: (e.get("history") or "").startswith(("Buys again", "First")) and e.get("stake") is not None and (e["stake"] == "new" or e["stake"] >= .5),
        "Small/tiny + CEO/CFO + stake +20%": lambda e: (e.get("dollar_vol") or 1e9) < 2e7 and role(e).startswith("CEO") and e.get("stake") is not None and (e["stake"] == "new" or e["stake"] >= .2),
        "Large company + CEO/CFO": lambda e: (e.get("dollar_vol") or 0) >= 2e8 and role(e).startswith("CEO"),
    }
    out.append("\n## Combinations")
    for k, f in combos.items():
        sub = [e for e in ev if f(e)]
        out.append(table(f"{k}", {"all years": sub, "2016-2026": [e for e in sub if e['filed'].year >= 2016]},
                         ["all years", "2016-2026"]))
    out.append("\n_Survivorship bias: companies that went bankrupt or were bought often have no prices and "
               "drop out. Compare groups with each other._")
    md = "\n".join(out)
    with open(os.path.join(HERE, "backtest2_results.md"), "w", encoding="utf-8") as f:
        f.write(md)
    log(md)
    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        try:
            requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                          "content": "📊 Backtest round 2 finished."})},
                          files={"files[0]": ("backtest2_results.md", md.encode(), "text/markdown")}, timeout=60)
        except Exception as ex:
            log(f"Discord post failed: {ex}")


if __name__ == "__main__":
    main()
