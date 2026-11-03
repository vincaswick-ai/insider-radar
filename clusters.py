"""
Cluster buy backtest, done properly.

Cluster = 3+ DIFFERENT insiders of the same company buy on the open market ($25K+ each spree)
within 7 calendar days. The signal date is when the 3rd insider's Form 4 is public
(before that nobody can know it is a cluster). One cluster per company per 60 days.

Entry: next day's OPEN after that filing. Cost: 0.25% round trip (spread/slippage) subtracted.
Horizons: 5, 10, 20, 60, 126 trading days, return minus SPY over the same days.
Placebo: the same stocks 126 trading days later, same horizon. If the real result is not better
than the placebo, the "edge" is just the type of company, not the insider timing.
Compared with single-insider buys (nobody else bought within 30 days).

Filters (alone and stacked): 4+ insiders, CEO/CFO together with a director, no 10b5-1 plans,
small companies (< $20M/day traded), stock 30%+ below its 1-year high.
Output: clusters_results.md
"""

import json
import os
import statistics as st
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests

from backtest import log, role
from backtest2 import load_quarter
from sameday import fetch

HERE = os.path.dirname(os.path.abspath(__file__))
H = [5, 10, 20, 60, 126]
COST = 0.0025
PLACEBO_SHIFT = 126


def detect(rows):
    """Return (clusters, singles) as event dicts."""
    by = defaultdict(list)
    for r in rows:
        if r["filed"] and r["traded"] and r["issuer"] and r["owner"] and r["shares"] * r["price"] >= 25_000:
            by[r["issuer"]].append(r)
    clusters, singles = [], []
    for iss, rs in by.items():
        rs.sort(key=lambda r: r["traded"])
        last_cluster = None
        for i, r in enumerate(rs):
            lo = r["traded"] - timedelta(days=7)
            win = [x for x in rs[:i + 1] if x["traded"] >= lo]
            first = {}
            for x in win:
                first.setdefault(x["owner"], x)
            if len(first) >= 3 and (last_cluster is None or (r["traded"] - last_cluster).days > 60):
                last_cluster = r["traded"]
                # everyone who joins within 7 days of the first buy counts toward the size
                start = min(x["traded"] for x in first.values())
                allw = {}
                for x in rs:
                    if start <= x["traded"] <= start + timedelta(days=7):
                        allw.setdefault(x["owner"], x)
                known = sorted(first.values(), key=lambda x: x["filed"])[:3]
                roles = {role(x) for x in allw.values()}
                clusters.append({
                    "ticker": r["ticker"], "filed": max(x["filed"] for x in known), "traded": r["traded"],
                    "n": len(allw), "price": r["price"],
                    "ceo_dir": any(x.startswith("CEO") for x in roles) and "Director" in roles,
                    "plan": any(x["plan"] for x in allw.values()),
                    "value": sum(x["shares"] * x["price"] for x in allw.values())})
        for i, r in enumerate(rs):
            others = {x["owner"] for x in rs if abs((x["traded"] - r["traded"]).days) <= 30} - {r["owner"]}
            if not others:
                singles.append({"ticker": r["ticker"], "filed": r["filed"], "traded": r["traded"], "n": 1,
                                "price": r["price"], "ceo_dir": False, "plan": r["plan"],
                                "value": r["shares"] * r["price"], "role": role(r)})
    return clusters, singles


def measure(e, p, spy):
    d = p["d"]
    k = bisect_right(d, e["traded"]) - 1
    f = 1.0
    for w, ratio in p["splits"]:
        if w > e["traded"]:
            f *= ratio
    if k < 0 or not (0.6 <= (e["price"] / f) / p["c"][k] <= 1.6):
        return False
    i = bisect_right(d, e["filed"])
    if i + 1 >= len(d):
        return False
    si = bisect_left(spy["d"], d[i])
    if si >= len(spy["d"]) or spy["d"][si] != d[i]:
        return False
    ya = bisect_right(d, d[i] - timedelta(days=365))
    e["drop"] = 1 - p["c"][i - 1] / max(p["c"][ya:i]) if i - ya > 20 else None
    dv = [p["c"][j] * p.get("v", [0] * len(d))[j] for j in range(max(0, i - 20), i)] if "v" in p else []
    e["small"] = None
    for lab_shift, key in ((0, "r"), (PLACEBO_SHIFT, "pl")):
        j, sj = i + lab_shift, si + lab_shift
        if j + 1 >= len(d) or sj >= len(spy["d"]):
            continue
        entry = p["o"][j]
        sentry = spy["o"][sj]
        for h in H:
            if j + h < len(d) and sj + h < len(spy["d"]):
                r = p["a"][j + h] / entry - 1 - COST
                e[f"{key}{h}"] = max(min(r, 3), -1) - (spy["a"][sj + h] / sentry - 1)
    return True


def row(label, evs):
    v20 = [e["r20"] for e in evs if "r20" in e]
    if len(v20) < 30:
        return None
    cells = [label, f"{len(v20):,}"]
    for h in H:
        v = [e[f"r{h}"] for e in evs if f"r{h}" in e]
        cells.append(f"{st.mean(v) * 100:+.2f}%" if len(v) >= 30 else "–")
    cells += [f"{st.median(v20) * 100:+.2f}%", f"{sum(x > 0 for x in v20) / len(v20) * 100:.0f}%"]
    pairs = [(e["r60"], e["pl60"]) for e in evs if "r60" in e and "pl60" in e]
    if len(pairs) >= 30:
        diff = [a - b for a, b in pairs]
        sd = st.pstdev(diff) or 1
        t = st.mean(diff) / (sd / len(diff) ** 0.5)
        cells.append(f"{st.mean(diff) * 100:+.2f}% (t {t:.1f})")
    else:
        cells.append("–")
    return "| " + " | ".join(cells) + " |"


def main():
    ua = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT required")
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    log("1/3 SEC data")
    rows = []
    for y in range(2006, date.today().year + 1):
        for q in range(1, 5):
            try:
                rows += load_quarter(s, y, q)[0]
            except Exception as ex:
                log(f"  {y}q{q}: {ex}")
    clusters, singles = detect(rows)
    del rows
    log(f"  clusters {len(clusters):,}, single buys {len(singles):,}")
    log("2/3 prices")
    tickers = sorted({e["ticker"] for e in clusters + singles} | {"SPY"})
    prices = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, p in zip(tickers, ex.map(fetch, tickers)):
            if p:
                prices[t] = p
    spy = prices["SPY"]
    log("3/3 measuring")
    C = [e for e in clusters if prices.get(e["ticker"]) and measure(e, prices[e["ticker"]], spy)]
    S = [e for e in singles if prices.get(e["ticker"]) and measure(e, prices[e["ticker"]], spy)]
    log(f"  measured clusters {len(C):,}, singles {len(S):,}")

    def groups(evs_c, evs_s):
        g = {"Single insider buys (baseline)": evs_s,
             "Single CEO/CFO buys": [e for e in evs_s if (e.get("role") or "").startswith("CEO")],
             "Cluster: 3+ insiders in 7 days": evs_c,
             "Cluster 4+ insiders": [e for e in evs_c if e["n"] >= 4],
             "Cluster with CEO/CFO + director": [e for e in evs_c if e["ceo_dir"]],
             "Cluster, no 10b5-1 plan": [e for e in evs_c if not e["plan"]],
             "Cluster, stock 30%+ below 1y high": [e for e in evs_c if (e.get("drop") or 0) >= .3],
             "Cluster, $1M+ total": [e for e in evs_c if e["value"] >= 1e6],
             "Gemini stack: CEO/CFO+director, no plan, 30%+ down": [
                 e for e in evs_c if e["ceo_dir"] and not e["plan"] and (e.get("drop") or 0) >= .3]}
        return g

    head = ["| Signal | Count | 5d | 10d | 20d | 60d | 126d | 20d median | 20d beat | 60d real − placebo |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    out = ["# Cluster buys, tested properly (2006–2026)",
           "Entry at the next OPEN after the 3rd insider's Form 4 is public, 0.25% cost subtracted. "
           "Columns = average return minus SPY over 5…126 trading days. "
           "Placebo = same stock 126 trading days later; a real edge must beat it (t above ~2 = unlikely luck)."]
    for title, flt in (("All years", lambda e: True), ("2006–2015", lambda e: e["filed"].year < 2016),
                       ("2016–2026", lambda e: e["filed"].year >= 2016)):
        out += [f"\n### {title}"] + head
        for k, v in groups([e for e in C if flt(e)], [e for e in S if flt(e)]).items():
            r = row(k, v)
            if r:
                out.append(r)
    out.append("\n_Survivorship bias: delisted/bankrupt companies often have no prices. "
               "Single = no other insider bought within 30 days._")
    md = "\n".join(out)
    with open(os.path.join(HERE, "clusters_results.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    log(md)
    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        try:
            requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                          "content": "👥 Cluster backtest finished."})},
                          files={"files[0]": ("clusters_results.md", md.encode(), "text/markdown")}, timeout=60)
        except Exception:
            pass


if __name__ == "__main__":
    main()
