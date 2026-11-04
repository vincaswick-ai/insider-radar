"""
Case Files: an insider buy + the known events coming up for that company.

Called by bot.py for every qualifying buy. A case opens only when the radar finds
at least one upcoming event within LOOKAHEAD_DAYS:
  - the company's next earnings date        (Nasdaq earnings calendar, cached daily)
  - an FDA decision date for the ticker      (RTTNews FDA calendar, cached daily)
  - a calendar event for its sector          (events.json, kept by hand: elections, Fed, votes...)
Live odds come from Polymarket's public search API.

When the (first) event has passed, update_cases() posts the result: price now vs the insider's price.
Everything is best-effort: a failed source just means fewer connections, never a crashed run.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone

import requests

import cards

LOOKAHEAD_DAYS = 45
MIN_VALUE = 100_000
CACHE_FILE = "casefiles_cache.json"
EVENTS_FILE = "events.json"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# SIC code ranges -> (sector key, label)
SIC_SECTORS = [
    ((2833, 2836), "biotech", "Biotech & pharma"), ((8731, 8731), "biotech", "Biotech & pharma"),
    ((6020, 6036), "banks", "Banks"), ((6199, 6199), "banks", "Finance"),
    ((1311, 1389), "oil", "Oil & gas"), ((2911, 2999), "oil", "Oil & gas"),
    ((3721, 3769), "defense", "Aerospace & defense"), ((3812, 3812), "defense", "Aerospace & defense"),
    ((3480, 3489), "defense", "Aerospace & defense"),
    ((3674, 3674), "semis", "Semiconductors"),
    ((7370, 7379), "software", "Software & internet"),
    ((6311, 6411), "insurance", "Insurance"),
    ((6798, 6798), "realestate", "Real estate"), ((6500, 6553), "realestate", "Real estate"),
    ((8000, 8099), "health", "Healthcare"), ((6324, 6324), "health", "Health insurance"),
    ((3711, 3716), "autos", "Autos"),
    ((4911, 4991), "utilities", "Utilities"),
    ((1000, 1099), "mining", "Mining"),
    ((4512, 4522), "airlines", "Airlines"),
    ((5200, 5999), "retail", "Retail"),
]
NAME_SECTORS = [(r"\bsolar|enphase|sunrun|first solar", "solar", "Solar")]


def log(msg):
    print(msg, flush=True)


def money(v):
    return cards.money(v)


# ---------------------------------------------------------------- company profile

_profiles = {}


def profile(session, cik, company=""):
    """{'sic': int|None, 'industry': str, 'sector': key|None, 'label': str}"""
    if cik in _profiles:
        return _profiles[cik]
    sic, industry = None, ""
    try:
        r = session.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", timeout=20)
        if r.status_code == 200:
            j = r.json()
            sic = int(j.get("sic") or 0) or None
            industry = (j.get("sicDescription") or "").strip()
    except Exception as e:
        log(f"  profile unavailable: {e}")
    sector, label = None, ""
    text = f"{company} {industry}".lower()
    for pat, key, lab in NAME_SECTORS:
        if re.search(pat, text):
            sector, label = key, lab
    if not sector and sic:
        for (lo, hi), key, lab in SIC_SECTORS:
            if lo <= sic <= hi:
                sector, label = key, lab
                break
    p = {"sic": sic, "industry": industry.title() if industry.isupper() else industry,
         "sector": sector, "label": label or (industry.title() if industry.isupper() else industry)}
    _profiles[cik] = p
    return p


# ---------------------------------------------------------------- daily cache: earnings + FDA

def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(c):
    with open(CACHE_FILE, "w") as f:
        json.dump(c, f)


def _earnings_calendar(days):
    out = {}
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
                      "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    d = date.today()
    for i in range(days + 1):
        day = d + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        try:
            r = s.get(f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}", timeout=10)
            rows = ((r.json().get("data") or {}).get("rows")) or []
            for row in rows:
                sym = (row.get("symbol") or "").upper().strip()
                if sym and sym not in out:
                    out[sym] = {"date": day.isoformat(), "time": (row.get("time") or "").replace("time-", "")}
        except Exception as e:
            log(f"  earnings calendar {day}: {e}")
    return out


def _fda_calendar():
    """{TICKER: [{'date': iso, 'drug': str}]} parsed from RTTNews' FDA calendar (div.grid-row blocks)."""
    out = {}
    try:
        r = requests.get("https://www.rttnews.com/corpinfo/fdacalendar.aspx",
                         headers={"User-Agent": BROWSER_UA}, timeout=30)
        for block in r.text.split('class="grid-row')[1:]:
            cells = {}
            for k, v in re.findall(r'data-th="([^"]+)"[^>]*>(.*?)</div>', block, flags=re.S):
                cells[k] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v)).strip()
            m_t = re.search(r"\(\s*([A-Z]{1,5})\s*[,)]", cells.get("Company Name", ""))
            m_d = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b", cells.get("Event", ""))
            if not (m_t and m_d):
                continue
            try:
                d = date(int(m_d.group(3)), int(m_d.group(1)), int(m_d.group(2)))
            except ValueError:
                continue
            out.setdefault(m_t.group(1), []).append({"date": d.isoformat(), "drug": cells.get("Drug", "")[:60]})
    except Exception as e:
        log(f"  FDA calendar unavailable: {e}")
    return out


def calendars():
    c = _load_cache()
    today = date.today().isoformat()
    if c.get("day") != today:
        log("  refreshing earnings + FDA calendars")
        c = {"day": today, "earnings": _earnings_calendar(LOOKAHEAD_DAYS), "fda": _fda_calendar()}
        _save_cache(c)
        log(f"  calendars: {len(c['earnings'])} earnings dates, {len(c['fda'])} FDA tickers")
    return c


def load_events():
    try:
        with open(EVENTS_FILE) as f:
            return json.load(f).get("events", [])
    except Exception:
        return []


# ---------------------------------------------------------------- Polymarket

def polymarket(query):
    """Most-traded active event for the query, shown by its most likely outcome:
    {'question','yes_label','yes','no_label','no','url'} or None."""
    try:
        r = requests.get("https://gamma-api.polymarket.com/public-search",
                         params={"q": query, "limit_per_type": 8, "events_status": "active"},
                         headers={"User-Agent": BROWSER_UA}, timeout=20)
        best = None
        for ev in (r.json().get("events") or []):
            if ev.get("closed"):
                continue
            top, vol = None, 0.0
            for m in ev.get("markets") or []:
                if m.get("closed") or not m.get("active", True):
                    continue
                try:
                    outs = json.loads(m.get("outcomes") or "[]")
                    prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                except Exception:
                    continue
                if len(outs) != 2 or len(prices) != 2:
                    continue
                vol += float(m.get("volume") or 0)
                if not top or prices[0] > top["yes"]:
                    top = {"question": m.get("question") or ev.get("title"),
                           "yes_label": str(outs[0]).upper(), "yes": prices[0],
                           "no_label": str(outs[1]).upper(), "no": prices[1],
                           "url": f"https://polymarket.com/event/{ev.get('slug')}"}
            if top and vol >= 5000 and (not best or vol > best[0]):
                best = (vol, top)
        return best[1] if best else None
    except Exception as e:
        log(f"  polymarket '{query}': {e}")
        return None


# ---------------------------------------------------------------- the radar

def upcoming(ticker, sector, today):
    """Sorted list of events within LOOKAHEAD_DAYS: {date, title, short, kind, why?, scenarios?, poly?}"""
    c = calendars()
    end = today + timedelta(days=LOOKAHEAD_DAYS)
    ev = []
    e = (c.get("earnings") or {}).get(ticker)
    if e:
        d = date.fromisoformat(e["date"])
        if today < d <= end:
            ev.append({"date": d, "kind": "earnings", "title": "Next earnings report",
                       "short": "earnings", "why": "The first numbers investors see after this buy.",
                       "scenarios": [["If results beat", "The insider's timing looks right; the stock could re-rate."],
                                     ["If results disappoint", "The buy was early, or wrong."]]})
    for f in (c.get("fda") or {}).get(ticker, []):
        d = date.fromisoformat(f["date"])
        if today < d <= end:
            ev.append({"date": d, "kind": "fda", "title": f"FDA decision{': ' + f['drug'] if f.get('drug') else ''}",
                       "short": "an FDA decision", "why": "An approval or rejection can move the stock a lot in one day.",
                       "scenarios": [["If the FDA approves", "The product can be sold; this is often the biggest catalyst."],
                                     ["If the FDA rejects or delays", "The stock usually falls hard."]]})
    for x in load_events():
        d = date.fromisoformat(x["date"])
        if not (today < d <= end):
            continue
        if ticker in x.get("tickers", []) or (sector and sector in x.get("sectors", [])):
            ev.append({"date": d, "kind": "calendar", "title": x["title"], "short": x.get("short", x["title"]),
                       "why": x.get("why", ""), "scenarios": x.get("scenarios"), "poly": x.get("polymarket_q")})
    return sorted(ev, key=lambda e: e["date"])


def qualifies(buy, others_14d, only_big_holder):
    if only_big_holder or buy["value"] < MIN_VALUE or buy.get("plan_10b5_1"):
        return False
    sc = buy.get("stake_change")
    return bool(buy.get("new_position") or (sc is not None and sc >= 50) or others_14d >= 1)


def _days(d, today):
    n = (d - today).days
    return "tomorrow" if n == 1 else f"in {n} days"


def maybe_open(webhook, state, buy, info):
    """Open a case if the buy qualifies and the radar finds events. Returns True if posted.
    info: name, role, role_short, company_short, filing_url, exchange, profile, others_14d, only_big_holder, filed"""
    if not webhook or not qualifies(buy, info["others_14d"], info["only_big_holder"]):
        return False
    t = buy["ticker"]
    today = date.today()
    cases = state.setdefault("cases", [])
    if any(c["ticker"] == t and (today - date.fromisoformat(c["opened"])).days < 30 for c in cases):
        return False
    prof = info["profile"]
    events = upcoming(t, prof.get("sector"), today)
    if not events:
        log(f"  case check {t}: no upcoming events")
        return False

    first = events[0]
    n = len(cases) + 1
    drop = buy.get("drop")
    drop_text = f"<b class='r'>−{drop * 100:.0f}%</b> from its 1-year high" if drop and drop >= 0.05 else "near its 1-year high"
    title = (f"${t} · {info['company_short']}'s {info['role_short']} buys {money(buy['value'])}, "
             f"{(first['date'] - today).days} day{'s' if (first['date'] - today).days != 1 else ''} before {first['short']}")
    summary = (f"{info['name']} bought shares with their own money"
               + (f" after the stock fell {drop * 100:.0f}% from its high" if drop and drop >= 0.15 else "")
               + f", {(first['date'] - today).days} day{'s' if (first['date'] - today).days != 1 else ''} before {first['short']}.")
    timeline = []
    if drop and drop >= 0.15:
        timeline.append({"when": "Before the buy", "color": "#ed4245",
                         "text": f"Stock <b class='r'>−{drop * 100:.0f}%</b> from its 1-year high."})
    stake = ("opening a <b>new position</b>" if buy.get("new_position") else
             f"stake <b class='g'>+{buy['stake_change']:.0f}%</b>" if buy.get("stake_change") else "")
    timeline.append({"when": f"{info['filed']} · today", "color": "#57f287",
                     "text": f"{cards.esc(info['role_short'])} buys <b class='g'>{money(buy['value'])}</b>"
                             + (f", {stake}" if stake else "")
                             + (f". {info['others_14d']} other insider{'s' if info['others_14d'] != 1 else ''} bought in the last 14 days" if info["others_14d"] else "") + "."})
    for e in events[:4]:
        timeline.append({"when": f"{e['date']:%b %d} · {_days(e['date'], today)} ⏳", "color": "#fee75c",
                         "text": f"<b>{cards.esc(e['title'])}.</b> {cards.esc(e.get('why') or '')}"})

    market = None
    for q in [e.get("poly") for e in events if e.get("poly")] + [info["company_short"]]:
        if q:
            market = polymarket(q)
            # a company-name search must really be about that company
            if market and q == info["company_short"] and q.split()[0].lower() not in (market["question"] or "").lower():
                market = None
            if market:
                break
    card = cards.case_card({
        "number": n, "ticker": t, "sector": prof.get("label") or "", "exchange": info.get("exchange") or "",
        "title": title, "summary": summary, "who": info["name"], "role": info["role"],
        "value": buy["value"], "shares": buy["shares"], "price": buy["avg_price"], "drop_text": drop_text,
        "company_desc": prof.get("industry") or "", "filed": info["filed"], "timeline": timeline,
        "scenarios": first.get("scenarios"),
        "market": dict(market, when=f"{today:%b %d}") if market else None})
    nd = (first["date"] - today).days
    content = (f"🔎 **CASE FILE #{n:03d} · ${t}** · {info['role_short']} buys {money(buy['value'])}, "
               f"{nd} day{'s' if nd != 1 else ''} before {first['short']} · [SEC filing](<{info['filing_url']}>)"
               + (f" · [Live odds](<{market['url']}>)" if market else ""))
    cards.post_card(webhook, content, card)
    cases.append({"number": n, "ticker": t, "opened": today.isoformat(), "price": round(buy["avg_price"], 4),
                  "event_date": first["date"].isoformat(), "event": first["title"], "status": "open",
                  "name": info["name"]})
    log(f"  CASE FILE #{n:03d} {t}: {first['title']} {first['date']}")
    return True


def update_cases(webhook, state):
    """Post a result for every open case whose first event has passed."""
    if not webhook:
        return
    from chart import fetch_prices
    today = date.today()
    for c in state.get("cases", []):
        if c.get("status") != "open" or date.fromisoformat(c["event_date"]) >= today:
            continue
        try:
            rows, _ = fetch_prices(c["ticker"], "6mo")
            spy, _ = fetch_prices("SPY", "6mo")
            if not rows:
                continue
            now = rows[-1][4]
            ret = now / c["price"] - 1
            opened = date.fromisoformat(c["opened"])
            base = next((r[4] for r in reversed(spy) if r[0] <= opened), None)
            spy_ret = spy[-1][4] / base - 1 if base else None
            card = cards.case_update_card({"number": c["number"], "ticker": c["ticker"],
                                           "event": f"{c['event']} ({date.fromisoformat(c['event_date']):%b %d})",
                                           "buy_price": c["price"], "now_price": now, "ret": ret, "spy": spy_ret,
                                           "days": (today - opened).days, "note": ""})
            cards.post_card(webhook, f"✅ **CASE FILE #{c['number']:03d} · ${c['ticker']} · UPDATE** · "
                                     f"{ret * 100:+.1f}% since the insider's buy", card)
            c.update(status="closed", closed=today.isoformat(), ret=round(ret, 4))
            log(f"  case #{c['number']} closed {ret * 100:+.1f}%")
        except Exception as e:
            log(f"  case update {c['ticker']}: {e}")
