"""
How much would the cluster strategy make per year? A realistic portfolio simulation.

Rules:
  - Signal: 3+ or 4+ insiders buying within 7 days (same detection as clusters.py),
    known when the 3rd insider's Form 4 is public.
  - Buy at the next day's OPEN, sell at the close after N trading days (5, 10 or 20).
  - Money: the portfolio has K equal slots (K = 10). Each new signal gets 1/K of current equity
    if a slot is free; if all slots are busy, the signal is skipped (like real life).
  - Idle money: either kept in cash (0%) or parked in SPY.
  - Costs: 0.25% per round trip.
Output per variant: yearly return, CAGR, max drawdown, trades per year, vs SPY.
Output: clusterport_results.md
"""

import json
import os
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests

from backtest import log
from backtest2 import load_quarter
from clusters import detect
from sameday import fetch

HERE = os.path.dirname(os.path.abspath(__file__))
COST = 0.0025
K = 10


def sanity(e, p):
    k = bisect_right(p["d"], e["traded"]) - 1
    f = 1.0
    for w, r in p["splits"]:
        if w > e["traded"]:
            f *= r
    return k >= 0 and 0.6 <= (e["price"] / f) / p["c"][k] <= 1.6


def simulate(events, prices, spy, hold, idle_spy):
    days = spy["d"]
    # trades: (entry_day_index_in_spy, exit_index, ticker, entry_price_idx_in_stock)
    starts = defaultdict(list)
    for e in events:
        p = prices.get(e["ticker"])
        if not p or not sanity(e, p):
            continue
        i = bisect_right(p["d"], e["filed"])
        if i + hold >= len(p["d"]):
            continue
        si = bisect_left(days, p["d"][i])
        if si >= len(days) or days[si] != p["d"][i]:
            continue
        starts[si].append((e["ticker"], i))
    cash, equity = 1.0, 1.0
    open_pos = []        # [ticker, stock_idx_now, shares_value_basis..] -> we track value directly
    curve, trades = [], 0
    positions = []       # dicts: ticker, j (stock index of last mark), value, exit_j
    for t in range(1, len(days)):
        # 1) mark positions to today's close
        for pos in positions:
            p = prices[pos["ticker"]]
            j = bisect_right(p["d"], days[t]) - 1
            if j > pos["j"]:
                pos["value"] *= p["a"][j] / p["a"][pos["j"]]
                pos["j"] = j
        # idle money in SPY grows with SPY
        if idle_spy:
            cash *= spy["a"][t] / spy["a"][t - 1]
        # 2) close finished positions at today's close
        keep = []
        for pos in positions:
            last = len(prices[pos["ticker"]]["d"]) - 1
            if pos["j"] >= pos["exit_j"] or (pos["j"] == last and days[t] > prices[pos["ticker"]]["d"][last]):
                cash += pos["value"] * (1 - COST / 2)
            else:
                keep.append(pos)
        positions = keep
        equity = cash + sum(p_["value"] for p_ in positions)
        # 3) open new positions at today's open (signals whose entry day is today)
        for tk, i in starts.get(t, []):
            if len(positions) >= K:
                continue
            p = prices[tk]
            size = min(equity / K, cash)
            if size <= 0:
                continue
            cash -= size
            # bought at the open, marked to the close of the same day
            val = size * (1 - COST / 2) * p["a"][i] / p["o"][i]
            positions.append({"ticker": tk, "j": i, "value": val, "exit_j": i + hold})
            trades += 1
        equity = cash + sum(p_["value"] for p_ in positions)
        curve.append((days[t], equity))
    return curve, trades


def stats(curve, spy, start):
    curve = [(d, v) for d, v in curve if d >= start]
    if len(curve) < 300:
        return None
    by_year = {}
    for d, v in curve:
        by_year.setdefault(d.year, [v, v])
        by_year[d.year][1] = v
    yearly, prev = {}, curve[0][1]
    for y in sorted(by_year):
        yearly[y] = by_year[y][1] / prev - 1
        prev = by_year[y][1]
    yrs = (curve[-1][0] - curve[0][0]).days / 365.25
    cagr = (curve[-1][1] / curve[0][1]) ** (1 / yrs) - 1
    peak, mdd = 0, 0
    for _, v in curve:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    return {"cagr": cagr, "mdd": mdd, "yearly": yearly, "years": yrs}


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
    clusters, _ = detect(rows)
    del rows
    log(f"  clusters {len(clusters):,}")
    log("2/3 prices")
    tickers = sorted({e["ticker"] for e in clusters} | {"SPY"})
    prices = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, p in zip(tickers, ex.map(fetch, tickers)):
            if p:
                prices[t] = p
    spy = prices["SPY"]
    log("3/3 simulating")
    spy_curve = [(d, a) for d, a in zip(spy["d"], spy["a"])]
    out = ["# What would the cluster strategy make per year? (portfolio simulation)",
           f"{K} equal slots, buy next open after the cluster is public, sell after N trading days, "
           "0.25% cost per trade, signals skipped when all slots are full. Starting 2006-04-01.", "",
           "| Strategy | Idle money | Per year (CAGR) | S&P 500 per year | Worst drop | Trades/yr | "
           "Per year since 2016 | S&P since 2016 |", "|---|---|---|---|---|---|---|---|"]
    start, recent = date(2006, 4, 1), date(2016, 1, 1)
    sp_all, sp_rec = stats(spy_curve, spy, start), stats(spy_curve, spy, recent)
    yearly_tables = []
    for n_min in (3, 4):
        evs = [e for e in clusters if e["n"] >= n_min]
        for hold in (5, 10, 20):
            for idle in (False, True):
                curve, trades = simulate(evs, prices, spy, hold, idle)
                a, r = stats(curve, spy, start), stats(curve, spy, recent)
                if not a:
                    continue
                name = f"{n_min}+ insiders, hold {hold} days"
                out.append(f"| {name} | {'SPY' if idle else 'cash'} | **{a['cagr'] * 100:+.1f}%** | "
                           f"{sp_all['cagr'] * 100:+.1f}% | -{a['mdd'] * 100:.0f}% | {trades / a['years']:.0f} | "
                           f"{r['cagr'] * 100:+.1f}% | {sp_rec['cagr'] * 100:+.1f}% |")
                if idle and hold in (5, 10):
                    yearly_tables.append((name + ", idle in SPY", a["yearly"]))
    out += ["", f"S&P 500 worst drop in the same period: -{sp_all['mdd'] * 100:.0f}%", "",
            "### Year by year"]
    years = sorted(sp_all["yearly"])
    out.append("| Year | S&P 500 | " + " | ".join(n for n, _ in yearly_tables) + " |")
    out.append("|---|---|" + "---|" * len(yearly_tables))
    for y in years:
        out.append(f"| {y} | {sp_all['yearly'][y] * 100:+.1f}% | " +
                   " | ".join(f"{t.get(y, 0) * 100:+.1f}%" for _, t in yearly_tables) + " |")
    out.append("\n_Survivorship bias: delisted/bankrupt companies often have no prices (they drop out of the test). "
               "No taxes. Past results don't guarantee future ones._")
    md = "\n".join(out)
    with open(os.path.join(HERE, "clusterport_results.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    log(md)
    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        try:
            requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                          "content": "📈 Cluster portfolio simulation finished."})},
                          files={"files[0]": ("clusterport_results.md", md.encode(), "text/markdown")}, timeout=60)
        except Exception:
            pass


if __name__ == "__main__":
    main()
