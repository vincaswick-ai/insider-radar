"""
Insider Radar - Proof Finder.

Finds past insider BUYS and shows what the stock did AFTER the buy was made public.
Great source for "proof" TikTok videos (winners) and honest "misses" (losers).

How it works:
  1. Downloads SEC's quarterly "Insider Transactions Data Sets" (every Form 4,
     one zip per quarter) for the latest quarters.
  2. Keeps open-market purchases (code P) above PROOF_MIN_VALUE that were
     disclosed between PROOF_MAX_AGE_DAYS and PROOF_MIN_AGE_DAYS ago.
  3. For each stock, compares the price on the disclosure day with today
     ("if you had copied the insider the day it became public").
  4. Posts the top winners + biggest misses (with charts) to Discord.

Settings (env / GitHub variables):
  WEBHOOK_PROOF        Discord webhook (falls back to WEBHOOK_INSIDER_BUYS)
  SEC_USER_AGENT       "InsiderRadar you@example.com" (required by SEC)
  PROOF_MIN_VALUE      minimum buy size in USD (default 100000)
  PROOF_MIN_AGE_DAYS   buys disclosed at least this many days ago (default 28)
  PROOF_MAX_AGE_DAYS   ...and at most this many days ago (default 120)
"""

import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

from bot import (ALLOWED_EXCHANGES, BAD_TICKERS, money, nice_company, nice_name,
                 post_discord, role_text)
from chart import fetch_prices, render_chart
from company import company_info

DATASETS_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"

MIN_VALUE = float(os.environ.get("PROOF_MIN_VALUE") or 100000)
MIN_AGE = int(os.environ.get("PROOF_MIN_AGE_DAYS") or 28)
MAX_AGE = int(os.environ.get("PROOF_MAX_AGE_DAYS") or 120)
MAX_TICKERS_TO_PRICE = 250     # keep Yahoo requests reasonable
TOP_WINNERS = 10
TOP_LOSERS = 5
CHARTS_FOR_TOP = 3

csv.field_size_limit(10_000_000)


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- SEC data sets

def latest_dataset_urls(session, quarters_needed):
    html = session.get(DATASETS_PAGE, timeout=30).text
    links = re.findall(r'href="([^"]*?(\d{4})q(\d)_form345\.zip)"', html)
    found = {}
    for href, y, q in links:
        url = href if href.startswith("http") else "https://www.sec.gov" + href
        found[(int(y), int(q))] = url
    keys = sorted(found, reverse=True)[:quarters_needed]
    return [found[k] for k in keys]


def read_tsv(zf, name):
    member = next(n for n in zf.namelist() if n.upper().endswith(name))
    with zf.open(member) as f:
        text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
        yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def num(s):
    try:
        return float((s or "").replace(",", ""))
    except ValueError:
        return 0.0


def load_buys(session, zip_url, oldest, newest):
    """Return list of buy dicts disclosed between oldest and newest (inclusive)."""
    log(f"Downloading {zip_url.rsplit('/', 1)[-1]} ...")
    r = session.get(zip_url, timeout=180)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))

    subs = {}
    for row in read_tsv(zf, "SUBMISSION.TSV"):
        if (row.get("DOCUMENT_TYPE") or "").strip() != "4":
            continue
        filed = parse_date(row.get("FILING_DATE"))
        ticker = (row.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        if not filed or not (oldest <= filed <= newest) or not ticker or ticker in BAD_TICKERS:
            continue
        if (row.get("AFF10B5ONE") or "").strip() == "1":
            continue                          # pre-planned 10b5-1 trades: weaker signal
        subs[row["ACCESSION_NUMBER"]] = {
            "accession": row["ACCESSION_NUMBER"], "filed": filed, "ticker": ticker,
            "company": (row.get("ISSUERNAME") or "").strip(),
            "issuer_cik": int(num(row.get("ISSUERCIK"))) or None,
        }

    owners = {}
    for row in read_tsv(zf, "REPORTINGOWNER.TSV"):
        acc = row["ACCESSION_NUMBER"]
        if acc not in subs or acc in owners:
            continue
        rel = (row.get("RPTOWNER_RELATIONSHIP") or "").split(",")
        roles = []
        if "Officer" in rel:
            roles.append((row.get("RPTOWNER_TITLE") or "").strip() or "Officer")
        if "Director" in rel:
            roles.append("Director")
        if "TenPercentOwner" in rel:
            roles.append("10% Owner")
        owners[acc] = {"name": (row.get("RPTOWNERNAME") or "").strip(), "roles": roles}

    agg = defaultdict(lambda: {"shares": 0.0, "value": 0.0, "dates": []})
    for row in read_tsv(zf, "NONDERIV_TRANS.TSV"):
        acc = row["ACCESSION_NUMBER"]
        if acc not in subs or (row.get("TRANS_CODE") or "").strip() != "P":
            continue
        if (row.get("TRANS_ACQUIRED_DISP_CD") or "").strip() == "D":
            continue
        s, p = num(row.get("TRANS_SHARES")), num(row.get("TRANS_PRICEPERSHARE"))
        if s <= 0 or p <= 0:
            continue
        a = agg[acc]
        a["shares"] += s
        a["value"] += s * p
        d = parse_date(row.get("TRANS_DATE"))
        if d:
            a["dates"].append(d)

    buys = []
    for acc, a in agg.items():
        if a["value"] < MIN_VALUE or acc not in owners:
            continue
        b = dict(subs[acc])
        b.update(owner=owners[acc], shares=a["shares"], value=a["value"],
                 avg_price=a["value"] / a["shares"],
                 trade_date=min(a["dates"]) if a["dates"] else None,
                 trade_date_end=max(a["dates"]) if a["dates"] else None)
        buys.append(b)
    log(f"  open-market buys over {money(MIN_VALUE)} in window: {len(buys)}")
    return buys


# ---------------------------------------------------------------- returns

def price_on_or_after(rows, day):
    for r in rows:
        if r[0] >= day:
            return r
    return None


def evaluate(buys):
    """Group by ticker (keep biggest buy), fetch prices, compute returns."""
    by_ticker = defaultdict(list)
    for b in buys:
        by_ticker[b["ticker"]].append(b)
    groups = sorted(by_ticker.values(), key=lambda g: -max(x["value"] for x in g))
    groups = groups[:MAX_TICKERS_TO_PRICE]
    log(f"Pricing {len(groups)} stocks ...")

    results = []
    for g in groups:
        main = max(g, key=lambda x: x["value"])
        insiders = {x["owner"]["name"] for x in g}
        try:
            rows, exchange = fetch_prices(main["ticker"], "1y")
        except Exception as e:
            log(f"  skip {main['ticker']}: {e}")
            time.sleep(0.5)
            continue
        time.sleep(0.25)
        if len(rows) < 20 or exchange not in ALLOWED_EXCHANGES:
            continue
        start = price_on_or_after(rows, main["filed"])
        if not start:
            continue
        # sanity check: insider's price must be close to the real market price
        # (some foreign filers report prices in their home currency)
        at_trade = price_on_or_after(rows, main["trade_date"] or main["filed"]) or start
        ratio = main["avg_price"] / at_trade[4] if at_trade[4] else 0
        if not (0.5 <= ratio <= 2.0):
            log(f"  skip {main['ticker']}: insider price ${main['avg_price']:,.2f} vs market ${at_trade[4]:,.2f}")
            continue
        last = rows[-1]
        ret = last[4] / start[4] - 1
        r = dict(main)
        r.update(exchange=exchange, rows=rows, disclosed_close=start[4], last_close=last[4],
                 ret=ret, ret_vs_insider=last[4] / main["avg_price"] - 1,
                 insiders=len(insiders), total_value=sum(x["value"] for x in g))
        results.append(r)
    log(f"Evaluated {len(results)} stocks on major exchanges.")
    return results


# ---------------------------------------------------------------- Discord

def pct(x):
    return f"{'+' if x >= 0 else ''}{x * 100:.0f}%"


def line(i, r, session):
    info = company_info(session, r.get("issuer_cik"), r["ticker"], r["last_close"])
    tags = []
    if info.get("sp500"):
        tags.append("⭐ S&P 500")
    if info.get("size"):
        tags.append(info["size"])
    who = f"{nice_name(r['owner']['name'])} ({role_text(r['owner']['roles'])})"
    extra = f" + {r['insiders'] - 1} more insider(s)" if r["insiders"] > 1 else ""
    url = FILING_URL.format(cik=r["issuer_cik"], acc_nodash=r["accession"].replace("-", ""), acc=r["accession"])
    return (f"**{i}. ${r['ticker']}** ({r['exchange']}{', ' + ', '.join(tags) if tags else ''}) "
            f"**{pct(r['ret'])}** since disclosure\n"
            f"   {who} bought {money(r['value'])}{extra} · disclosed {r['filed']:%b %d} · "
            f"${r['disclosed_close']:,.2f} → ${r['last_close']:,.2f} · [filing]({url})")


def post_report(webhook, results, session):
    winners = sorted((r for r in results if r["ret"] > 0), key=lambda r: -r["ret"])[:TOP_WINNERS]
    losers = sorted((r for r in results if r["ret"] < 0), key=lambda r: r["ret"])[:TOP_LOSERS]
    avg = sum(r["ret"] for r in results) / len(results) if results else 0
    up = sum(1 for r in results if r["ret"] > 0)

    header = {
        "title": "🧾 Proof Finder · insider buys, then what happened",
        "description": (
            f"Insider buys over **{money(MIN_VALUE)}** disclosed **{MIN_AGE}–{MAX_AGE} days ago**, "
            f"priced from the **day they became public** to now.\n\n"
            f"Stocks checked: **{len(results)}** · went up: **{up}** ({up / max(len(results), 1) * 100:.0f}%) · "
            f"average move: **{pct(avg)}**\n\n"
            f"*Hypothetical results for content research. Not financial advice.*"),
        "color": 0x5865F2,
    }
    post_discord(webhook, header)
    if winners:
        post_discord(webhook, {"title": "🏆 Top winners", "color": 0x2ECC71,
                               "description": "\n\n".join(line(i + 1, r, session) for i, r in enumerate(winners))})
    if losers:
        post_discord(webhook, {"title": "📉 Biggest misses (good for honest content)", "color": 0xE74C3C,
                               "description": "\n\n".join(line(i + 1, r, session) for i, r in enumerate(losers))})

    for r in [w for w in winners if w["ret"] >= 0.10][:CHARTS_FOR_TOP]:
        start = r["filed"] - timedelta(days=60)
        rows = [x for x in r["rows"] if x[0] >= start]
        td = r["trade_date"] or r["filed"]
        tde = r["trade_date_end"] or td
        png = render_chart(r["ticker"], rows, td.isoformat(), tde.isoformat(), r["avg_price"], r["exchange"])
        post_discord(webhook, {
            "title": f"📈 ${r['ticker']} · {pct(r['ret'])} since the buy was disclosed",
            "description": (f"**{nice_name(r['owner']['name'])}** ({role_text(r['owner']['roles'])}) bought "
                            f"**{money(r['value'])}** of **{nice_company(r['company'])}** at ~${r['avg_price']:,.2f}. "
                            f"Disclosed {r['filed']:%b %d, %Y}."),
            "color": 0x2ECC71,
            "footer": {"text": "📡 Insider Radar · Proof Finder · Hypothetical · Not financial advice"},
        }, png)


# ---------------------------------------------------------------- main

def run():
    webhook = (os.environ.get("WEBHOOK_PROOF") or os.environ.get("WEBHOOK_INSIDER_BUYS") or "").strip()
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not webhook:
        sys.exit("Set WEBHOOK_PROOF (or WEBHOOK_INSIDER_BUYS).")
    if "@" not in ua:
        sys.exit('SEC_USER_AGENT must look like "InsiderRadar you@example.com".')

    session = requests.Session()
    session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})

    today = datetime.now(timezone.utc).date()
    oldest, newest = today - timedelta(days=MAX_AGE), today - timedelta(days=MIN_AGE)
    quarters = (MAX_AGE // 90) + 2
    buys = []
    for url in latest_dataset_urls(session, quarters):
        buys += load_buys(session, url, oldest, newest)
        time.sleep(0.5)
    if not buys:
        sys.exit("No buys found in the window (data sets may not cover it yet).")

    results = evaluate(buys)
    if not results:
        sys.exit("No stocks could be priced.")
    post_report(webhook, results, session)
    log("Proof Finder report posted.")


if __name__ == "__main__":
    run()
