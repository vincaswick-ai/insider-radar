"""
Pre-registered test (rule written down BEFORE running, on 2026-11-03):

  Conviction cluster = 3+ insiders within 7 days (only insiders public by the signal day count)
                       AND (total bought $5M+  OR  insiders at least doubled their holdings / new position)

  Realistic costs per round trip, by how much the stock trades per day (20-day average before entry):
      > $10M/day  0.25%     $1-10M  0.5%     $250K-1M  1%     < $250K  2%

  Entry next open after the signal, hold 5 or 10 trading days. Portfolio: 10 equal slots (5 shown too),
  skip signals when full, idle money in cash (SPY variant shown too).
  Verdict: does it beat the S&P 500 after these costs, on 2016-2026 and on the full period?
Output: conviction_results.md
"""

import json
import os
import statistics as st
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests

import clusterport
import quant
from backtest import log

HERE = os.path.dirname(os.path.abspath(__file__))
H = (5, 10)


def real_cost(dv):
    return 0.0025 if dv > 1e7 else 0.005 if dv > 1e6 else 0.01 if dv > 2.5e5 else 0.02


def conviction(e):
    return e["value"] >= 5e6 or (e["stake"] or 0) >= 1.0


def simulate(events, prices, spy, hold, idle_spy, k):
    days = spy["d"]
    starts = defaultdict(list)
    for e in events:
        p = prices[e["ticker"]]
        i = e["i"]
        if i + hold >= len(p["d"]):
            continue
        si = bisect_left(days, p["d"][i])
        if si < len(days) and days[si] == p["d"][i]:
            starts[si].append((e["ticker"], i, e["cost"]))
    cash, positions, curve, trades = 1.0, [], [], 0
    for t in range(1, len(days)):
        for pos in positions:
            p = prices[pos["tk"]]
            j = bisect_right(p["d"], days[t]) - 1
            if j > pos["j"]:
                pos["v"] *= p["a"][j] / p["a"][pos["j"]]
                pos["j"] = j
        if idle_spy:
            cash *= spy["a"][t] / spy["a"][t - 1]
        keep = []
        for pos in positions:
            p = prices[pos["tk"]]
            last = len(p["d"]) - 1
            if pos["j"] >= pos["exit"] or (pos["j"] == last and days[t] > p["d"][last]):
                cash += pos["v"] * (1 - pos["c"] / 2)
            else:
                keep.append(pos)
        positions = keep
        equity = cash + sum(x["v"] for x in positions)
        for tk, i, c in starts.get(t, []):
            if len(positions) >= k:
                continue
            p = prices[tk]
            size = min(equity / k, cash)
            if size <= 0:
                continue
            cash -= size
            positions.append({"tk": tk, "j": i, "v": size * (1 - c / 2) * p["a"][i] / p["o"][i],
                              "exit": i + hold, "c": c})
            trades += 1
        curve.append((days[t], cash + sum(x["v"] for x in positions)))
    return curve, trades


def summ(evs, h, key="r"):
    v = [e[f"{key}{h}"] + 0.0025 - e["cost"] for e in evs if f"{key}{h}" in e]
    if len(v) < 20:
        return None
    sd = st.pstdev(v) or 1
    trim = sorted(v)[:max(1, int(len(v) * .99))]
    return {"n": len(v), "win": sum(x > 0 for x in v) / len(v), "mean": st.mean(v), "med": st.median(v),
            "trim": st.mean(trim), "t": st.mean(v) / (sd / len(v) ** .5)}


def pct(x):
    return f"{x * 100:+.2f}%"


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
                b, sl = quant.load_quarter(s, y, q)
                rows += b
                for _, iss, d in sl:
                    sells[iss].append(d)
            except Exception as ex:
                log(f"  {y}q{q}: {ex}")
    for k in sells:
        sells[k].sort()
    clusters = quant.detect(rows)
    del rows
    log("2/4 prices")
    tickers = sorted({e["ticker"] for e in clusters} | {"SPY"})
    prices = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, p in zip(tickers, ex.map(quant.fetch, tickers)):
            if p:
                prices[t] = p
    spy = prices["SPY"]
    log("3/4 measuring")
    ev = [e for e in clusters if prices.get(e["ticker"]) and quant.features(e, prices[e["ticker"]], spy, sells)]
    for e in ev:
        e["cost"] = real_cost(e["dv"])
    conv = [e for e in ev if conviction(e)]
    log(f"  clusters {len(ev):,}, conviction {len(conv):,}")

    out = ["# Pre-registered test: conviction clusters with realistic costs",
           "**Rule (written down before running):** 3+ insiders within 7 days AND (total bought $5M+ OR insiders "
           "at least doubled their holdings / new position).",
           "**Costs by liquidity:** >$10M/day 0.25%, $1-10M 0.5%, $250K-1M 1%, <$250K 2% per round trip.",
           "Returns minus S&P over the same days. Entry next open after the signal. Win = beat the S&P after costs.", "",
           "## 1. Trades",
           "| Group | Years | Hold | Trades | Win | Avg | Median | Avg w/o top 1% | t |",
           "|---|---|---|---|---|---|---|---|---|"]
    periods = (("2006-2015", lambda e: e["filed"].year < 2016), ("2016-2026", lambda e: e["filed"].year >= 2016),
               ("All", lambda e: True))
    for name, grp in (("All 3+ clusters", ev), ("**Conviction**", conv)):
        for pl, fl in periods:
            for h in H:
                sm = summ([e for e in grp if fl(e)], h)
                if sm:
                    out.append(f"| {name} | {pl} | {h}d | {sm['n']:,} | {sm['win'] * 100:.0f}% | {pct(sm['mean'])} | "
                               f"{pct(sm['med'])} | {pct(sm['trim'])} | {sm['t']:.1f} |")

    out += ["", "**Conviction by liquidity (all years, 5d)**", "| Traded per day | Trades | Cost | Win | Avg | Median |",
            "|---|---|---|---|---|---|"]
    for lab, lo, hi in (("> $10M", 1e7, 1e30), ("$1-10M", 1e6, 1e7), ("$250K-1M", 2.5e5, 1e6), ("< $250K", 0, 2.5e5)):
        sm = summ([e for e in conv if lo < e["dv"] <= hi or (lo == 0 and e["dv"] <= hi)], 5)
        if sm:
            out.append(f"| {lab} | {sm['n']:,} | {real_cost((lo + min(hi, 1e9)) / 2) * 100:.2g}% | "
                       f"{sm['win'] * 100:.0f}% | {pct(sm['mean'])} | {pct(sm['med'])} |")

    out += ["", "## 2. Placebo (same stocks 126 trading days later, same costs)",
            "| Group | Years | Hold | Trades | Real − placebo | t |", "|---|---|---|---|---|---|"]
    for name, grp in (("All 3+ clusters", ev), ("Conviction", conv)):
        for pl, fl in periods[1:]:
            for h in H:
                pairs = [(e[f"r{h}"], e[f"pl{h}"]) for e in grp if fl(e) and f"r{h}" in e and f"pl{h}" in e]
                if len(pairs) >= 30:
                    diff = [x - y for x, y in pairs]
                    sd = st.pstdev(diff) or 1
                    out.append(f"| {name} | {pl} | {h}d | {len(diff):,} | {pct(st.mean(diff))} | "
                               f"{st.mean(diff) / (sd / len(diff) ** .5):.1f} |")

    log("4/4 portfolios")
    spy_curve = list(zip(spy["d"], spy["a"]))
    sp_a = clusterport.stats(spy_curve, spy, date(2006, 4, 1))
    sp_r = clusterport.stats(spy_curve, spy, date(2016, 1, 1))
    out += ["", "## 3. Portfolio with realistic costs (start 2006-04)",
            "| Strategy | Slots | Hold | Idle money | Per year | Since 2016 | Worst drop | Trades/yr |",
            "|---|---|---|---|---|---|---|---|",
            f"| S&P 500 | – | – | – | {pct(sp_a['cagr'])} | {pct(sp_r['cagr'])} | -{sp_a['mdd'] * 100:.0f}% | – |"]
    yearly = []
    for name, grp in (("All 3+ clusters", ev), ("**Conviction**", conv)):
        for k in (10, 5):
            for h in H:
                for idle in (False, True):
                    curve, trades = simulate(grp, prices, spy, h, idle, k)
                    a_, r_ = clusterport.stats(curve, spy, date(2006, 4, 1)), clusterport.stats(curve, spy, date(2016, 1, 1))
                    if not a_ or not r_:
                        continue
                    out.append(f"| {name} | {k} | {h}d | {'SPY' if idle else 'cash'} | **{pct(a_['cagr'])}** | "
                               f"{pct(r_['cagr'])} | -{a_['mdd'] * 100:.0f}% | {trades / a_['years']:.0f} |")
                    if name.startswith("**") and k == 10 and h == 5:
                        yearly.append((f"Conviction, 10 slots, 5d, idle {'SPY' if idle else 'cash'}", a_["yearly"]))
    out += ["", "### Year by year (conviction)", "| Year | S&P 500 | " + " | ".join(n for n, _ in yearly) + " |",
            "|---|---|" + "---|" * len(yearly)]
    for y in sorted(sp_a["yearly"]):
        out.append(f"| {y} | {sp_a['yearly'][y] * 100:+.1f}% | " +
                   " | ".join(f"{t.get(y, 0) * 100:+.1f}%" for _, t in yearly) + " |")
    out.append("\n_Survivorship bias: delisted/bankrupt companies often have no prices. No taxes._")
    md = "\n".join(out)
    with open(os.path.join(HERE, "conviction_results.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    log(md)
    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        try:
            requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                          "content": "🎯 Conviction cluster test finished."})},
                          files={"files[0]": ("conviction_results.md", md.encode(), "text/markdown")}, timeout=60)
        except Exception:
            pass


if __name__ == "__main__":
    main()
