"""
Find the best TikTok candidates among recent insider buys:
Insider Score + track record (did this insider buy before, and is that buy up?).

Prints a ranked table, posts nothing. Run from GitHub Actions -> "Video candidates".

Environment:
  SEC_USER_AGENT  required by SEC
  DAYS            look at buys from the last N days (default 21)
  MIN_VALUE       minimum buy size in USD (default 100000)
"""

import os
from datetime import datetime, timedelta, timezone

import requests

from bot import add_score, fetch_form4_xml, load_state, log, money, nice_name, parse_form4, readable_filing_url
from chart import fetch_prices
from company import company_info
from repost import issuer_cik


def main():
    days = int(os.environ.get("DAYS") or 21)
    min_value = float(os.environ.get("MIN_VALUE") or 100000)
    session = requests.Session()
    session.headers.update({"User-Agent": os.environ.get("SEC_USER_AGENT", ""), "Accept-Encoding": "gzip, deflate"})
    state = load_state() or {"buys": []}
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()

    # one row per (ticker, insider): keep their biggest recent buy
    best = {}
    for b in state["buys"]:
        if b["date"] < since or b["value"] < min_value or not b.get("accession"):
            continue
        key = (b["ticker"], b["name"])
        if key not in best or b["value"] > best[key]["value"]:
            best[key] = b
    log(f"Checking {len(best)} insider buys since {since}")

    ciks, rows_cache, out = {}, {}, []
    for (ticker, name), b in best.items():
        try:
            if ticker not in ciks:
                ciks[ticker] = issuer_cik(session, ticker)
            xml_text, base = fetch_form4_xml(session, b["accession"], ciks[ticker])
            buy = parse_form4(xml_text) if xml_text else None
            if not buy:
                continue
            if ticker not in rows_cache:
                rows_cache[ticker] = fetch_prices(ticker, "5y")
            rows, exchange = rows_cache[ticker]
            last = rows[-1][4] if rows else None
            buy.update(company_info(session, ciks[ticker], ticker, last))
            add_score(session, buy, rows, state, nice_name(buy["owner"]["name"]), today.isoformat())
            rec = buy.get("record") or {}
            out.append({
                "score": buy.get("score") or 0, "ticker": ticker, "name": nice_name(buy["owner"]["name"]),
                "role": b["role"], "value": buy["value"], "date": buy["trade_date"], "price": buy["avg_price"],
                "now": last, "cap": buy.get("market_cap"), "drop": buy.get("drop"),
                "rec_n": rec.get("count"), "rec_last": rec.get("last"), "rec_ret": rec.get("ret"),
                "why": ", ".join(buy.get("score_why") or []), "url": readable_filing_url(session, base, b["accession"]),
            })
        except Exception as e:
            log(f"  skip {ticker} {name}: {e}")

    out.sort(key=lambda r: (-(r["rec_ret"] is not None and r["rec_ret"] > 0), -r["score"], -r["value"]))
    log("\n===== candidates (repeat buyers whose last buy is up first, then by score) =====")
    for r in out:
        cap = money(r["cap"]) if r["cap"] else "?"
        drop = f"-{r['drop'] * 100:.0f}% from high" if r["drop"] is not None else "?"
        rec = (f"prev buy {r['rec_last']} ({r['rec_n']} earlier buys) now {r['rec_ret'] * 100:+.0f}%"
               if r["rec_ret"] is not None else (f"{r['rec_n']} earlier buys" if r["rec_n"] else "first buy"))
        now = f"${r['now']:,.2f}" if r["now"] else "?"
        log(f"{r['score']:>2}/10 | {r['ticker']:6} | {r['name']} ({r['role']}) | {money(r['value'])} on {r['date']} "
            f"at ${r['price']:,.2f}, now {now} | cap {cap} | {drop} | {rec} | {r['why']} | {r['url']}")


if __name__ == "__main__":
    main()
