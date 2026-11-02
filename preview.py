"""
Preview an alert in the CURRENT format for one ticker's latest insider buy.
Handy for testing changes (score, track record, ...) without waiting for new filings.

Run from GitHub Actions -> "Preview alert" -> Run workflow.

Environment:
  TICKER          e.g. DINO
  TARGET          "test" (default, WEBHOOK_PROOF / bot-testing) or "public" (WEBHOOK_INSIDER_BUYS)
  SEC_USER_AGENT  required by SEC
"""

import os
import re
import sys
from datetime import datetime, timezone

import requests

from bot import (add_score, build_embed, clean_ticker, fetch_form4_xml, http_get, load_state, log, money,
                 nice_name, parse_form4, post_discord, readable_filing_url)
from chart import make_chart
from company import company_info
from repost import issuer_cik
from score import recent_form4s

ISSUER_URL = ("https://www.sec.gov/cgi-bin/own-disp?action=getissuer&CIK={cik:010d}"
              "&type=P&dateb=&owner=include&start=0")


def latest_buy_filings(session, cik, limit=10):
    """(accession, folder_cik) of the issuer's most recent open-market purchase filings, newest first."""
    try:
        html = http_get(ISSUER_URL.format(cik=cik), session).text
    except Exception as e:
        log(f"  issuer page unavailable ({e}), using the SEC JSON API instead")
        return [(acc, str(cik)) for acc, _, _ in recent_form4s(session, cik, 60)]
    m = re.search(r'<table[^>]*id="transaction-report"[^>]*>(.*?)</table>', html, re.S | re.I)
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1) if m else "", re.S | re.I):
        if "P-Purchase" not in row:
            continue
        link = re.search(r"/Archives/edgar/data/(\d+)/\d+/(\d{10}-\d{2}-\d{6})-index", row)
        if link and (link.group(2), link.group(1)) not in out:
            out.append((link.group(2), link.group(1)))
        if len(out) >= limit:
            break
    return out


def main():
    ticker = clean_ticker(os.environ.get("TICKER", ""))
    target = os.environ.get("TARGET", "test").strip().lower()
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ticker:
        sys.exit("TICKER is not set.")
    webhook = (os.environ.get("WEBHOOK_INSIDER_BUYS") if target == "public"
               else os.environ.get("WEBHOOK_PROOF") or os.environ.get("WEBHOOK_INSIDER_BUYS") or "").strip()
    if not webhook:
        sys.exit("No webhook for this target.")

    session = requests.Session()
    session.headers.update({"User-Agent": agent, "Accept-Encoding": "gzip, deflate"})
    cik = issuer_cik(session, ticker)
    if not cik:
        sys.exit(f"Could not find the SEC CIK for {ticker}.")

    buy = base = acc = None
    for acc, folder in latest_buy_filings(session, cik):
        try:
            xml_text, base = fetch_form4_xml(session, acc, folder)
            buy = parse_form4(xml_text) if xml_text else None
        except Exception as e:
            log(f"  skip {acc}: {e}")
            buy = None
        want = os.environ.get("NAME", "").strip().lower()
        if buy and want and want not in buy["owner"]["name"].lower():
            buy = None
            continue
        if buy:
            break
    if not buy:
        sys.exit(f"No recent open-market buy found for {ticker}.")

    today = datetime.now(timezone.utc).date().isoformat()
    image, buy["exchange"], last_price, rows = make_chart(buy["ticker"], buy["trade_date"],
                                                         buy["trade_date_end"], buy["avg_price"])
    buy.update(company_info(session, buy.get("issuer_cik"), buy["ticker"], last_price))
    name = nice_name(buy["owner"]["name"])
    state = load_state() or {"buys": []}
    add_score(session, buy, rows, state, name, today)
    url = readable_filing_url(session, base, acc)
    post_discord(webhook, build_embed(buy, today, url), image)
    log(f"Preview posted: {ticker} {name} {money(buy['value'])} -> {target}")


if __name__ == "__main__":
    main()
