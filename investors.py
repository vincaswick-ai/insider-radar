"""
Top investors (#top-investors): a card every time a famous fund files a new 13F.

Each run checks the latest 13F-HR of every fund in FUNDS. When there is a filing we haven't posted yet,
it compares the fund's US stock holdings with the previous quarter and posts:
new positions, big adds, big cuts, full exits and the top holdings.

First run: only remembers the latest filings, posts nothing (no old news).
Env: SEC_USER_AGENT, WEBHOOK_INVESTORS, MODE=test (post the latest filing of each fund to WEBHOOK_PROOF, no state change)
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date

import requests

import cards
from famous import cusip_tickers

FUNDS = [  # (CIK, investor, fund)
    (1067983, "Warren Buffett", "Berkshire Hathaway"),
    (1649339, "Michael Burry", "Scion Asset Management"),
    (1336528, "Bill Ackman", "Pershing Square"),
    (1656456, "David Tepper", "Appaloosa"),
    (1061768, "Seth Klarman", "Baupost Group"),
    (1079114, "David Einhorn", "Greenlight Capital"),
    (1536411, "Stanley Druckenmiller", "Duquesne Family Office"),
    (1709323, "Li Lu", "Himalaya Capital"),
    (1040273, "Dan Loeb", "Third Point"),
    (1103804, "Andreas Halvorsen", "Viking Global"),
    (1350694, "Ray Dalio", "Bridgewater Associates"),
    (1166559, "Bill Gates", "Gates Foundation Trust"),
]
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "investors_state.json")
SUBS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
BIG_MOVE = 0.20          # adds / cuts of at least 20% of the shares count


def log(m):
    print(m, flush=True)


def get(session, url):
    r = None
    for i in range(4):
        time.sleep(0.15)
        r = session.get(url, timeout=60)
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 + 4 * i)
            continue
        return r
    return r


def latest_two(session, cik):
    """[(filed, accession, period)] of the two newest 13F-HR reports (one per period), newest first."""
    j = get(session, SUBS.format(cik=cik)).json()
    r = j["filings"]["recent"]
    out, periods = [], set()
    for form, acc, filed, period in zip(r["form"], r["accessionNumber"], r["filingDate"], r["reportDate"]):
        if form == "13F-HR" and period not in periods:
            periods.add(period)
            out.append((filed, acc, period))
        if len(out) == 2:
            break
    return out


def holdings(session, cik, acc):
    """{cusip: {'name', 'value', 'shares'}} of common stock (options skipped). Values in US dollars."""
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
                sh = float(it.findtext("shrsOrPrnAmt/sshPrnamt") or 0)
            except ValueError:
                continue
            if cusip and val > 0:
                h = out.setdefault(cusip, {"name": (it.findtext("nameOfIssuer") or "").strip().title(), "value": 0.0, "shares": 0.0})
                h["value"] += val
                h["shares"] += sh
        return out
    return {}


def quarter(period):
    d = date.fromisoformat(period)
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def diff(now, before):
    total = sum(h["value"] for h in now.values()) or 1
    new, added, cut, sold = [], [], [], []
    for c, h in now.items():
        p = before.get(c)
        if not p:
            new.append((c, h["value"], f"{h['value'] / total * 100:.1f}% of portfolio"))
        elif p["shares"] and h["shares"] >= p["shares"] * (1 + BIG_MOVE):
            added.append((c, h["value"], f"{(h['shares'] / p['shares'] - 1) * 100:.0f}% shares"))
        elif p["shares"] and h["shares"] <= p["shares"] * (1 - BIG_MOVE):
            cut.append((c, h["value"], f"−{(1 - h['shares'] / p['shares']) * 100:.0f}% shares"))
    for c, p in before.items():
        if c not in now:
            sold.append((c, p["value"], ""))
    key = lambda x: -x[1]
    top = sorted(((c, h["value"], f"{h['value'] / total * 100:.1f}%") for c, h in now.items()), key=key)[:8]
    return total, sorted(new, key=key), sorted(added, key=key), sorted(cut, key=key), sorted(sold, key=key), top


def build(session, cik, who, fund, two):
    (filed, acc, period), prev = two[0], (two[1] if len(two) > 1 else None)
    now = holdings(session, cik, acc)
    before = holdings(session, cik, prev[1]) if prev else {}
    if not now:
        return None
    total, new, added, cut, sold, top = diff(now, before)
    show = {c for lst in (new[:6], added[:6], cut[:6], sold[:6], top) for c, _, _ in lst}
    tick = cusip_tickers(show)
    names = {**{c: h["name"] for c, h in before.items()}, **{c: h["name"] for c, h in now.items()}}
    fmt = lambda lst: [(tick.get(c), names.get(c, c), v, p) for c, v, p in lst]
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
    card = cards.investor_card({
        "who": who, "fund": fund, "period": quarter(period), "filed": date.fromisoformat(filed).strftime("%b %d"),
        "total": total, "n_positions": len(now), "new": fmt(new), "added": fmt(added), "cut": fmt(cut),
        "sold": fmt(sold), "top": fmt(top), "url": url})
    content = f"🧠 **{who}** · {fund} · new 13F for {quarter(period)} · [SEC filing](<{url}>)"
    return content, card


def main():
    ua = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT required")
    test = os.environ.get("MODE") == "test"
    hook = os.environ.get("WEBHOOK_PROOF" if test else "WEBHOOK_INVESTORS", "").strip()
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    except Exception:
        state = None
    bootstrap = state is None
    state = state or {}
    only = os.environ.get("ONLY", "").strip()      # test: one fund by CIK
    for cik, who, fund in FUNDS:
        if only and str(cik) != only:
            continue
        try:
            two = latest_two(s, cik)
        except Exception as e:
            log(f"{who}: {e}")
            continue
        if not two:
            log(f"{who}: no 13F found")
            continue
        acc = two[0][1]
        if not test and state.get(str(cik)) == acc:
            continue
        if not test and bootstrap:
            state[str(cik)] = acc
            log(f"{who}: remembered {acc} ({two[0][2]})")
            continue
        try:
            out = build(s, cik, who, fund, two)
            if out and hook:
                cards.post_card(hook, out[0], out[1])
                log(f"{who}: posted {acc}")
            if not test:
                state[str(cik)] = acc
        except Exception as e:
            log(f"{who}: failed {e}")
    if not test:
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=1)


if __name__ == "__main__":
    main()
