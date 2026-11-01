"""
Insider Score (1-10) plus the extra facts shown in an alert:

  - track record: this insider's earlier open-market buys of the same company
                  (from SEC's ownership pages) and how the stock did since
  - drop from the 52-week high (from the price data the chart already uses)
  - short interest (FINRA, published twice a month, free)

Everything is best-effort: if a lookup fails the alert is still posted,
just without that line.
"""

import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

OWNER_URL = ("https://www.sec.gov/cgi-bin/own-disp?action=getowner&CIK={cik:010d}"
             "&type=&dateb=&owner=include&start=0")
FINRA_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"

TOP_ROLES = ("chief executive", "ceo", "chief financial", "cfo", "president", "chair", "founder")


def log(msg):
    print(msg, flush=True)


def _day(s):
    try:
        return datetime.fromisoformat(str(s)[:10]).date()
    except ValueError:
        return None


def close_on(rows, day):
    """Closing price on `day` or the nearest trading day before it (None if before the data)."""
    best = None
    for r in rows:
        if r[0] <= day:
            best = r[4]
        else:
            break
    return best


# ---------------------------------------------------------------- track record

def _cells(row_html):
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S | re.I)
    return [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip() for c in cells]


def past_buys(session, owner_cik, issuer_cik, before):
    """
    Dates of this insider's earlier open-market buys (code P) of this company,
    newest first. Reads SEC's 'ownership by owner' page (latest 80 transactions).
    """
    if not owner_cik or not issuer_cik or not before:
        return []
    time.sleep(0.15)
    r = session.get(OWNER_URL.format(cik=int(owner_cik)), timeout=30)
    r.raise_for_status()
    m = re.search(r'<table[^>]*id="transaction-report"[^>]*>(.*?)</table>', r.text, re.S | re.I)
    if not m:
        return []
    days = set()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S | re.I):
        c = _cells(row)
        # A/D | date | deemed | issuer | form | type | D/I | shares | owned | line | issuer CIK | security
        if len(c) < 11 or not c[5].startswith("P-"):
            continue
        try:
            if int(c[10] or 0) != int(issuer_cik):
                continue
        except ValueError:
            continue
        d = _day(c[1])
        if d and d < before:
            days.add(d)
    return sorted(days, reverse=True)


def track_record(session, owner_cik, issuer_cik, trade_date, rows):
    """{'count', 'first', 'last', 'last_price', 'ret'} or None if no earlier buys."""
    before = _day(trade_date) or datetime.now(timezone.utc).date()
    try:
        days = past_buys(session, owner_cik, issuer_cik, before)
    except Exception as e:
        log(f"  track record unavailable: {e}")
        return None
    if not days:
        return None
    out = {"count": len(days), "first": days[-1], "last": days[0], "last_price": None, "ret": None}
    if rows:
        p = close_on(rows, days[0])
        if p:
            out["last_price"] = p
            out["ret"] = rows[-1][4] / p - 1
    return out


# ---------------------------------------------------------------- 52-week high

def drop_from_high(rows):
    """How far the last close is below the 52-week high, as a fraction (0.35 = 35% below)."""
    if not rows:
        return None
    since = rows[-1][0] - timedelta(days=365)
    year = [r for r in rows if r[0] >= since]
    high = max(r[2] for r in year)
    return 1 - year[-1][4] / high if high else None


# ---------------------------------------------------------------- short interest

def short_interest(ticker, shares_out=None):
    """Latest FINRA short interest: {'shares', 'pct', 'days_to_cover', 'date'} or None."""
    today = datetime.now(timezone.utc).date()
    body = {
        "limit": 20,
        "compareFilters": [{"compareType": "equal", "fieldName": "symbolCode", "fieldValue": ticker}],
        "dateRangeFilters": [{"fieldName": "settlementDate",
                              "startDate": (today - timedelta(days=60)).isoformat(),
                              "endDate": today.isoformat()}],
    }
    try:
        r = requests.post(FINRA_URL, json=body, headers={"Accept": "application/json"}, timeout=20)
        if r.status_code != 200 or not r.text.strip():
            return None
        rows = r.json()
        if not rows:
            return None
        last = max(rows, key=lambda x: x.get("settlementDate", ""))
        qty = float(last.get("currentShortPositionQuantity") or 0)
        if qty <= 0:
            return None
        pct = qty / shares_out if shares_out else None
        if pct is not None and pct > 1.5:       # share count looks wrong, don't show nonsense
            pct = None
        return {"shares": qty, "pct": pct, "date": last.get("settlementDate"),
                "days_to_cover": last.get("daysToCoverQuantity")}
    except Exception as e:
        log(f"  short interest unavailable for {ticker}: {e}")
        return None


# ---------------------------------------------------------------- the score

def _is_top(roles):
    text = " ".join(roles).lower().replace("vice president", "vp").replace("vice chair", "vc")
    return any(k in text for k in TOP_ROLES)


def insider_score(buy, drop=None, others_in_cluster=0, record=None, short=None):
    """
    Return (score 1-10, [short reasons]).

    Points (capped at 10):
      who      CEO/CFO/President/Chair 3 · other officer 2 · director 1 · fund/10% owner 0.5
      size     $1M+ 2 · $250K+ 1.5 · $100K+ 1 · smaller 0.5
      stake    new position or +50% 1.5 · +10% 1
      price    40%+ below 52w high 2 · 20%+ below 1
      cluster  2+ other insiders in 14 days 1.5 · one other 0.75
      repeat   bought before 0.5 · and that buy is up 0.5 more
      shorts   10%+ of shares sold short 0.5 (insiders buying against the crowd)
      plan     pre-planned 10b5-1 trade -2
    """
    pts, why = 0.0, []
    roles = buy["owner"]["roles"]
    if _is_top(roles):
        pts += 3
        why.append("top executive")
    elif any(r not in ("Director", "10% Owner") for r in roles):
        pts += 2
        why.append("officer")
    elif "Director" in roles:
        pts += 1
        why.append("director")
    else:
        pts += 0.5

    v = buy["value"]
    pts += 2 if v >= 1e6 else 1.5 if v >= 250e3 else 1 if v >= 100e3 else 0.5
    if v >= 250e3:
        why.append("big buy")

    sc = buy.get("stake_change")
    if sc is None or sc >= 50:
        pts += 1.5
        why.append("new position" if sc is None else "stake +50%")
    elif sc >= 10:
        pts += 1

    if drop is not None:
        if drop >= 0.40:
            pts += 2
            why.append("stock down 40%+")
        elif drop >= 0.20:
            pts += 1
            why.append("stock down 20%+")

    if others_in_cluster >= 2:
        pts += 1.5
        why.append("cluster buy")
    elif others_in_cluster == 1:
        pts += 0.75
        why.append("another insider bought too")

    if record:
        pts += 0.5
        if record.get("ret") is not None and record["ret"] > 0:
            pts += 0.5
            why.append("last buy is up")
        else:
            why.append("repeat buyer")

    if short and short.get("pct") and short["pct"] >= 0.10:
        pts += 0.5
        why.append("heavily shorted")

    if buy.get("plan_10b5_1"):
        pts -= 2
        why.append("pre-planned trade")

    return max(1, min(10, round(pts))), why


def score_bar(score):
    return "🟩" * score + "⬜" * (10 - score)
