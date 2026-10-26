"""
Company size info for alerts: market cap and S&P 500 membership.

- Market cap = shares outstanding (from the company's latest SEC report)
               x latest share price (from the price chart data).
- S&P 500    = public constituents list (matched by SEC CIK number).

Everything here is best-effort: if a lookup fails, the alert is still posted,
just without that piece of information.
"""

import csv
import io
from datetime import datetime, timedelta, timezone

SHARES_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/dei/EntityCommonStockSharesOutstanding.json"
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
MAX_SHARES_AGE_DAYS = 200      # ignore share counts older than ~2 quarters

_sp500_ciks = None
_sp500_tickers = None


def load_sp500(session):
    """Download the S&P 500 list once per run. Returns (set_of_ciks, set_of_tickers)."""
    global _sp500_ciks, _sp500_tickers
    if _sp500_ciks is not None:
        return _sp500_ciks, _sp500_tickers
    _sp500_ciks, _sp500_tickers = set(), set()
    try:
        r = session.get(SP500_URL, timeout=20)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.text)):
            sym = (row.get("Symbol") or "").upper().replace(".", "").replace("-", "")
            if sym:
                _sp500_tickers.add(sym)
            try:
                _sp500_ciks.add(int(row.get("CIK") or 0))
            except ValueError:
                pass
    except Exception as e:
        print(f"  S&P 500 list unavailable: {e}", flush=True)
    return _sp500_ciks, _sp500_tickers


def is_sp500(session, cik, ticker):
    ciks, tickers = load_sp500(session)
    norm = (ticker or "").upper().replace(".", "").replace("-", "")
    return (cik is not None and cik in ciks) or norm in tickers


def shares_outstanding(session, cik):
    """Latest reported shares outstanding, or None."""
    if not cik:
        return None
    try:
        r = session.get(SHARES_URL.format(cik=int(cik)), timeout=20)
        if r.status_code != 200:
            return None
        facts = (r.json().get("units") or {}).get("shares") or []
        if not facts:
            return None
        last_filed = max(f.get("filed", "") for f in facts)
        if not last_filed:
            return None
        age = datetime.now(timezone.utc).date() - datetime.fromisoformat(last_filed).date()
        if age > timedelta(days=MAX_SHARES_AGE_DAYS):
            return None
        latest = [f for f in facts if f.get("filed") == last_filed]
        latest_end = max(f.get("end", "") for f in latest)
        vals = {f["val"] for f in latest if f.get("end") == latest_end and f.get("val")}
        total = sum(vals)
        return total if total > 0 else None
    except Exception as e:
        print(f"  shares outstanding unavailable for CIK {cik}: {e}", flush=True)
        return None


def size_label(market_cap):
    if market_cap >= 200e9:
        return "Mega cap"
    if market_cap >= 10e9:
        return "Large cap"
    if market_cap >= 2e9:
        return "Mid cap"
    if market_cap >= 300e6:
        return "Small cap"
    return "Micro cap"


def company_info(session, cik, ticker, last_price):
    """Return {'market_cap': float|None, 'size': str|None, 'sp500': bool}."""
    info = {"market_cap": None, "size": None, "sp500": False}
    info["sp500"] = is_sp500(session, cik, ticker)
    if last_price:
        shares = shares_outstanding(session, cik)
        if shares:
            cap = shares * last_price
            info["market_cap"] = cap
            info["size"] = size_label(cap)
    return info
