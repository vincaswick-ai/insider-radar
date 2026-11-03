"""
Backtest round 3: sell on a profit target instead of after a fixed time.

Buy: the day after the Form 4 filing (close), same insider buys as before ($25K+ open-market).
Sell rules tested (daily closes, dividends included):
  take profit +10/20/50/100%, if never reached sell after 12 or 24 months
  take profit + stop loss pairs (e.g. +20% / -10%)
  trailing stop: sell when the price falls 20% / 30% from its highest close since buying
For every rule: how often the target was hit, average and median trade, average days held,
the S&P 500 over exactly the same days, and a rough per-year figure.

Groups: all buys, 2016-2026 only, CEO/CFO 2016-2026, buys during market crashes (S&P 20%+ down).
Output: backtest3_results.md
"""

import json
import os
import statistics as st
import sys
from bisect import bisect_right
from datetime import date

import numpy as np
import requests

from backtest import log, role
from backtest2 import buy_events, load_prices, load_quarter, measure

HERE = os.path.dirname(os.path.abspath(__file__))

RULES = []
for months, days in ((12, 252), (24, 504)):
    for tp in (0.10, 0.20, 0.50, 1.00):
        RULES.append((f"Take profit +{tp:.0%}, else sell after {months}M", tp, None, None, days))
for tp, sl in ((0.10, 0.10), (0.20, 0.10), (0.20, 0.20), (0.50, 0.20), (0.50, 0.30), (1.00, 0.30)):
    RULES.append((f"Take profit +{tp:.0%} / stop loss -{sl:.0%} (max 24M)", tp, sl, None, 504))
for tr in (0.20, 0.30):
    RULES.append((f"Trailing stop -{tr:.0%} from the high (max 24M)", None, None, tr, 504))
RULES.append(("Benchmark: hold 6M fixed", None, None, None, 126))
RULES.append(("Benchmark: hold 12M fixed", None, None, None, 252))


def simulate(path, spy_path, tp, sl, trail, maxd):
    """path/spy_path: price ratios vs entry for days 1..N. Returns (ret, spy_ret, days, how)."""
    if min(len(path), len(spy_path)) < maxd:   # only trades whose full window is known (no recent-trade bias)
        return None
    n = maxd
    p = path[:n]
    hits = []
    if tp is not None:
        w = np.nonzero(p >= 1 + tp)[0]
        if len(w):
            hits.append((w[0], "target"))
    if sl is not None:
        w = np.nonzero(p <= 1 - sl)[0]
        if len(w):
            hits.append((w[0], "stop"))
    if trail is not None:
        peak = np.maximum.accumulate(np.maximum(p, 1.0))
        w = np.nonzero(p <= peak * (1 - trail))[0]
        if len(w):
            hits.append((w[0], "trail"))
    if hits:
        k, how = min(hits)
    else:
        k, how = n - 1, "time"
    return p[k] - 1, spy_path[k] - 1, k + 1, how


def main():
    start = int(os.environ.get("START_YEAR") or 2006)
    end = int(os.environ.get("END_YEAR") or date.today().year)
    ua = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT required")
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    log("1/3 SEC data")
    buys = []
    for y in range(start, end + 1):
        for q in range(1, 5):
            try:
                buys += load_quarter(s, y, q)[0]
            except Exception as ex:
                log(f"  {y}q{q} failed: {ex}")
    events = buy_events(buys)
    del buys
    log("2/3 prices")
    prices = load_prices({e["ticker"] for e in events} | {"SPY"})
    spy = prices["SPY"]
    spy_a = np.array(spy["a"])
    log("3/3 simulating")
    trades = []
    for e in events:
        p = prices.get(e["ticker"])
        if not p:
            continue
        measure(e, p, spy, spy)                 # price sanity check + market drawdown
        if e.get("1M") is None:
            continue
        i = bisect_right(p["d"], e["filed"])
        si = bisect_right(spy["d"], p["d"][i]) - 1
        a = np.array(p["a"][i:i + 505])
        path = a[1:] / a[0]
        sp = spy_a[si + 1:si + 505] / spy_a[si]
        trades.append((e, path, sp))
    log(f"  trades: {len(trades):,}")

    groups = {
        "All buys (2006-2026)": lambda e: True,
        "All buys, 2016-2026 only": lambda e: e["filed"].year >= 2016,
        "CEO/CFO, 2016-2026 only": lambda e: e["filed"].year >= 2016 and role(e).startswith("CEO"),
        "During market crashes (S&P 20%+ down)": lambda e: (e.get("mkt_dd") or 0) >= .2,
    }
    out = ["# Insider Radar backtest round 3: sell on a profit target (2006–2026)",
           "Buy the day after the Form 4 filing. Daily closing prices, dividends included. "
           "'vs S&P' = the trade minus the S&P 500 over exactly the same days. "
           "'Per year' = average trade scaled to a year by the average days held (rough).", ""]
    for gname, gf in groups.items():
        sub = [t for t in trades if gf(t[0])]
        out += [f"\n### {gname} ({len(sub):,} buys)",
                "| Sell rule | Trades | Hit target | Avg trade | Median | Avg vs S&P | Beat S&P | Avg days | Per year |",
                "|---|---|---|---|---|---|---|---|---|"]
        for name, tp, sl, trail, maxd in RULES:
            res = [r for r in (simulate(pa, sp, tp, sl, trail, maxd) for _, pa, sp in sub) if r]
            if len(res) < 30:
                continue
            rets = [min(r[0], 3.0) for r in res]
            ex = [min(r[0], 3.0) - r[1] for r in res]
            days = [r[2] for r in res]
            hit = sum(1 for r in res if r[3] == "target") / len(res)
            avg, d = st.mean(rets), st.mean(days)
            per_year = (1 + avg) ** (252 / d) - 1 if avg > -1 and d > 0 else float("nan")
            out.append(f"| {name} | {len(res):,} | {hit * 100:.0f}% | {avg * 100:+.1f}% | "
                       f"{st.median(rets) * 100:+.1f}% | {st.mean(ex) * 100:+.1f}% | "
                       f"{sum(1 for x in ex if x > 0) / len(ex) * 100:.0f}% | {d:.0f} | {per_year * 100:+.0f}% |")
    out.append("\n_Uses daily closes (a stock that touched the target intraday but closed below it does not "
               "count). No trading costs. Survivorship bias: bankrupt or acquired companies often have no prices._")
    md = "\n".join(out)
    with open(os.path.join(HERE, "backtest3_results.md"), "w", encoding="utf-8") as f:
        f.write(md)
    log(md)
    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        try:
            requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                          "content": "📊 Backtest round 3 (profit targets) finished."})},
                          files={"files[0]": ("backtest3_results.md", md.encode(), "text/markdown")}, timeout=60)
        except Exception as ex:
            log(f"Discord post failed: {ex}")


if __name__ == "__main__":
    main()
