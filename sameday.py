"""
Speed test: does buying FAST after a Form 4 (same day) beat buying the next day?

For every open-market insider buy ($25K+) we look up the exact time SEC accepted the
Form 4 (EDGAR acceptanceDateTime) and compare three entries:
  same-day close   only for filings accepted during market hours (9:30-15:30 New York time)
  next-day open    the first price an after-hours filing can get
  next-day close   what all our earlier tests used
Results: return minus SPY after 1, 3, 6, 12 months (dividends included).
Output: sameday_results.md
"""

import io
import json
import os
import re
import statistics as st
import sys
import time
import zipfile
from bisect import bisect_left, bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from backtest import PRICE_URL, UA_BROWSER, ZIP_URL, fnum, log, pday, read_tsv, role

HERE = os.path.dirname(os.path.abspath(__file__))
NY = ZoneInfo("America/New_York")
H = [("1M", 21), ("3M", 63), ("6M", 126), ("12M", 252)]


def load_quarter(s, y, q):
    r = None
    for i in range(4):
        r = s.get(ZIP_URL.format(y=y, q=q), timeout=120)
        if r.status_code in (200, 404):
            break
        time.sleep(3 + 5 * i)
    if r is None or r.status_code != 200:
        return {}
    z = zipfile.ZipFile(io.BytesIO(r.content))
    subs = {x["ACCESSION_NUMBER"]: x for x in read_tsv(z, "SUBMISSION.TSV")
            if (x.get("DOCUMENT_TYPE") or "") == "4"}
    owners = {}
    for o in read_tsv(z, "REPORTINGOWNER.TSV"):
        owners.setdefault(o["ACCESSION_NUMBER"], o)
    ev = {}
    for t in read_tsv(z, "NONDERIV_TRANS.TSV"):
        acc = t["ACCESSION_NUMBER"]
        if (t.get("TRANS_CODE") or "").strip() != "P" or acc not in subs or acc not in owners:
            continue
        sh, pr = fnum(t.get("TRANS_SHARES")), fnum(t.get("TRANS_PRICEPERSHARE"))
        if not sh or not pr or pr <= 0:
            continue
        sub, o = subs[acc], owners[acc]
        tk = (sub.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{1,5}([.-][A-Z])?", tk):
            continue
        e = ev.setdefault(acc, {"acc": acc, "filed": pday(sub.get("FILING_DATE")), "ticker": tk,
                                "issuer": sub.get("ISSUERCIK"), "title": o.get("RPTOWNER_TITLE") or "",
                                "rel": o.get("RPTOWNER_RELATIONSHIP") or "", "value": 0.0, "shares": 0.0,
                                "traded": pday(t.get("TRANS_DATE"))})
        e["value"] += sh * pr
        e["shares"] += sh
    log(f"  {y}q{q}: {len(ev)} buy filings")
    return ev


def acceptance_times(s, issuers):
    """accession -> acceptance datetime string, from each issuer's EDGAR submissions history."""
    out = {}
    for k, cik in enumerate(sorted(issuers)):
        try:
            time.sleep(0.11)
            d = s.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", timeout=30).json()
            blocks = [d["filings"]["recent"]]
            for f in d["filings"].get("files", []):
                time.sleep(0.11)
                blocks.append(s.get(f"https://data.sec.gov/submissions/{f['name']}", timeout=30).json())
            for b in blocks:
                for form, acc, t in zip(b["form"], b["accessionNumber"], b.get("acceptanceDateTime", [])):
                    if form == "4" and t:
                        out[acc] = t
        except Exception:
            continue
        if k % 1000 == 0:
            log(f"  acceptance times: {k}/{len(issuers)} issuers, {len(out):,} filings")
    return out


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
            d, o, c, a = [], [], [], []
            for k, x in enumerate(res.get("timestamp") or []):
                cl, op, ad = q["close"][k], q["open"][k], adj[k]
                if cl and op and ad:
                    d.append(datetime.fromtimestamp(x, timezone.utc).date())
                    c.append(cl)
                    a.append(ad)
                    o.append(op * ad / cl)          # open on the same adjusted scale
            splits = [(datetime.fromtimestamp(x["date"], timezone.utc).date(), x["numerator"] / x["denominator"])
                      for x in ((res.get("events") or {}).get("splits") or {}).values()
                      if x.get("numerator") and x.get("denominator")]
            return {"d": d, "o": o, "c": c, "a": a, "splits": splits} if len(d) > 20 else None
        except Exception:
            time.sleep(2)
    return None


def excess(p, spy, i, price_i, spy_price_i, si):
    out = {}
    for lab, h in H:
        if i + h < len(p["d"]) and si + h < len(spy["d"]):
            out[lab] = (p["a"][i + h] / price_i - 1) - (spy["a"][si + h] / spy_price_i - 1)
    return out


def main():
    ua = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT required")
    start = int(os.environ.get("START_YEAR") or 2006)
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    log("1/4 Form 4 data")
    ev = {}
    for y in range(start, date.today().year + 1):
        for q in range(1, 5):
            try:
                ev.update(load_quarter(s, y, q))
            except Exception as ex:
                log(f"  {y}q{q}: {ex}")
    events = [e for e in ev.values() if e["value"] >= 25_000 and e["filed"] and e["traded"]]
    log(f"  buy filings $25K+: {len(events):,}")
    log("2/4 acceptance times")
    acc_t = acceptance_times(s, {e["issuer"] for e in events})
    hours = defaultdict(int)
    for e in events:
        t = acc_t.get(e["acc"])
        if t:
            hours[int(t[11:13])] += 1
    tot = sum(hours.values()) or 1
    utc = sum(hours[h] for h in range(0, 6)) / tot > 0.03     # EDGAR runs 6am-10pm New York time
    log(f"  hour histogram {dict(sorted(hours.items()))} -> times look like {'UTC' if utc else 'New York'}")
    for e in events:
        t = acc_t.get(e["acc"])
        if not t:
            continue
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        dt = dt.astimezone(NY) if utc else dt.replace(tzinfo=NY)
        e["acc_date"], e["acc_min"] = dt.date(), dt.hour * 60 + dt.minute
    log("3/4 prices")
    tickers = sorted({e["ticker"] for e in events if e.get("acc_date")} | {"SPY"})
    prices = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, p in zip(tickers, ex.map(fetch, tickers)):
            if p:
                prices[t] = p
    spy = prices["SPY"]
    log("4/4 measuring")
    rows = []
    for e in events:
        p = prices.get(e["ticker"])
        if not p or not e.get("acc_date"):
            continue
        d = p["d"]
        k = bisect_right(d, e["traded"]) - 1
        f = 1.0
        for w, r in p["splits"]:
            if w > e["traded"]:
                f *= r
        if k < 0 or not (0.6 <= (e["value"] / e["shares"] / f) / p["c"][k] <= 1.6):
            continue
        day = e["acc_date"]
        m = e["acc_min"]
        kind = ("market hours" if 570 <= m <= 930 else "after close" if m >= 960 else "before open")
        j = bisect_left(d, day)               # index of the acceptance day (or next trading day)
        if j >= len(d) or j < 1:
            continue
        sj = bisect_left(spy["d"], d[j])
        if sj >= len(spy["d"]) or spy["d"][sj] != d[j]:
            continue
        row = {"kind": kind, "year": day.year, "ceo": role(e).startswith("CEO"),
               "move": (p["a"][j] / p["a"][j - 1] - 1) - (spy["a"][sj] / spy["a"][sj - 1] - 1)}
        if kind == "market hours" and d[j] == day:
            row["same_close"] = excess(p, spy, j, p["a"][j], spy["a"][sj], sj)
        n = bisect_right(d, day)                      # first trading day after the acceptance day
        if kind == "before open" and d[j] == day:
            n = j                                     # a pre-market filing can be bought at today's open
        if n < len(d):
            sn = bisect_left(spy["d"], d[n])
            if sn < len(spy["d"]) and spy["d"][sn] == d[n]:
                row["next_open"] = excess(p, spy, n, p["o"][n], spy["o"][sn], sn)
                row["next_close"] = excess(p, spy, n, p["a"][n], spy["a"][sn], sn)
        rows.append(row)
    log(f"  measured {len(rows):,}")

    def line(label, rs, key):
        vals = {lab: [max(min(r[key][lab], 3), -1) for r in rs if key in r and lab in r[key]] for lab, _ in H}
        if len(vals["6M"]) < 30:
            return None
        cells = [label, f"{len(vals['6M']):,}"] + [f"{st.mean(v) * 100:+.1f}%" if len(v) >= 30 else "–"
                                                   for v in vals.values()]
        cells += [f"{st.median(vals['6M']) * 100:+.1f}%", f"{sum(x > 0 for x in vals['6M']) / len(vals['6M']) * 100:.0f}%"]
        return "| " + " | ".join(cells) + " |"

    out = ["# Speed test: buying the same day vs the next day (insider buys $25K+, since 2006)",
           "Return minus SPY over the same days, dividends included. Market hours = Form 4 accepted "
           "9:30-15:30 New York time.", ""]
    for title, flt in (("All years", lambda r: True), ("2016-2026 only", lambda r: r["year"] >= 2016),
                       ("CEO/CFO, all years", lambda r: r["ceo"])):
        rs = [r for r in rows if flt(r)]
        out += [f"\n### {title}", "| Entry | Buys | 1M | 3M | 6M | 12M | 6M median | 6M beat |",
                "|---|---|---|---|---|---|---|---|"]
        for kind in ("market hours", "after close", "before open"):
            sub = [r for r in rs if r["kind"] == kind]
            if not sub:
                continue
            mv = [max(min(r["move"], 3), -1) for r in sub]
            out.append(f"| **Filed {kind}** ({len(sub):,} filings, stock moves {st.mean(mv) * 100:+.2f}% "
                       f"that day vs SPY) | | | | | | | |")
            for key, label in (("same_close", "→ buy same-day close"), ("next_open", "→ buy next open"),
                               ("next_close", "→ buy next-day close")):
                ln = line(label, sub, key)
                if ln:
                    out.append(ln)
    out.append("\n_No trading costs. Survivorship bias: bankrupt/acquired companies often have no prices._")
    md = "\n".join(out)
    with open(os.path.join(HERE, "sameday_results.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    log(md)
    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        try:
            requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                          "content": "⚡ Speed test finished."})},
                          files={"files[0]": ("sameday_results.md", md.encode(), "text/markdown")}, timeout=60)
        except Exception:
            pass


if __name__ == "__main__":
    main()
