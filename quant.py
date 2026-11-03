"""
Quant pass on the 3+ insider cluster strategy: can filters raise the win rate WITHOUT overfitting?

Discipline:
  - Rules are chosen ONLY on 2006-2015 (train). 2016-2026 (test) is never used for choosing.
  - Every feature is known before the entry (next day's open after the 3rd insider's Form 4).
  - Cluster size counts only insiders whose Form 4 was public by the signal day (no look-ahead).
  - Costs 0.25% per round trip in the trade tables; the portfolio is re-run at 0.5% and 1%.
  - Placebo: same stocks 126 trading days later. A real edge must beat it on the test years.
Output: quant_results.md
"""

import json
import os
import statistics as st
import sys
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests

import clusterport
from backtest import PRICE_URL, UA_BROWSER, log, role
from backtest2 import load_quarter

HERE = os.path.dirname(os.path.abspath(__file__))
COST = 0.0025
H = (5, 10)
SPLIT = 2016
PLACEBO = 126


# ------------------------------------------------------------------ data

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
            q = (res["indicators"]["quote"] or [{}])[0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q.get("close")
            vol = q.get("volume") or []
            d, o, c, a, v = [], [], [], [], []
            for k, x in enumerate(res.get("timestamp") or []):
                cl, op, ad = q["close"][k], q["open"][k], adj[k]
                if cl and op and ad:
                    d.append(datetime.fromtimestamp(x, timezone.utc).date())
                    c.append(cl)
                    a.append(ad)
                    o.append(op * ad / cl)
                    v.append((vol[k] if k < len(vol) and vol[k] else 0) or 0)
            splits = [(datetime.fromtimestamp(x["date"], timezone.utc).date(), x["numerator"] / x["denominator"])
                      for x in ((res.get("events") or {}).get("splits") or {}).values()
                      if x.get("numerator") and x.get("denominator")]
            return {"d": d, "o": o, "c": c, "a": a, "v": v, "splits": splits} if len(d) > 20 else None
        except Exception:
            time.sleep(2)
    return None


def detect(rows):
    """3+ distinct insiders ($25K+ each) within 7 days; only insiders public by the signal day count."""
    by = defaultdict(list)
    for r in rows:
        if r["filed"] and r["traded"] and r["issuer"] and r["owner"] and r["shares"] * r["price"] >= 25_000:
            by[r["issuer"]].append(r)
    out = []
    for iss, rs in by.items():
        rs.sort(key=lambda r: r["traded"])
        last = None
        for i, r in enumerate(rs):
            lo = r["traded"] - timedelta(days=7)
            first = {}
            for x in rs[:i + 1]:
                if x["traded"] >= lo:
                    first.setdefault(x["owner"], x)
            if len(first) < 3 or (last is not None and (r["traded"] - last).days <= 60):
                continue
            last = r["traded"]
            known3 = sorted(first.values(), key=lambda x: x["filed"])[:3]
            sig = max(x["filed"] for x in known3)
            start = min(x["traded"] for x in first.values())
            own = {}
            for x in rs:
                if start <= x["traded"] <= start + timedelta(days=7) and x["filed"] <= sig:
                    o = own.setdefault(x["owner"], {"value": 0.0, "row": x, "plan": False, "stakes": []})
                    o["value"] += x["shares"] * x["price"]
                    o["plan"] = o["plan"] or x["plan"]
                    if x["owned_after"] and x["owned_after"] > x["shares"]:
                        o["stakes"].append(x["shares"] / (x["owned_after"] - x["shares"]))
                    elif x["owned_after"] is not None:
                        o["stakes"].append(9.99)          # new position
            stakes = [max(o["stakes"]) for o in own.values() if o["stakes"]]
            roles = {role(o["row"]) for o in own.values()}
            out.append({
                "ticker": r["ticker"], "issuer": iss, "filed": sig, "traded": r["traded"], "price": r["price"],
                "n": len(own), "value": sum(o["value"] for o in own.values()),
                "ceo": any(x.startswith("CEO") for x in roles),
                "plan": any(o["plan"] for o in own.values()),
                "stake": st.median(stakes) if stakes else None,
                "lag": max((x["filed"] - x["traded"]).days for x in known3)})
    return out


def features(e, p, spy, sells):
    d = p["d"]
    k = bisect_right(d, e["traded"]) - 1
    f = 1.0
    for w, ratio in p["splits"]:
        if w > e["traded"]:
            f *= ratio
    if k < 0 or not (0.6 <= (e["price"] / f) / p["c"][k] <= 1.6):
        return False
    i = bisect_right(d, e["filed"])
    if i < 61 or i >= len(d):
        return False
    si = bisect_left(spy["d"], d[i])
    if si >= len(spy["d"]) or spy["d"][si] != d[i] or si < 201:
        return False
    a, c = p["a"], p["c"]
    e["i"], e["si"] = i, si
    e["dv"] = st.mean(c[j] * p["v"][j] for j in range(i - 20, i))
    hi = max(c[max(0, i - 252):i])
    e["drop"] = 1 - c[i - 1] / hi if hi else None
    e["ret20"] = a[i - 1] / a[i - 21] - 1
    e["ret5"] = a[i - 1] / a[i - 6] - 1
    e["dmove"] = (a[i - 1] / a[i - 2] - 1) - (spy["a"][si - 1] / spy["a"][si - 2] - 1)
    e["gap"] = p["o"][i] / a[i - 1] - 1
    rets = [a[j] / a[j - 1] - 1 for j in range(i - 60, i)]
    e["vol"] = st.pstdev(rets)
    e["bull"] = spy["a"][si - 1] > st.mean(spy["a"][si - 201:si - 1])
    ds = sells.get(e["issuer"], [])
    e["sells"] = bisect_left(ds, d[i]) - bisect_left(ds, d[i] - timedelta(days=90))
    for key, shift in (("r", 0), ("pl", PLACEBO)):
        j, sj = i + shift, si + shift
        for h in H:
            if j + h < len(d) and sj + h < len(spy["d"]):
                raw = max(min(a[j + h] / p["o"][j] - 1 - COST, 3), -1)
                e[f"{key}{h}"] = raw - (spy["a"][sj + h] / spy["o"][sj] - 1)
                if key == "r":
                    e[f"raw{h}"] = raw
    return "r5" in e


# ------------------------------------------------------------------ stats

def summ(evs, h=5):
    v = [e[f"r{h}"] for e in evs if f"r{h}" in e]
    if len(v) < 20:
        return None
    raw = [e[f"raw{h}"] for e in evs if f"raw{h}" in e]
    sd = st.pstdev(v) or 1
    trim = sorted(v)[:max(1, int(len(v) * 0.99))]
    return {"n": len(v), "win": sum(x > 0 for x in v) / len(v), "winraw": sum(x > 0 for x in raw) / len(raw),
            "mean": st.mean(v), "med": st.median(v), "trim": st.mean(trim), "t": st.mean(v) / (sd / len(v) ** .5)}


def pct(x):
    return f"{x * 100:+.2f}%"


FILTERS = {
    "4+ insiders known": lambda e: e["n"] >= 4,
    "CEO/CFO in cluster": lambda e: e["ceo"],
    "No 10b5-1 plan": lambda e: not e["plan"],
    "Total $1M+": lambda e: e["value"] >= 1e6,
    "Total under $1M": lambda e: e["value"] < 1e6,
    "Median stake +20%+": lambda e: (e["stake"] or 0) >= .2,
    "Insider price $3+": lambda e: e["price"] >= 3,
    "Trading $1M+/day": lambda e: e["dv"] >= 1e6,
    "Trading under $10M/day": lambda e: e["dv"] < 1e7,
    "Stock 30%+ below 1y high": lambda e: (e["drop"] or 0) >= .3,
    "Stock within 15% of 1y high": lambda e: e["drop"] is not None and e["drop"] < .15,
    "Fell 5%+ last 20 days": lambda e: e["ret20"] <= -.05,
    "Rose last 20 days": lambda e: e["ret20"] > 0,
    "Down last 5 days": lambda e: e["ret5"] < 0,
    "Filing day move under +3%": lambda e: e["dmove"] < .03,
    "Open gap under +2%": lambda e: e["gap"] < .02,
    "Low volatility (<3%/day)": lambda e: e["vol"] < .03,
    "S&P above 200-day avg": lambda e: e["bull"],
    "S&P below 200-day avg": lambda e: not e["bull"],
    "No insider sells 90 days": lambda e: e["sells"] == 0,
    "Filed within 2 days": lambda e: e["lag"] <= 2,
}

BUCKETS = {
    "Insiders known at signal": lambda e: "3" if e["n"] == 3 else "4" if e["n"] == 4 else "5+",
    "Total bought": lambda e: "<$250K" if e["value"] < 2.5e5 else "$250K-1M" if e["value"] < 1e6 else
                              "$1-5M" if e["value"] < 5e6 else "$5M+",
    "CEO/CFO in cluster": lambda e: "yes" if e["ceo"] else "no",
    "Median stake increase": lambda e: None if e["stake"] is None else "<5%" if e["stake"] < .05 else
                                       "5-20%" if e["stake"] < .2 else "20-100%" if e["stake"] < 1 else "100%+/new",
    "Insider price": lambda e: "<$2" if e["price"] < 2 else "$2-5" if e["price"] < 5 else
                               "$5-20" if e["price"] < 20 else "$20+",
    "Traded per day": lambda e: "<$250K" if e["dv"] < 2.5e5 else "$250K-1M" if e["dv"] < 1e6 else
                                "$1-10M" if e["dv"] < 1e7 else "$10M+",
    "Below 1y high": lambda e: None if e["drop"] is None else "<15%" if e["drop"] < .15 else
                               "15-30%" if e["drop"] < .3 else "30-50%" if e["drop"] < .5 else "50%+",
    "Last 20 days": lambda e: "< -20%" if e["ret20"] < -.2 else "-20..-5%" if e["ret20"] < -.05 else
                              "-5..+5%" if e["ret20"] <= .05 else "> +5%",
    "Last 5 days": lambda e: "< -10%" if e["ret5"] < -.1 else "-10..0%" if e["ret5"] < 0 else
                             "0..+10%" if e["ret5"] <= .1 else "> +10%",
    "Filing day vs S&P": lambda e: "< -3%" if e["dmove"] < -.03 else "-3..+3%" if e["dmove"] <= .03 else "> +3%",
    "Gap at entry open": lambda e: "< -2%" if e["gap"] < -.02 else "-2..+2%" if e["gap"] <= .02 else
                                   "+2..+5%" if e["gap"] <= .05 else "> +5%",
    "Daily volatility": lambda e: "<2%" if e["vol"] < .02 else "2-4%" if e["vol"] < .04 else "4%+",
    "S&P vs 200-day avg": lambda e: "above" if e["bull"] else "below",
    "Insider sells 90 days before": lambda e: "none" if e["sells"] == 0 else "some",
    "Filing delay": lambda e: "0-1 days" if e["lag"] <= 1 else "2 days" if e["lag"] == 2 else "3+ days",
}


def greedy(train, h, steps=3, min_n=250):
    chosen, cur = [], train
    base = summ(cur, h)["t"]
    path = []
    for _ in range(steps):
        best = None
        for name, fn in FILTERS.items():
            if name in chosen:
                continue
            sub = [e for e in cur if fn(e)]
            s = summ(sub, h)
            if s and s["n"] >= min_n and (best is None or s["t"] > best[1]["t"]):
                best = (name, s, sub)
        if not best or best[1]["t"] < base + 0.3:
            break
        chosen.append(best[0])
        cur, base = best[2], best[1]["t"]
        path.append(list(chosen))
    return path


def apply(evs, names):
    return [e for e in evs if all(FILTERS[n](e) for n in names)]


def main():
    ua = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT required")
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    log("1/4 SEC data")
    rows, sells = [], defaultdict(list)
    for y in range(2006, date.today().year + 1):
        for q in range(1, 5):
            try:
                b, sl = load_quarter(s, y, q)
                rows += b
                for _, iss, d in sl:
                    sells[iss].append(d)
            except Exception as ex:
                log(f"  {y}q{q}: {ex}")
    for k in sells:
        sells[k].sort()
    clusters = detect(rows)
    del rows
    log(f"  clusters {len(clusters):,}")
    log("2/4 prices")
    tickers = sorted({e["ticker"] for e in clusters} | {"SPY"})
    prices = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, p in zip(tickers, ex.map(fetch, tickers)):
            if p:
                prices[t] = p
    spy = prices["SPY"]
    log("3/4 features")
    ev = [e for e in clusters if prices.get(e["ticker"]) and features(e, prices[e["ticker"]], spy, sells)]
    train = [e for e in ev if e["filed"].year < SPLIT]
    test = [e for e in ev if e["filed"].year >= SPLIT]
    log(f"  measured {len(ev):,} (train {len(train):,}, test {len(test):,})")

    out = ["# Quant pass: making the 3+ insider cluster strategy better (without fooling ourselves)",
           f"Rules picked ONLY on {2006}-{SPLIT - 1} (train), then checked on {SPLIT}-2026 (test, never used for picking). "
           "Entry = next open after the 3rd insider's Form 4 is public. Returns minus S&P (SPY) over the same days, "
           "0.25% cost included. **Win** = beat the S&P. **Win raw** = made money at all. "
           "**Avg w/o top 1%** = average after removing the 1% biggest winners (shows if a few lottery tickets carry it).", ""]

    hdr = ("| Group | Trades | Win | Win raw | Avg | Median | Avg w/o top 1% | t |", "|---|---|---|---|---|---|---|---|")

    def line(label, s):
        return (f"| {label} | {s['n']:,} | {s['win'] * 100:.0f}% | {s['winraw'] * 100:.0f}% | {pct(s['mean'])} | "
                f"{pct(s['med'])} | {pct(s['trim'])} | {s['t']:.1f} |")

    out += ["## 1. Baseline (all 3+ clusters)"]
    for h in H:
        out += [f"\n**Hold {h} days**", *hdr]
        for lab, evs in (("Train 2006-2015", train), ("Test 2016-2026", test)):
            sm = summ(evs, h)
            if sm:
                out.append(line(lab, sm))

    out += ["", "## 2. What separates winners from losers (5-day hold)",
            "Left = train years, right = test years. A useful feature shows the SAME direction on both sides.", "",
            "| Feature | Bucket | Train trades | Train win | Train avg | Test trades | Test win | Test avg |",
            "|---|---|---|---|---|---|---|---|"]
    for feat, fn in BUCKETS.items():
        groups = defaultdict(lambda: ([], []))
        for e in ev:
            b = fn(e)
            if b is not None:
                groups[b][0 if e["filed"].year < SPLIT else 1].append(e)
        for b in sorted(groups):
            a_, t_ = summ(groups[b][0], 5), summ(groups[b][1], 5)
            ca = f"{a_['n']:,} | {a_['win'] * 100:.0f}% | {pct(a_['mean'])}" if a_ else "– | – | –"
            ct = f"{t_['n']:,} | {t_['win'] * 100:.0f}% | {pct(t_['mean'])}" if t_ else "– | – | –"
            out.append(f"| {feat} | {b} | {ca} | {ct} |")

    rules = {"Baseline (all 3+)": []}
    out += ["", "## 3. Automatic rule search (train only), then the honest test"]
    for h in H:
        path = greedy(train, h)
        out += [f"\n**Hold {h} days** — filters added one by one, each must clearly improve the train t-stat", *hdr]
        b_tr, b_te = summ(train, h), summ(test, h)
        out += [line("Baseline train", b_tr), line("Baseline TEST", b_te)]
        for names in path:
            tr, te = summ(apply(train, names), h), summ(apply(test, names), h)
            out.append(line("Train: " + " + ".join(names), tr))
            if te:
                out.append(line("**TEST**: " + " + ".join(names), te))
        if path:
            rules[f"Auto rule ({h}d): " + " + ".join(path[-1])] = path[-1]

    hygiene = ["Insider price $3+", "Trading $1M+/day", "No 10b5-1 plan", "Open gap under +2%"]
    rules["Hygiene rule (set in advance): " + " + ".join(hygiene)] = hygiene
    out += ["", "**Hygiene rule decided in advance (not optimised):** " + ", ".join(hygiene), *hdr]
    for h in H:
        for lab, evs in (("train", train), ("TEST", test)):
            sm = summ(apply(evs, hygiene), h)
            if sm:
                out.append(line(f"{h}d {lab}", sm))

    out += ["", "## 4. Placebo on the TEST years (same stocks 126 trading days later)",
            "| Rule | Hold | Trades | Real − placebo | t |", "|---|---|---|---|---|"]
    for name, names in rules.items():
        for h in H:
            pairs = [(e[f"r{h}"], e[f"pl{h}"]) for e in apply(test, names) if f"r{h}" in e and f"pl{h}" in e]
            if len(pairs) >= 30:
                diff = [x - y for x, y in pairs]
                sd = st.pstdev(diff) or 1
                out.append(f"| {name} | {h}d | {len(diff):,} | {pct(st.mean(diff))} | "
                           f"{st.mean(diff) / (sd / len(diff) ** .5):.1f} |")

    log("4/4 portfolios")
    out += ["", "## 5. Portfolio: 10 slots, idle money in cash, start 2006-04",
            "Same simulator as before. Cost per round trip changed to show how fragile the edge is.", "",
            "| Rule | Hold | Cost | Per year | Since 2016 | Worst drop | Trades/yr |", "|---|---|---|---|---|---|---|"]
    spy_curve = list(zip(spy["d"], spy["a"]))
    sp_a, sp_r = clusterport.stats(spy_curve, spy, date(2006, 4, 1)), clusterport.stats(spy_curve, spy, date(2016, 1, 1))
    out.append(f"| S&P 500 | – | – | {pct(sp_a['cagr'])} | {pct(sp_r['cagr'])} | -{sp_a['mdd'] * 100:.0f}% | – |")
    for name, names in rules.items():
        evs = apply(ev, names)
        for h in H:
            for cost in (0.0025, 0.005, 0.01):
                clusterport.COST = cost
                curve, trades = clusterport.simulate(evs, prices, spy, h, False)
                a_, r_ = clusterport.stats(curve, spy, date(2006, 4, 1)), clusterport.stats(curve, spy, date(2016, 1, 1))
                if a_ and r_:
                    out.append(f"| {name} | {h}d | {cost * 100:.2g}% | **{pct(a_['cagr'])}** | {pct(r_['cagr'])} | "
                               f"-{a_['mdd'] * 100:.0f}% | {trades / a_['years']:.0f} |")
    out.append("\n_Survivorship bias: delisted/bankrupt companies often have no prices. No taxes. "
               "Filters chosen on 2006-2015 only; 2016-2026 is the honest check._")
    md = "\n".join(out)
    with open(os.path.join(HERE, "quant_results.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    log(md)
    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        try:
            requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                          "content": "🧪 Quant pass on clusters finished."})},
                          files={"files[0]": ("quant_results.md", md.encode(), "text/markdown")}, timeout=60)
        except Exception:
            pass


if __name__ == "__main__":
    main()
