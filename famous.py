"""
Famous investors backtest: would copying their 13F filings have beaten the S&P 500?

Big funds (> $100M) must report their US stock holdings every quarter on Form 13F,
up to 45 days after the quarter ends. So a copier always trades LATE. We test exactly that:

  Clone portfolio  On each 13F filing date, buy the fund's reported stock portfolio (same weights),
                   hold it until the next filing, then switch. Compare with SPY on the same days.
  New buys         Every stock that appears for the first time in a filing: return over the next
                   6 and 12 months (from the filing date) minus SPY.

Data: SEC EDGAR 13F-HR filings (XML info tables since 2013), OpenFIGI (CUSIP -> ticker), Yahoo prices.
Env: SEC_USER_AGENT, WEBHOOK_PROOF (optional). Output: famous_results.md
"""

import json
import os
import re
import statistics as st
import sys
import time
import xml.etree.ElementTree as ET
from bisect import bisect_right
from datetime import date, datetime, timedelta

import requests

from backtest import fetch

FUNDS = [  # (CIK, who)
    (1067983, "Warren Buffett · Berkshire Hathaway"),
    (1649339, "Michael Burry · Scion Asset Management"),
    (1336528, "Bill Ackman · Pershing Square"),
    (1656456, "David Tepper · Appaloosa"),
    (1061768, "Seth Klarman · Baupost Group"),
    (1079114, "David Einhorn · Greenlight Capital"),
    (1536411, "Stanley Druckenmiller · Duquesne Family Office"),
    (1709323, "Li Lu · Himalaya Capital"),
    (1040273, "Dan Loeb · Third Point"),
    (1103804, "Viking Global (Andreas Halvorsen)"),
]
SUBS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBS_PAGE = "https://data.sec.gov/submissions/{name}"
HERE = os.path.dirname(os.path.abspath(__file__))


def log(m):
    print(m, flush=True)


def get(session, url, **kw):
    for i in range(4):
        time.sleep(0.15)
        r = session.get(url, timeout=60, **kw)
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 + 4 * i)
            continue
        return r
    return r


def filings_13f(session, cik):
    data = get(session, SUBS.format(cik=cik)).json()
    name = data.get("name")
    blocks = [data["filings"]["recent"]]
    for f in data["filings"].get("files", []):
        blocks.append(get(session, SUBS_PAGE.format(name=f["name"])).json())
    out = []
    for b in blocks:
        for form, acc, filed, period in zip(b["form"], b["accessionNumber"], b["filingDate"],
                                            b.get("reportDate", [""] * len(b["form"]))):
            if form == "13F-HR":
                out.append((date.fromisoformat(filed), acc, period))
    out.sort()
    # one filing per report period (keep the first one)
    seen, uniq = set(), []
    for f in out:
        if f[2] not in seen:
            seen.add(f[2])
            uniq.append(f)
    return name, uniq


def holdings(session, cik, acc):
    """{cusip: (name, value)} of common-stock holdings in one 13F (options skipped)."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
    idx = get(session, base + "index.json").json()
    for item in idx.get("directory", {}).get("item", []):
        n = item["name"]
        if not n.lower().endswith(".xml") or n.lower() == "primary_doc.xml":
            continue
        text = get(session, base + n).text
        if "infoTable" not in text:
            continue
        text = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", text)
        text = re.sub(r"<(/?)\w+:", r"<\1", text)
        root = ET.fromstring(text.encode())
        out = {}
        for it in root.iter("infoTable"):
            if (it.findtext("putCall") or "").strip():
                continue
            cusip = (it.findtext("cusip") or "").strip().upper()
            try:
                val = float(it.findtext("value") or 0)
            except ValueError:
                continue
            if cusip and val > 0:
                nm, v = out.get(cusip, (it.findtext("nameOfIssuer"), 0))
                out[cusip] = (nm, v + val)
        return out
    return {}


def cusip_tickers(cusips):
    """CUSIP -> ticker via OpenFIGI (free: 25 requests/min, 10 ids each)."""
    out, cusips = {}, sorted(cusips)
    for i in range(0, len(cusips), 10):
        batch = cusips[i:i + 10]
        body = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch]
        for k in range(4):
            r = requests.post("https://api.openfigi.com/v3/mapping", json=body, timeout=30)
            if r.status_code == 429:
                time.sleep(20)
                continue
            break
        try:
            for c, res in zip(batch, r.json()):
                data = res.get("data") or []
                eq = [d for d in data if d.get("marketSector") == "Equity" and d.get("ticker")]
                if eq:
                    out[c] = eq[0]["ticker"].replace("/", ".").replace(" ", "")
        except Exception as e:
            log(f"  openfigi failed: {e}")
        time.sleep(2.6)
    return out


def close_after(p, day):
    """Index of the first trading day after `day`."""
    return bisect_right(p["d"], day)


def ret_between(p, d0, d1):
    i, j = close_after(p, d0), min(close_after(p, d1), len(p["d"]) - 1)
    if i >= len(p["d"]) or j <= i:
        return None
    return p["a"][j] / p["a"][i] - 1


def main():
    ua = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT required")
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    funds = []
    all_cusips = set()
    for cik, who in FUNDS:
        try:
            name, fl = filings_13f(s, cik)
        except Exception as e:
            log(f"{who}: failed {e}")
            continue
        fl = [f for f in fl if f[0] >= date(2013, 7, 1)]          # XML info tables start mid-2013
        hs = []
        for filed, acc, period in fl:
            try:
                h = holdings(s, cik, acc)
            except Exception as e:
                log(f"  {who} {acc}: {e}")
                h = {}
            if h:
                hs.append((filed, h))
        log(f"{who}: SEC name '{name}', {len(hs)} filings")
        funds.append((who, hs))
        for _, h in hs:
            all_cusips |= set(h)
    log(f"Mapping {len(all_cusips)} CUSIPs to tickers")
    tick = cusip_tickers(all_cusips)
    log(f"  mapped {len(tick)}")
    prices = {}
    for t in sorted(set(tick.values()) | {"SPY"}):
        p = fetch(t)
        if p:
            prices[t] = p
    spy = prices["SPY"]
    today = date.today()

    out = ["# Famous investors: copying their 13F filings (since 2013)",
           "We buy on the day the 13F becomes public (up to 45 days after the quarter), "
           "so this is what a real copier gets. Returns include dividends.", "",
           "| Investor | Years | Clone portfolio / yr | S&P 500 / yr | Beat S&P? | New buys | New buys 12M vs S&P (avg / median / win) |",
           "|---|---|---|---|---|---|---|"]
    detail = []
    for who, hs in funds:
        if len(hs) < 4:
            out.append(f"| {who} | – | not enough data | | | | |")
            continue
        val, sval, covered = 1.0, 1.0, []
        for k, (filed, h) in enumerate(hs):
            end = hs[k + 1][0] if k + 1 < len(hs) else today
            tot, acc = 0.0, 0.0
            for c, (_, v) in h.items():
                t = tick.get(c)
                if t in prices:
                    r = ret_between(prices[t], filed, end)
                    if r is not None:
                        acc += v * r
                        tot += v
            allv = sum(v for _, v in h.values())
            covered.append(tot / allv if allv else 0)
            if tot:
                val *= 1 + acc / tot
                sr = ret_between(spy, filed, end)
                sval *= 1 + (sr or 0)
        years = (today - hs[0][0]).days / 365.25
        cagr, scagr = val ** (1 / years) - 1, sval ** (1 / years) - 1
        # new positions
        news = []
        for k in range(1, len(hs)):
            filed, h = hs[k]
            for c in set(h) - set(hs[k - 1][1]):
                t = tick.get(c)
                if t in prices:
                    p = prices[t]
                    i = close_after(p, filed)
                    si = close_after(spy, filed)
                    if i + 252 < len(p["d"]) and si + 252 < len(spy["d"]):
                        r = p["a"][i + 252] / p["a"][i] - 1 - (spy["a"][si + 252] / spy["a"][si] - 1)
                        news.append((r, t, filed))
        if news:
            rs = [r for r, _, _ in news]
            nb = (f"{len(news)} | {st.mean(rs) * 100:+.1f}% / {st.median(rs) * 100:+.1f}% / "
                  f"{sum(r > 0 for r in rs) / len(rs) * 100:.0f}%")
            best = sorted(news, reverse=True)[:3]
            detail.append(f"- **{who}** best new buys: " + ", ".join(f"${t} {r * 100:+.0f}% ({d:%Y})" for r, t, d in best))
        else:
            nb = "0 | –"
        out.append(f"| {who} | {years:.1f} | **{cagr * 100:+.1f}%** | {scagr * 100:+.1f}% | "
                   f"{'✅' if cagr > scagr else '❌'} {(cagr - scagr) * 100:+.1f}% | {nb} |")
        log(f"{who}: coverage {st.mean(covered):.0%}")
    out += ["", *detail, "",
            "_Clone = buy the reported portfolio on each filing date, hold until the next one. "
            "Only stocks we could match to a ticker and price are counted._"]
    md = "\n".join(out)
    with open(os.path.join(HERE, "famous_results.md"), "w", encoding="utf-8") as f:
        f.write(md)
    log(md)
    hook = os.environ.get("WEBHOOK_PROOF", "").strip()
    if hook:
        requests.post(hook, data={"payload_json": json.dumps({"username": "Insider Radar",
                      "content": "📊 Famous investors backtest finished."})},
                      files={"files[0]": ("famous_results.md", md.encode(), "text/markdown")}, timeout=60)


if __name__ == "__main__":
    main()
