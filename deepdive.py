"""
Radar Deep Dive: extra research for the strongest insider buys, posted to #deep-dive.

Who gets one: Insider Score 7+ or part of a cluster, and at least MIN_VALUE ($100K).

Sections (each one is best-effort and left out when there is nothing solid to say):
  💰 Skin in the game  buy size vs the CEO's / executives' reported pay (SEC pay-versus-performance
                       XBRL data) and vs the shares they already owned; biggest personal buy or not
  📈 Win rate          every earlier buying spree of this insider in this stock (3 years):
                       what the stock did 6 months later, and how often it was up
  📅 Catalysts         next earnings date, an FDA decision date (PDUFA) the company itself wrote in a
                       recent filing, an FDA advisory committee meeting, House floor votes this week
                       that touch the company's sector
  🏛️ Washington        lobbying spend (Senate LDA filings) and federal contracts (USAspending.gov)
  🧭 Sector            the stock and its sector ETF over the last month: buying against the trend?
  📰 Latest news       the company's 8-K filings of the last 45 days, in plain words

Manual run: GitHub Actions -> "Deep dive" (TICKER, optional NAME, TARGET test/public).
"""

import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html import unescape

import requests

from chart import fetch_prices
from score import OWNER_URL, SPREE_DAYS, SUBMISSIONS_URL, _cells, close_on, sec_get

MIN_VALUE = 100_000
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept": "application/json, text/plain, */*"}
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/ecd/{concept}.json"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

# SEC SIC code ranges -> (sector name, sector ETF, keywords for House floor bills)
SECTORS = [
    ((3670, 3679), "Semiconductors & electronics", "SMH", ["semiconductor", "chips", "microelectronic"]),
    ((7370, 7379), "Software", "IGV", ["software", "artificial intelligence", "cyber", "data privacy"]),
    ((3570, 3579), "Computer hardware", "XLK", ["computer", "artificial intelligence"]),
    ((3720, 3729), "Aerospace & defense", "ITA", ["defense", "armed forces", "military", "aviation", "aircraft"]),
    ((3760, 3769), "Aerospace & defense", "ITA", ["defense", "missile", "space", "military"]),
    ((3812, 3812), "Aerospace & defense", "ITA", ["defense", "military", "navigation"]),
    ((3480, 3489), "Aerospace & defense", "ITA", ["defense", "firearm", "military"]),
    ((2833, 2836), "Biotech & pharma", "XBI", ["drug", "pharmaceutical", "medicare", "health", "fda"]),
    ((3841, 3851), "Medical devices", "IHI", ["medical device", "medicare", "health"]),
    ((8000, 8099), "Health care services", "XLV", ["medicare", "medicaid", "health", "hospital"]),
    ((6020, 6036), "Banks", "KBE", ["bank", "financial", "credit", "deposit"]),
    ((6300, 6411), "Insurance", "KIE", ["insurance", "flood"]),
    ((6798, 6798), "Real estate (REIT)", "XLRE", ["housing", "real estate", "mortgage"]),
    ((6000, 6799), "Financials", "XLF", ["financial", "securities", "bank", "investment"]),
    ((1300, 1389), "Oil & gas", "XLE", ["energy", "oil", "natural gas", "pipeline", "lng"]),
    ((2911, 2911), "Oil refining", "XLE", ["energy", "fuel", "oil", "refiner"]),
    ((1000, 1499), "Mining", "XME", ["mineral", "mining", "critical minerals"]),
    ((4900, 4991), "Utilities", "XLU", ["energy", "electric", "grid", "nuclear", "water"]),
    ((4800, 4899), "Telecom & media", "XLC", ["broadband", "telecommunication", "spectrum", "wireless"]),
    ((3710, 3716), "Autos", "CARZ", ["vehicle", "automotive", "electric vehicle"]),
    ((4000, 4799), "Transportation", "IYT", ["transportation", "rail", "shipping", "aviation", "highway"]),
    ((2800, 2899), "Chemicals", "XLB", ["chemical", "fertilizer"]),
    ((2000, 2099), "Food", "XLP", ["food", "agricultur", "farm"]),
    ((5200, 5999), "Retail", "XRT", ["retail", "consumer", "tariff"]),
    ((3500, 3569), "Industrial machinery", "XLI", ["manufacturing", "infrastructure", "tariff"]),
    ((1500, 1799), "Construction", "XLI", ["infrastructure", "construction", "housing"]),
    ((6500, 6599), "Real estate", "XLRE", ["housing", "real estate"]),
]

EIGHT_K = {
    "1.01": "signed a major agreement", "1.02": "ended a major agreement", "1.03": "bankruptcy",
    "2.01": "completed an acquisition or sale", "2.02": "reported results", "2.03": "took on new debt",
    "2.05": "announced restructuring or layoffs", "2.06": "booked an impairment (write-down)",
    "3.01": "received a stock exchange listing notice", "3.02": "sold new shares",
    "4.01": "changed its auditor", "4.02": "said older financials can't be relied on",
    "5.01": "had a change in control", "5.02": "changed directors or top executives",
    "5.03": "changed its bylaws", "5.07": "held a shareholder vote", "7.01": "published investor material",
    "8.01": "announced other news",
}


def log(msg):
    print(msg, flush=True)


def pct(x):
    return f"{x * 100:+.0f}%"


def money(v):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:,.{1 if div > 1e3 else 0}f}{unit}".replace(".0" + unit, unit)
    return f"${v:,.0f}"


def sector_of(sic):
    if not sic:
        return None
    for (lo, hi), name, etf, words in SECTORS:
        if lo <= sic <= hi:
            return {"name": name, "etf": etf, "words": words}
    return None


def short_company(name):
    """'TTM TECHNOLOGIES INC' -> 'TTM Technologies' for searches in other databases."""
    words = re.sub(r"[,.]", " ", name or "").split()
    drop = {"INC", "CORP", "CORPORATION", "CO", "LTD", "PLC", "LLC", "LP", "HOLDINGS", "GROUP", "THE", "/DE/", "NV"}
    keep = [w for w in words if w.upper() not in drop]
    return " ".join(w if (len(w) <= 3 and w.isupper()) else w.capitalize() for w in keep) or name


# ---------------------------------------------------------------- SEC helpers

def submissions(session, cik):
    return sec_get(session, SUBMISSIONS_URL.format(cik=int(cik))).json()


def owner_buys(session, owner_cik, issuer_cik):
    """[(date, shares)] of every open-market buy (code P) of this issuer by this owner on SEC's owner page."""
    html = sec_get(session, OWNER_URL.format(cik=int(owner_cik))).text
    m = re.search(r'<table[^>]*id="transaction-report"[^>]*>(.*?)</table>', html, re.S | re.I)
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1) if m else "", re.S | re.I):
        c = _cells(row)
        if len(c) < 11 or not c[5].startswith("P-"):
            continue
        try:
            if int(c[10] or 0) != int(issuer_cik):
                continue
            d = datetime.fromisoformat(c[1][:10]).date()
            out.append((d, float(c[7].replace(",", "") or 0)))
        except ValueError:
            continue
    return sorted(out, reverse=True)


def group_sprees(buys):
    """[(date, shares)] newest first -> [{'start', 'end', 'shares'}] where buys <= SPREE_DAYS apart are one."""
    out = []
    for d, s in buys:
        if out and (out[-1]["start"] - d).days <= SPREE_DAYS:
            out[-1]["start"] = d
            out[-1]["shares"] += s
        else:
            out.append({"start": d, "end": d, "shares": s})
    return out


# ---------------------------------------------------------------- 💰 skin in the game

def latest_concept(session, cik, concept):
    try:
        r = session.get(CONCEPT_URL.format(cik=int(cik), concept=concept), timeout=20)
        if r.status_code != 200:
            return None
        facts = (r.json().get("units") or {}).get("USD") or []
        facts = [f for f in facts if f.get("val")]
        if not facts:
            return None
        f = max(facts, key=lambda f: (f.get("end", ""), f.get("filed", "")))
        return {"val": float(f["val"]), "year": f.get("end", "")[:4]}
    except Exception:
        return None


def skin_in_the_game(session, buy, issuer_cik, sprees, rows):
    lines = []
    roles = " ".join(buy["owner"]["roles"]).lower()
    value = buy["value"]
    is_ceo = "chief executive" in roles or re.search(r"\bceo\b", roles)
    officer = any(r not in ("Director", "10% Owner") for r in buy["owner"]["roles"])
    if is_ceo:
        pay = latest_concept(session, issuer_cik, "PeoTotalCompAmt")
        if pay and pay["val"] > 0:
            lines.append(f"This {money(value)} buy is **{value / pay['val'] * 100:.0f}%** of the CEO's total "
                         f"reported pay for {pay['year']} ({money(pay['val'])}).")
    elif officer:
        pay = latest_concept(session, issuer_cik, "NonPeoNeoAvgTotalCompAmt")
        if pay and pay["val"] > 0:
            lines.append(f"This {money(value)} buy is **{value / pay['val'] * 100:.0f}%** of the average yearly pay "
                         f"of the company's top executives ({money(pay['val'])} in {pay['year']}).")
    before = buy["owned_after"] - buy["shares"]
    if before > 0:
        lines.append(f"It adds **{buy['shares'] / before * 100:.0f}%** to the {before:,.0f} shares they already owned.")
    elif buy.get("new_position"):
        lines.append("They owned **no shares** before this buy.")
    # biggest personal buy? compare with earlier sprees at the price of their first day
    earlier = [s for s in sprees if s["end"] < date.fromisoformat(buy["trade_date"]) - timedelta(days=SPREE_DAYS)]
    vals = [s["shares"] * (close_on(rows, s["start"]) or 0) for s in earlier]
    vals = [v for v in vals if v > 0]
    if vals and value > max(vals):
        lines.append(f"Their **biggest buy** of this stock on record (earlier best ≈ {money(max(vals))}).")
    return lines


# ---------------------------------------------------------------- 📈 win rate

def win_rate(buy, sprees, rows, spy):
    """6-month results of every earlier buying spree in the last 3 years."""
    trade = date.fromisoformat(buy["trade_date"])
    last = rows[-1][0] if rows else None
    done, open_ = [], []
    for s in sprees:
        d = s["start"]
        if s["end"] >= trade - timedelta(days=SPREE_DAYS) or d < trade - timedelta(days=3 * 365):
            continue
        base = close_on(rows, d)
        if not base:
            continue
        later = d + timedelta(days=182)
        if last and later <= last:
            r = close_on(rows, later) / base - 1
            sp = (close_on(spy, later) / close_on(spy, d) - 1) if spy and close_on(spy, d) else None
            done.append((d, r, sp))
        elif last:
            open_.append((d, rows[-1][4] / base - 1))
    lines = []
    if done:
        wins = sum(1 for _, r, _ in done if r > 0)
        avg = sum(r for _, r, _ in done) / len(done)
        times = "time" if len(done) == 1 else "times"
        line = (f"Bought this stock **{len(done)} {times}** in the last 3 years (6+ months ago). "
                f"6 months later the stock was up **{wins} of {len(done)}** times, **{pct(avg)}** on average")
        sps = [sp for _, _, sp in done if sp is not None]
        if sps:
            line += f" (S&P 500: {pct(sum(sps) / len(sps))})"
        lines.append(line + ".")
        best = max(done, key=lambda x: x[1])
        lines.append(f"Best: the {best[0]:%b %Y} buy, {pct(best[1])} after 6 months.")
    for d, r in open_:
        lines.append(f"Buy from {d:%b %d, %Y} (under 6 months old): {pct(r)} so far.")
    return lines, done


# ---------------------------------------------------------------- 📅 catalysts

def earnings_date(ticker):
    """Next earnings date (date or None): Nasdaq first, Yahoo as a fallback."""
    try:
        r = requests.get(f"https://api.nasdaq.com/api/analyst/{ticker}/earnings-date", headers=BROWSER, timeout=15)
        text = ((r.json().get("data") or {}).get("announcement") or "")
        log(f"  nasdaq earnings: HTTP {r.status_code} '{text[:80]}'")
        m = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", text)
        if m:
            return datetime.strptime(" ".join(m.groups()), "%b %d %Y").date()
    except Exception as e:
        log(f"  nasdaq earnings date unavailable: {e}")
    try:
        s = requests.Session()
        s.headers.update(BROWSER)
        s.get("https://fc.yahoo.com", timeout=10)
        crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10).text.strip()
        r = s.get(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                  params={"modules": "calendarEvents", "crumb": crumb}, timeout=15)
        ev = r.json()["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]["earningsDate"]
        log(f"  yahoo earnings: {ev}")
        if ev:
            return datetime.fromtimestamp(ev[0]["raw"], timezone.utc).date()
    except Exception as e:
        log(f"  yahoo earnings date unavailable: {e}")
    return None


def doc_text(session, cik, acc, doc):
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
    html = sec_get(session, url).text
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def pdufa_date(session, cik, subs, today):
    """A future FDA decision date (PDUFA) that the company wrote in one of its recent 10-Q/10-K/8-K filings."""
    rec = (subs.get("filings") or {}).get("recent") or {}
    checked = 0
    month = "|".join(MONTHS)
    for form, acc, doc, filed in zip(rec.get("form", []), rec.get("accessionNumber", []),
                                     rec.get("primaryDocument", []), rec.get("filingDate", [])):
        if form not in ("10-Q", "10-K", "8-K") or filed < (today - timedelta(days=120)).isoformat():
            continue
        checked += 1
        if checked > 6:
            break
        try:
            text = doc_text(session, cik, acc, doc)
        except Exception:
            continue
        for m in re.finditer(r"PDUFA", text):
            window = text[max(0, m.start() - 300): m.end() + 300]
            for d in re.finditer(rf"({month}) (\d{{1,2}}), (20\d\d)", window):
                try:
                    when = datetime.strptime(" ".join(d.groups()), "%B %d %Y").date()
                except ValueError:
                    continue
                if when > today:
                    return when, form, filed
    return None


def fda_advisory(company, today):
    """FDA advisory committee meetings on the FDA calendar that mention the company."""
    try:
        r = requests.get("https://www.fda.gov/advisory-committees/advisory-committee-calendar",
                         headers=BROWSER, timeout=20)
        text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", r.text)))
    except Exception as e:
        log(f"  FDA calendar unavailable: {e}")
        return None
    first = company.split()[0]
    if len(first) < 4:
        return None
    for m in re.finditer(re.escape(first), text, re.I):
        window = text[max(0, m.start() - 250): m.end() + 250]
        d = re.search(r"(\d{2})/(\d{2})/(20\d\d)", window) or re.search(
            rf"({'|'.join(MONTHS)}) (\d{{1,2}}), (20\d\d)", window)
        if d:
            try:
                when = (date(int(d.group(3)), int(d.group(1)), int(d.group(2))) if "/" in d.group(0)
                        else datetime.strptime(" ".join(d.groups()), "%B %d %Y").date())
            except ValueError:
                continue
            if when >= today:
                return when
    return None


def house_votes(words, today):
    """Bills on the House floor schedule this week whose titles touch the sector."""
    monday = today - timedelta(days=today.weekday())
    stamp = monday.strftime("%Y%m%d")
    for url in (f"https://docs.house.gov/floor/Download.aspx?file=%2Fbillsthisweek%2F{stamp}%2F{stamp}.xml",
                f"https://docs.house.gov/billsthisweek/{stamp}/{stamp}.xml"):
        try:
            r = requests.get(url, headers=BROWSER, timeout=20)
            if r.status_code != 200 or "<" not in r.text[:50]:
                continue
            root = ET.fromstring(r.content)
        except Exception:
            continue
        out = []
        for item in root.iter("floor-item"):
            num = (item.findtext("legis-num") or "").strip()
            title = re.sub(r"\s+", " ", (item.findtext("floor-text") or "").strip())
            if title and any(w in title.lower() for w in words):
                out.append(f"{num} – {title[:110]}" if num else title[:120])
        log(f"  House floor {stamp}: {sum(1 for _ in root.iter('floor-item'))} bills, {len(out)} match")
        return out[:3]
    log(f"  House floor schedule {stamp} not found")
    return []


def catalysts(session, buy, issuer_cik, subs, sector, today):
    lines = []
    e = earnings_date(buy["ticker"])
    if e and e >= today:
        n = (e - today).days
        lines.append(f"**Earnings {'today' if n == 0 else 'tomorrow' if n == 1 else f'in {n} days'}** ({e:%b %d}).")
    sic = int(subs.get("sic") or 0)
    if 2833 <= sic <= 2836 or 3841 <= sic <= 3851 or sic == 8731:
        p = pdufa_date(session, issuer_cik, subs, today)
        if p:
            lines.append(f"**FDA decision date (PDUFA): {p[0]:%b %d, %Y}**, {(p[0] - today).days} days away "
                         f"(from the company's own {p[1]} filed {p[2]}).")
        a = fda_advisory(short_company(buy["company"]), today)
        if a:
            lines.append(f"FDA advisory committee meeting mentioning the company on **{a:%b %d, %Y}**.")
    if sector:
        for bill in house_votes(sector["words"], today):
            lines.append(f"On the House floor this week: {bill}")
    return lines


# ---------------------------------------------------------------- 🏛️ Washington

def lobbying(company, today):
    """Lobbying spend reported for the company in Senate LDA filings: last 4 quarters vs the 4 before."""
    name = company.split(" ")[0] if len(company.split(" ")[0]) >= 5 else company
    key = os.environ.get("LDA_API_KEY", "").strip()
    headers = dict(BROWSER, **({"Authorization": f"Token {key}"} if key else {}))
    order = {"first_quarter": 1, "second_quarter": 2, "third_quarter": 3, "fourth_quarter": 4}
    per_q = {}
    for base in ("https://lda.senate.gov/api/v1/filings/", "https://lda.gov/api/v1/filings/"):
        try:
            for year in (today.year - 2, today.year - 1, today.year):
                url, params = base, {"client_name": name, "filing_year": year, "page_size": 100}
                while url:
                    r = requests.get(url, params=params, headers=headers, timeout=25)
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    data = r.json()
                    for f in data.get("results", []):
                        client = ((f.get("client") or {}).get("name") or "").lower()
                        if company.lower().split()[0] not in client:
                            continue
                        q = order.get(f.get("filing_period"))
                        amt = float(f.get("income") or 0) + float(f.get("expenses") or 0)
                        if q and amt:
                            per_q[(f.get("filing_year"), q)] = per_q.get((f.get("filing_year"), q), 0) + amt
                    url, params = data.get("next"), None
                    time.sleep(0.5)
            break
        except Exception as e:
            log(f"  lobbying data unavailable at {base}: {e}")
            per_q = {}
    if not per_q:
        return []
    qs = sorted(per_q)
    last4 = sum(per_q[q] for q in qs[-4:])
    prev4 = sum(per_q[q] for q in qs[-8:-4])
    line = f"Spent **{money(last4)}** on lobbying in the last 4 reported quarters"
    if prev4 > 0:
        line += f" ({pct(last4 / prev4 - 1)} vs the 4 before)"
    return [line + " (Senate LDA filings)."]


def contracts(company, today):
    """Federal contracts awarded to the company in the last 12 months (USAspending.gov)."""
    body = {
        "filters": {"recipient_search_text": [company],
                    "award_type_codes": ["A", "B", "C", "D"],
                    "time_period": [{"start_date": (today - timedelta(days=365)).isoformat(),
                                     "end_date": today.isoformat()}]},
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency"],
        "limit": 100, "sort": "Award Amount", "order": "desc",
    }
    try:
        r = requests.post("https://api.usaspending.gov/api/v2/search/spending_by_award/", json=body, timeout=30)
        rows = r.json().get("results") or []
        log(f"  USAspending: HTTP {r.status_code}, {len(rows)} awards, names: "
            f"{sorted({x.get('Recipient Name') for x in rows})[:5]} {r.text[:200] if r.status_code != 200 else ''}")
    except Exception as e:
        log(f"  USAspending unavailable: {e}")
        return []
    first = company.lower().split()[0]
    rows = [x for x in rows if first in (x.get("Recipient Name") or "").lower() and (x.get("Award Amount") or 0) > 0]
    if not rows:
        return []
    total = sum(x["Award Amount"] for x in rows)
    agencies = {}
    for x in rows:
        agencies[x.get("Awarding Agency") or "?"] = agencies.get(x.get("Awarding Agency") or "?", 0) + x["Award Amount"]
    top = max(agencies, key=agencies.get)
    return [f"Won **{money(total)}** in federal contracts in the last 12 months ({len(rows)} awards, "
            f"mostly {top}) (USAspending.gov)."]


# ---------------------------------------------------------------- 🧭 sector

def sector_check(rows, sector, who="The insider"):
    if not sector or not rows or len(rows) < 25:
        return []
    try:
        etf = fetch_prices(sector["etf"], "3mo")[0]
    except Exception:
        etf = []
    month_ago = rows[-1][0] - timedelta(days=30)
    stock = rows[-1][4] / close_on(rows, month_ago) - 1
    line = f"Last month: the stock **{pct(stock)}**"
    if etf and close_on(etf, month_ago):
        sec = etf[-1][4] / close_on(etf, month_ago) - 1
        line += f", its sector ({sector['name']}, {sector['etf']}) **{pct(sec)}**."
        if stock < -0.05 and stock < sec - 0.05:
            line += f" {who} bought while the stock lagged its own sector: a **contrarian** buy."
        elif sec < -0.03:
            line += f" {who} bought while the **whole sector was falling**."
        return [line]
    return [line + "."]


# ---------------------------------------------------------------- 📰 latest news (8-K)

def news(subs, today):
    rec = (subs.get("filings") or {}).get("recent") or {}
    out = []
    for form, filed, items in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("items", [])):
        if form != "8-K" or filed < (today - timedelta(days=45)).isoformat():
            continue
        words = [EIGHT_K[i] for i in (items or "").split(",") if i.strip() in EIGHT_K]
        if words:
            out.append(f"{datetime.fromisoformat(filed):%b %d}: {', '.join(dict.fromkeys(words))}")
        if len(out) >= 4:
            break
    return out


# ---------------------------------------------------------------- the embed

def wanted(buy, in_cluster=False):
    return buy["value"] >= MIN_VALUE and ((buy.get("score") or 0) >= 7 or in_cluster)


def build(session, buy, rows, filing_url=None, in_cluster=False):
    """Return a Discord embed for the deep dive (never raises; sections that fail are left out)."""
    today = datetime.now(timezone.utc).date()
    issuer = buy.get("issuer_cik")
    sections = []

    def add(title, fn):
        try:
            lines = fn()
            if lines:
                sections.append(f"**{title}**\n" + "\n".join(f"• {x}" for x in lines))
        except Exception as e:
            log(f"  deep dive section '{title}' failed: {e}")

    try:
        subs = submissions(session, issuer)
    except Exception:
        subs = {}
    sector = sector_of(int(subs.get("sic") or 0))
    try:
        sprees = group_sprees(owner_buys(session, buy["owner"]["cik"], issuer)) if buy["owner"].get("cik") else []
    except Exception as e:
        log(f"  owner history unavailable: {e}")
        sprees = []
    try:
        long_rows = fetch_prices(buy["ticker"], "5y")[0]
        spy = fetch_prices("SPY", "5y")[0]
    except Exception:
        long_rows, spy = rows or [], []
    company = short_company(buy["company"])

    add("💰 SKIN IN THE GAME", lambda: skin_in_the_game(session, buy, issuer, sprees, long_rows))
    add("📈 WIN RATE", lambda: win_rate(buy, sprees, long_rows, spy)[0])
    add("📅 CATALYSTS", lambda: catalysts(session, buy, issuer, subs, sector, today))
    add("🏛️ WASHINGTON", lambda: lobbying(company, today) + contracts(company, today))
    add("🧭 SECTOR", lambda: sector_check(long_rows, sector))
    add("📰 LATEST COMPANY NEWS (SEC 8-K)", lambda: news(subs, today))

    head = (f"**{buy.get('owner_label') or buy['owner']['name']}** bought **{money(buy['value'])}** "
            f"of **{company}** on {buy['trade_date']}.")
    if buy.get("score"):
        head = f"🎯 Insider Score **{buy['score']}/10**" + (" · 🔥 cluster buy" if in_cluster else "") + "\n" + head
    desc = head + "\n\n" + ("\n\n".join(sections) if sections else "_No extra facts found for this one._")
    if filing_url:
        desc += f"\n\n📄 [SEC filing]({filing_url})"
    return {
        "title": f"🔍 Radar Deep Dive · ${buy['ticker']}",
        "url": filing_url,
        "description": desc[:4000],
        "color": 0x9B59B6,
        "footer": {"text": "📡 Insider Radar · public data (SEC, Senate LDA, USAspending, House, FDA) · "
                           "Not financial advice"},
    }


# ---------------------------------------------------------------- politicians

def pol_wanted(trade, amount_top):
    """A politician's stock buy worth a deep dive: $50K+ or in their own committee's sector."""
    return (trade.get("type") == "P" and not trade.get("option")
            and (amount_top >= 50_001 or trade.get("match")))


def build_politician(session, trade, who, filing_url, committees=None):
    """Deep dive for one politician stock buy. trade: {'ticker','date','amount','match'}."""
    today = datetime.now(timezone.utc).date()
    from repost import issuer_cik
    sections = []

    def add(title, fn):
        try:
            lines = fn()
            if lines:
                sections.append(f"**{title}**\n" + "\n".join(f"• {x}" for x in lines))
        except Exception as e:
            log(f"  politician deep dive '{title}' failed: {e}")

    ticker = trade["ticker"]
    cik = issuer_cik(session, ticker)
    subs = submissions(session, cik) if cik else {}
    company = short_company(subs.get("name") or ticker)
    sector = sector_of(int(subs.get("sic") or 0))
    try:
        rows = fetch_prices(ticker, "1y")[0]
    except Exception:
        rows = []
    fake_buy = {"ticker": ticker, "company": company}

    def why():
        out = []
        if trade.get("match"):
            comm, sec = trade["match"][0], trade["match"][1]
            out.append(f"⚠️ They sit on the **{comm}** committee, which oversees **{sec}**, and bought a "
                       f"{sec.lower()} stock.")
        elif committees:
            out.append(f"Committees: {', '.join(committees[:3])}.")
        d = trade.get("date")
        if rows and d:
            day = d if isinstance(d, date) else date.fromisoformat(str(d))
            base = close_on(rows, day)
            if base:
                out.append(f"The stock is **{pct(rows[-1][4] / base - 1)}** since their trade on {day:%b %d}.")
        return out

    add("🧠 WHY IT STANDS OUT", why)
    add("📅 CATALYSTS", lambda: catalysts(session, fake_buy, cik, subs, sector, today) if cik else [])
    add("🏛️ WASHINGTON", lambda: lobbying(company, today) + contracts(company, today))
    add("🧭 SECTOR", lambda: sector_check(rows, sector, "They"))
    add("📰 LATEST COMPANY NEWS (SEC 8-K)", lambda: news(subs, today))
    desc = (f"**{who}** bought **{trade['amount']}** of **{company}** (${ticker}).\n\n"
            + ("\n\n".join(sections) if sections else "_No extra facts found for this one._")
            + f"\n\n📄 [Disclosure filing]({filing_url})")
    return {
        "title": f"🔍 Radar Deep Dive · Politician · ${ticker}",
        "url": filing_url,
        "description": desc[:4000],
        "color": 0xE67E22,
        "footer": {"text": "📡 Insider Radar · public data (House Clerk, SEC, Senate LDA, USAspending) · "
                           "Not financial advice"},
    }


# ---------------------------------------------------------------- manual run

def main():
    from bot import (add_score, fetch_form4_xml, load_state, nice_name, parse_form4, post_discord,
                     readable_filing_url, role_text)
    from company import company_info
    from preview import latest_buy_filings
    from repost import issuer_cik

    ticker = (os.environ.get("TICKER") or "").strip().upper().lstrip("$")
    target = (os.environ.get("TARGET") or "test").strip().lower()
    webhook = (os.environ.get("WEBHOOK_DEEPDIVE") if target == "public" else os.environ.get("WEBHOOK_PROOF")) or ""
    if not ticker or not webhook.strip():
        sys.exit("TICKER and a webhook for the target are required.")
    session = requests.Session()
    session.headers.update({"User-Agent": os.environ.get("SEC_USER_AGENT", ""), "Accept-Encoding": "gzip, deflate"})
    cik = issuer_cik(session, ticker)
    want = (os.environ.get("NAME") or "").strip().lower()
    buy = base = acc = None
    for acc, folder in latest_buy_filings(session, cik):
        try:
            xml_text, base = fetch_form4_xml(session, acc, folder)
            buy = parse_form4(xml_text) if xml_text else None
        except Exception:
            buy = None
        if buy and (not want or want in buy["owner"]["name"].lower()):
            break
        buy = None
    if not buy:
        sys.exit(f"No recent open-market buy found for {ticker}.")
    rows = fetch_prices(ticker, "1y")[0]
    buy.update(company_info(session, buy.get("issuer_cik"), ticker, rows[-1][4] if rows else None))
    name = nice_name(buy["owner"]["name"])
    buy["owner_label"] = f"{name} ({role_text(buy['owner']['roles'])})"
    add_score(session, buy, rows, load_state() or {"buys": []}, name, date.today().isoformat())
    embed = build(session, buy, rows, readable_filing_url(session, base, acc))
    log(embed["description"])
    post_discord(webhook.strip(), embed, None)
    log(f"Deep dive posted: {ticker} {name} -> {target}")


if __name__ == "__main__":
    main()
