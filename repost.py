"""
Re-post a cluster alert for one ticker in the current format
(chart with every insider marked + a link to each SEC Form 4).

Used for older clusters that were posted before charts/links existed.
Run from GitHub Actions -> "Repost cluster" -> Run workflow.

Environment:
  TICKER          e.g. STC
  TARGET          "test" (default, posts to WEBHOOK_PROOF / bot-testing) or "public" (WEBHOOK_CLUSTER)
  SEC_USER_AGENT  required by SEC
"""

import os
import sys

import requests

from bot import (ARCHIVE_URL, clean_ticker, CLUSTER_WINDOW_DAYS, build_cluster_embed, fetch_form4_xml, http_get,
                 load_state, log, nice_name, parse_form4, post_discord, readable_filing_url, role_text)
from chart import fetch_prices, make_cluster_chart
from company import company_info

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def issuer_cik(session, ticker):
    data = http_get(TICKERS_URL, session).json()
    for row in data.values():
        if row.get("ticker", "").upper() == ticker:
            return int(row["cik_str"])
    return None


def main():
    ticker = clean_ticker(os.environ.get("TICKER", ""))
    target = os.environ.get("TARGET", "test").strip().lower()
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ticker:
        sys.exit("TICKER is not set.")
    if target == "public":
        webhook = os.environ.get("WEBHOOK_CLUSTER", "").strip()
    else:
        webhook = (os.environ.get("WEBHOOK_PROOF") or os.environ.get("WEBHOOK_INSIDER_BUYS") or "").strip()
    if not webhook:
        sys.exit("No webhook for this target.")

    session = requests.Session()
    session.headers.update({"User-Agent": agent, "Accept-Encoding": "gzip, deflate"})

    state = load_state() or {"buys": []}
    stored = [b for b in state["buys"] if b["ticker"] == ticker and b.get("accession")]
    if not stored:
        sys.exit(f"No saved buys for {ticker} (the bot keeps the last 30 days).")
    cik = issuer_cik(session, ticker)
    if not cik:
        sys.exit(f"Could not find the SEC CIK for {ticker}.")

    buys, company = [], ticker
    for b in stored:
        acc = b["accession"]
        try:
            xml_text, base = fetch_form4_xml(session, acc, cik)
            buy = parse_form4(xml_text) if xml_text else None
        except Exception as e:
            log(f"  skip {acc}: {e}")
            continue
        if not buy:
            continue
        company = buy["company"]
        buys.append({
            "name": nice_name(buy["owner"]["name"]),
            "role": role_text(buy["owner"]["roles"]),
            "value": buy["value"],
            "price": buy["avg_price"],
            "date": buy["trade_date"] or b["date"],
            "url": readable_filing_url(session, base, acc),
        })
        log(f"  {buys[-1]['name']} {buys[-1]['date']} {buys[-1]['url']}")

    if not buys:
        sys.exit("Could not read any of the filings.")
    buys.sort(key=lambda b: b["date"])

    rows, exchange = fetch_prices(ticker)
    last_price = rows[-1][4] if rows else None
    info = company_info(session, cik, ticker, last_price)
    image = make_cluster_chart(ticker, buys, exchange)
    post_discord(webhook, build_cluster_embed(ticker, company, buys, exchange, info), image)
    log(f"Posted {ticker} cluster ({len(buys)} insiders) to {target}. Window: {CLUSTER_WINDOW_DAYS} days.")


if __name__ == "__main__":
    main()
