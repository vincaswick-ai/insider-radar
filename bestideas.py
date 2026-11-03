"""
13F "best ideas": do the BIGGEST new bets of concentrated fund managers beat the S&P 500?

Data: SEC Form 13F data sets (every 13F-HR filing since 2013, all managers).
We keep "concentrated" managers: 5-40 stock positions and $50M-$20B reported value
(stock pickers, not index-like giants). For each of their quarterly filings:
  best new idea   a stock that was NOT in last quarter's filing and is now 10%+ of the portfolio
  top holding     the single biggest position (new or not)
  consensus buy   a stock that 5+ concentrated managers bought new in the same quarter
Entry: the day after the 13F filing becomes public (that's when a copier can act).
Results: return minus SPY over 3, 6, 12 months (dividends included).
Persistence: managers whose best new ideas beat the market 2013-2019 -> their ideas 2020-2026.
Output: bestideas_results.md
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
from datetime import date, datetime

import requests

from backtest import fetch, log, pday

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
H = [("3M", 63), ("6M", 126), ("12M", 252)]
MIN_POS, MAX_POS = 5, 40
MIN_VAL, MAX_VAL = 50e6, 20e9


def tsv(z, name):
    for n in z.namelist():
        if n.upper().endswith(name):
            with z.open(n) as f:
                yield from csv.DictReader(io.TextIOWrapper(f, encoding="latin-1", newline=""),
                                          delimiter="\t", quoting=csv.QUOTE_NONE)
            return


def zip_links(s):
    html = s.get(PAGE, timeout=60).text
    links = sorted(set(re.findall(r'href="([^"]+_form13f\.zip)"', html)))
    return ["https://www.sec.gov" + l if l.startswith("/") else l for l in links]


def load_zip(s, url):
    """{(cik, period): (filed, name, {cusip: value})} for concentrated managers."""
    r = None
    for i in range(4):
        r = s.get(url, timeout=600)
        if r.status_code == 200:
            break
        time.sleep(5 + 10 * i)
    if r is None or r.status_code != 200:
        log(f"  {url}: HTTP {getattr(r, 'status_code', '?')}")
        return {}
    z = zipfile.ZipFile(io.BytesIO(r.content))
    subs = {}
    for x in tsv(z, "SUBMISSION.TSV"):
        if x.get("SUBMISSIONTYPE") == "13F-HR":
            subs[x["ACCESSION_NUMBER"]] = (x.get("CIK"), pday(x.get("PERIODOFREPORT")), pday(x.get("FILING_DATE")))
    amend = {x["ACCESSION_NUMBER"] for x in tsv(z, "COVERPAGE.TSV") if (x.get("ISAMENDMENT") or "").upper() == "Y"}
    names = {x["ACCESSION_NUMBER"]: x.get("FILINGMANAGER_NAME") for x in tsv(z, "COVERPAGE.TSV")}
    keep = set()
    for x in tsv(z, "SUMMARYPAGE.TSV"):
        acc = x["ACCESSION_NUMBER"]
        try:
            n = int(float(x.get("TABLEENTRYTOTAL") or 0))
        except ValueError:
            continue
        if acc in subs and acc not in amend and MIN_POS <= n <= MAX_POS * 2:
            keep.add(acc)
    hold = defaultdict(dict)
    for x in tsv(z, "INFOTABLE.TSV"):
        acc = x["ACCESSION_NUMBER"]
        if acc not in keep or (x.get("PUTCALL") or "").strip():
            continue
        try:
            v = float(x.get("VALUE") or 0)
        except ValueError:
            continue
        c = (x.get("CUSIP") or "").strip().upper()
        if c and v > 0:
            hold[acc][c] = hold[acc].get(c, 0) + v
    out = {}
    for acc, h in hold.items():
        cik, period, filed = subs[acc]
        if not period or not filed or not (MIN_POS <= len(h) <= MAX_POS):
            continue
        tot = sum(h.values())
        scale = 1000 if period < date(2023, 1, 1) else 1      # values were in $ thousands before 2023
        if not (MIN_VAL <= tot * scale <= MAX_VAL):
            continue
        key = (cik, period)
        if key not in out or filed < out[key][0]:
            out[key] = (filed, names.get(acc), {c: v / tot for c, v in h.items()})
    log(f"  {url.rsplit('/', 1)[-1]}: {len(out)} concentrated filings")
    return out


def figi(cusips):
    out, cusips = {}, sorted(cusips)
    for i in range(0, len(cusips), 10):
        b = cusips[i:i + 10]
        for _ in range(4):
            r = requests.post("https://api.openfigi.com/v3/mapping",
                              json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in b], timeout=30)
            if r.status_code == 429:
                time.sleep(20)
                continue
            break
        try:
            for c, res in zip(b, r.json()):
                eq = [d for d in (res.get("data") or []) if d.get("marketSector") == "Equity" and d.get("ticker")]
                if eq:
                    out[c] = eq[0]["ticker"].replace("/", ".").replace(" ", "")
        except Exception:
            pass
        time.sleep(2.5)
        if i % 2000 == 0:
            log(f"  figi {i}/{len(cusips)}")
    return out


def main():
    ua = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT required")
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    links = zip_links(s)
    log(f"1/4 {len(links)} 13F data set files")
    filings = {}
    for url in links:
        try:
            for k, v in load_zip(s, url).items():
                if k not in filings or v[0] < filings[k][0]:
                    filings[k] = v
        except Exception as ex:
            log(f"  {url}: {ex}")
    by_mgr = defaultdict(list)
    for (cik, period), (filed, name, w) in filings.items():
        by_mgr[cik].append((period, filed, name, w))
    signals = []          # (kind, cik, name, cusip, filed, weight)
    new_by_q = defaultdict(set)
    for cik, fl in by_mgr.items():
        fl.sort()
        for k, (period, filed, name, w) in enumerate(fl):
            top = max(w, key=w.get)
            signals.append(("Top holding", cik, name, top, filed, w[top]))
            if k == 0 or (period - fl[k - 1][0]).days > 100:      # need the previous quarter to know what's new
                continue
            prev = fl[k - 1][3]
            for c, x in w.items():
                if c not in prev:
                    new_by_q[period].add((c, cik, filed))
                    if x >= 0.10:
                        signals.append(("Best new idea (new, 10%+ of portfolio)", cik, name, c, filed, x))
                    elif x >= 0.05:
                        signals.append(("New position 5-10%", cik, name, c, filed, x))
    for period, items in new_by_q.items():
        cnt = defaultdict(list)
        for c, cik, filed in items:
            cnt[c].append(filed)
        for c, fds in cnt.items():
            if len(fds) >= 5:
                signals.append(("Consensus buy (5+ managers new)", None, None, c, sorted(fds)[4], None))
    log(f"2/4 managers {len(by_mgr):,}, signals {len(signals):,}")
    tick = figi({x[3] for x in signals})
    log(f"3/4 tickers mapped {len(tick):,}")
    tickers = sorted(set(tick.values()) | {"SPY"})
    prices = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, p in zip(tickers, ex.map(fetch, tickers)):
            if p:
                prices[t] = p
    spy = prices["SPY"]
    log("4/4 measuring")
    res = []
    for kind, cik, name, c, filed, wt in signals:
        t = tick.get(c)
        p = prices.get(t)
        if not p:
            continue
        i = bisect_right(p["d"], filed)
        si = bisect_right(spy["d"], filed)
        r = {"kind": kind, "cik": cik, "name": name, "ticker": t, "filed": filed}
        for lab, h in H:
            if i + h < len(p["d"]) and si + h < len(spy["d"]):
                r[lab] = max(min(p["a"][i + h] / p["a"][i] - 1, 3), -1) - (spy["a"][si + h] / spy["a"][si] - 1)
        if "3M" in r:
            res.append(r)

    def table(title, groups):
        lines = [f"\n### {title}", "| Signal | Count | 3M vs S&P | 6M vs S&P | 12M vs S&P | 12M median | 12M beat |",
                 "|---|---|---|---|---|---|---|"]
        for g, rs in groups.items():
            v12 = [r["12M"] for r in rs if "12M" in r]
            if len(v12) < 30:
                continue
            cells = [g, f"{len(rs):,}"]
            for lab, _ in H:
                v = [r[lab] for r in rs if lab in r]
                cells.append(f"{st.mean(v) * 100:+.1f}%")
            cells += [f"{st.median(v12) * 100:+.1f}%", f"{sum(x > 0 for x in v12) / len(v12) * 100:.0f}%"]
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    kinds = sorted({r["kind"] for r in res})
    out = ["# 13F best ideas: copying concentrated fund managers (2013–2026)",
           f"Concentrated managers ({MIN_POS}-{MAX_POS} stocks, $50M-$20B): **{len(by_mgr):,}**. "
           "Entry = the day after the 13F is filed. Return minus SPY, dividends included, single results capped at +300%."]
    out.append(table("All years", {k: [r for r in res if r["kind"] == k] for k in kinds}))
    out.append(table("2020-2026 only", {k: [r for r in res if r["kind"] == k and r["filed"].year >= 2020] for k in kinds}))
    # persistence: pick managers by their 2013-2019 best ideas, follow them 2020-2026
    best = [r for r in res if r["kind"].startswith("Best new") and "12M" in r]
    before, after = defaultdict(list), defaultdict(list)
    for r in best:
        (before if r["filed"].year < 2020 else after)[r["cik"]].append(r)
    groups = {"All managers 2020-2026 (baseline)": [r for rs in after.values() for r in rs]}
    for n_min, thr in ((3, 0.0), (3, 0.10), (5, 0.0), (5, 0.10)):
        chosen = {c for c, rs in before.items() if len(rs) >= n_min and st.mean(x["12M"] for x in rs) > thr}
        groups[f"Managers with {n_min}+ ideas beating S&P by {thr:.0%}+ before 2020"] = \
            [r for c in chosen for r in after.get(c, [])]
    out.append(table("Persistence: pick good managers using 2013-2019, follow them 2020-2026", groups))
    # top managers list (all years, 5+ best new ideas)
    allm = defaultdict(list)
    for r in best:
        allm[(r["cik"], r["name"])].append(r["12M"])
    top = sorted(((st.mean(v), sum(x > 0 for x in v) / len(v), len(v), k[1]) for k, v in allm.items() if len(v) >= 5),
                 reverse=True)[:15]
    out += ["\n### Managers whose best new ideas did best (5+ ideas, 12M vs S&P)",
            "| Manager | Ideas | Avg 12M vs S&P | Beat S&P |", "|---|---|---|---|"]
    out += [f"| {n} | {c} | {a * 100:+.1f}% | {w * 100:.0f}% |" for a, w, c, n in top]
    out.append("\n_Looking back, some managers will always look great by luck. The persistence table is the honest test._")
    md = "\n".join(out)
    with open(os.path.join(HERE, "bestideas_results.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    log(md)
    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        try:
            requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                          "content": "🏦 13F best ideas test finished."})},
                          files={"files[0]": ("bestideas_results.md", md.encode(), "text/markdown")}, timeout=60)
        except Exception:
            pass


if __name__ == "__main__":
    main()
