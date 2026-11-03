"""
#signals post: one strong insider buy, explained from every angle. Free data only, no AI.

The idea: insiders know things we don't. We can't know why they bought, but we can
collect what is about to happen and what the market is worried about, and make a
clear, sourced best guess.

Facts collected (each best-effort, with a link):
  company    what it does, sales, growth, rank by sales among US-listed peers (SEC XBRL)
  setup      big one-day drops in the last 60 days and which SEC filing came with them,
             distance from the 52-week high
  results    sentences from the latest earnings press release about records and outlook
  valuation  forward P/E, analyst growth estimates, analyst price target (Yahoo)
  washington new federal rules for the company's industry (Federal Register),
             lobbying, federal contracts
  catalysts  earnings date, FDA dates, deals expected to close, rule effective dates
  insider    their earlier buys and how they did, size vs their pay, other insiders selling
  risks      debt, cash burn, insider selling, short interest, a stock already up a lot

Then simple rules turn the facts into:
  TL;DR · our best guess why · what to watch · thesis breaks if · risks

Manual test: GitHub Actions -> "Signal preview" (TICKER, optional NAME).
"""

import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html import unescape

import requests

import deepdive as dd
from chart import fetch_prices
from score import close_on, sec_get

FR_URL = "https://www.federalregister.gov/api/v1/documents.json"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/CY{year}.json"
PEERS_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC={sic}&type=10-K"
             "&dateb=&owner=include&count=100&start={start}&output=atom")
REVENUE_CONCEPTS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"]
YAHOO_MODULES = ("assetProfile,financialData,defaultKeyStatistics,earningsTrend,"
                 "upgradeDowngradeHistory,insiderTransactions,calendarEvents")
MONTH_RE = "|".join(dd.MONTHS)

# extra Federal Register search phrases for industries whose SEC name is too vague
INDUSTRY_TERMS = {
    3672: ["printed circuit board"], 3674: ["semiconductor"], 2834: ["drug pricing"],
    2836: ["biologic"], 3841: ["medical device"], 2911: ["renewable fuel"],
    1311: ["oil and gas lease"], 4911: ["transmission"], 3711: ["fuel economy"],
    6022: ["bank capital"], 3812: ["missile defense"], 3721: ["aircraft"],
}


def log(msg):
    print(msg, flush=True)


pct = dd.pct
money = dd.money


def pct1(x):
    return f"{x * 100:+.1f}%"


def sentences(text):
    return re.split(r"(?<=[.!?])\s+(?=[A-Z\"“])", text)


# ---------------------------------------------------------------- Yahoo (one call, many facts)

_yahoo = {}


def yahoo(ticker):
    if ticker in _yahoo:
        return _yahoo[ticker]
    out = {}
    try:
        s = requests.Session()
        s.headers.update(dd.BROWSER)
        s.get("https://fc.yahoo.com", timeout=10)
        crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10).text.strip()
        r = s.get(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                  params={"modules": YAHOO_MODULES, "crumb": crumb}, timeout=20)
        out = (r.json().get("quoteSummary", {}).get("result") or [{}])[0] or {}
        log(f"  yahoo: HTTP {r.status_code}, modules {sorted(out)}")
    except Exception as e:
        log(f"  yahoo summary unavailable: {e}")
    _yahoo[ticker] = out
    return out


def raw(d, *keys):
    for k in keys:
        d = (d or {}).get(k)
    if isinstance(d, dict):
        return d.get("raw")
    return d


# ---------------------------------------------------------------- 🏢 company

def what_it_does(y, subs):
    summary = raw(y, "assetProfile", "longBusinessSummary") or ""
    if summary:
        first = sentences(summary)[0]
        first = re.sub(r",? together with its subsidiaries,?", "", first)
        first = re.sub(r",? through its subsidiaries,?", "", first)
        first = re.sub(r",? (in|across) the United States,.*$", ".", first)
        if len(first) > 230:
            first = first[:227].rsplit(" ", 1)[0] + "…"
        return first
    desc = (subs.get("sicDescription") or "").lower()
    return f"{dd.short_company(subs.get('name', ''))} is in {desc}." if desc else None


def peer_ciks(session, sic, limit=400):
    ciks = set()
    for start in range(0, limit, 100):
        try:
            text = sec_get(session, PEERS_URL.format(sic=sic, start=start)).text
        except Exception:
            break
        found = set(int(c) for c in re.findall(r"<cik>(\d+)</cik>", text, re.I)) or \
            set(int(c) for c in re.findall(r"CIK=(\d{6,10})", text))
        if not found - ciks:
            break
        ciks |= found
        if len(found) < 100:
            break
    return ciks


def sector_rank(session, cik, sic):
    """(rank, out_of, year) by yearly sales among SEC filers with the same industry code."""
    if not sic:
        return None
    peers = peer_ciks(session, sic)
    if int(cik) not in peers:
        peers.add(int(cik))
    year = date.today().year - 1
    for y in (year, year - 1):
        sales = {}
        for concept in REVENUE_CONCEPTS:
            try:
                data = sec_get(session, FRAME_URL.format(concept=concept, year=y)).json()
            except Exception:
                continue
            for row in data.get("data", []):
                c = int(row.get("cik") or 0)
                if c in peers and row.get("val"):
                    sales[c] = max(sales.get(c, 0), float(row["val"]))
        if int(cik) in sales and len(sales) >= 3:
            ranked = sorted(sales, key=sales.get, reverse=True)
            return ranked.index(int(cik)) + 1, len(ranked), y
    return None


def company_lines(session, buy, subs, y):
    out = []
    what = what_it_does(y, subs)
    if what:
        out.append(what)
    rev = raw(y, "financialData", "totalRevenue")
    growth = raw(y, "financialData", "revenueGrowth")
    bits = []
    if rev:
        bits.append(f"Sales last 12 months: **{money(rev)}**")
    if growth is not None:
        bits.append(f"growing **{pct(growth)}** a year")
    if buy.get("market_cap"):
        bits.append(f"market value {money(buy['market_cap'])}")
    try:
        rank = sector_rank(session, buy["issuer_cik"], int(subs.get("sic") or 0))
    except Exception as e:
        log(f"  sector rank unavailable: {e}")
        rank = None
    if rank and rank[1] >= 3:
        industry = (subs.get("sicDescription") or "its industry").lower()
        bits.append(f"**#{rank[0]} of {rank[1]}** US-listed companies in {industry} by sales ({rank[2]})")
    if bits:
        out.append(" · ".join(bits) + ".")
    return out, rank


# ---------------------------------------------------------------- 📉 setup: what just happened

def filings_by_day(subs):
    rec = (subs.get("filings") or {}).get("recent") or {}
    out = {}
    for form, filed, items, acc, doc in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("items", []),
                                           rec.get("accessionNumber", []), rec.get("primaryDocument", [])):
        out.setdefault(filed, []).append({"form": form, "items": items or "", "acc": acc, "doc": doc})
    return out


def big_moves(rows, days=60, limit=-0.08):
    """One-day closes down 8%+ in the last `days` days: [(date, change)], biggest first."""
    since = rows[-1][0] - timedelta(days=days)
    out = []
    for prev, cur in zip(rows, rows[1:]):
        if cur[0] >= since:
            ch = cur[4] / prev[4] - 1
            if ch <= limit:
                out.append((cur[0], ch))
    return sorted(out, key=lambda x: x[1])[:2]


def explain_move(day, by_day):
    """What the company filed with the SEC on that day or the day before."""
    for d in (day, day - timedelta(days=1), day - timedelta(days=3)):
        for f in by_day.get(d.isoformat(), []):
            if f["form"] == "8-K":
                words = [dd.EIGHT_K[i] for i in f["items"].split(",") if i.strip() in dd.EIGHT_K]
                if "2.02" in f["items"]:
                    return "right after its earnings report"
                if words:
                    return f"after it {words[0]}"
            if f["form"] in ("10-Q", "10-K"):
                return f"after its {f['form']} report"
    return None


def setup(rows, subs, buy):
    """Facts + a setup type for the thesis: 'crash' | 'downtrend' | 'strength' | None."""
    facts, kind, move = [], None, None
    if not rows or len(rows) < 30:
        return facts, kind, move
    by_day = filings_by_day(subs)
    moves = big_moves(rows)
    if moves:
        day, ch = moves[0]
        why = explain_move(day, by_day)
        move = {"day": day, "ch": ch, "why": why}
        facts.append(f"The stock fell **{abs(ch) * 100:.0f}% in one day** on {day:%b %d}" + (f", {why}." if why else "."))
        kind = "crash"
    year = [r for r in rows if r[0] >= rows[-1][0] - timedelta(days=365)]
    high = max(r[2] for r in year)
    low = min(r[3] for r in year)
    drop = 1 - rows[-1][4] / high
    if drop >= 0.15:
        facts.append(f"It is **{drop * 100:.0f}%** below its 52-week high (${high:,.2f}).")
        kind = kind or "downtrend"
    elif rows[-1][4] / low - 1 > 1.0:
        facts.append(f"It is up **{pct(rows[-1][4] / low - 1)}** from its 52-week low: they are buying strength, "
                     f"not a dip.")
        kind = kind or "strength"
    return facts, kind, move


# ---------------------------------------------------------------- 📊 latest results (earnings press release)

def exhibit_991(session, cik, acc):
    idx = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm"
    html = sec_get(session, idx).text
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        if re.search(r"EX-99\.?1\b", row):
            m = re.search(r'href="([^"]+)"', row)
            if m:
                return "https://www.sec.gov" + m.group(1) if m.group(1).startswith("/") else m.group(1)
    return None


def press_release(session, cik, subs, today):
    """(url, text) of the latest earnings press release (8-K item 2.02, last 120 days)."""
    rec = (subs.get("filings") or {}).get("recent") or {}
    for form, filed, items, acc in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("items", []),
                                       rec.get("accessionNumber", [])):
        if form == "8-K" and "2.02" in (items or "") and filed >= (today - timedelta(days=120)).isoformat():
            url = exhibit_991(session, cik, acc)
            if url:
                html = sec_get(session, url).text
                text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))
                return url, filed, text
    return None


def results_facts(pr):
    """Record + outlook sentences, short and with numbers."""
    if not pr:
        return [], []
    url, filed, text = pr
    clean = text[:80000].replace("•", ". ")
    sents = [s.strip() for s in sentences(clean[:60000]) if 40 < len(s) < 320 and re.search(r"\$|\d%", s)
             and "expected to close" not in s]
    picks, deals = [], []
    for s in sents:
        low = s.lower()
        if re.search(r"\brecord\b", low) and len(picks) < 1:
            picks.append(s)
        elif re.search(r"\b(outlook|guidance|expects?|anticipates?)\b", low) and "forward-looking" not in low \
                and len([p for p in picks if p]) < 3 and s not in picks:
            picks.append(s)
    for s in sentences(text[:80000].replace("•", ". ")):
        m = re.search(r"expected to (?:close|be completed) (?:during|in|by) (the [a-z]+ quarter of 20\d\d|[A-Z][a-z]+ 20\d\d|20\d\d)", s)
        if m and m.group(1) not in [d for d in deals]:
            deals.append(m.group(1))
    return picks[:3], deals[:2]


# ---------------------------------------------------------------- 💵 valuation & analysts

def valuation_facts(y, price):
    out = []
    fpe = raw(y, "defaultKeyStatistics", "forwardPE")
    trend = {t.get("period"): t for t in (raw(y, "earningsTrend", "trend") or [])}
    nxt = trend.get("+1y") or {}
    eps_g = raw(nxt, "growth")
    rev_g = raw(nxt, "revenueEstimate", "growth")
    bits = []
    if fpe and 0 < fpe < 200:
        bits.append(f"**{fpe:.0f}×** next year's expected earnings")
    if rev_g is not None:
        bits.append(f"analysts expect sales **{pct(rev_g)}** next year")
    if eps_g is not None:
        bits.append(f"earnings **{pct(eps_g)}**")
    if bits:
        out.append("Price: " + ", ".join(bits) + ".")
    target = raw(y, "financialData", "targetMeanPrice")
    n = raw(y, "financialData", "numberOfAnalystOpinions")
    if target and price and n:
        out.append(f"{n} analysts' average price target **${target:,.0f}** ({pct(target / price - 1)} from here).")
    ups = raw(y, "upgradeDowngradeHistory", "history") or []
    recent = [u for u in ups if u.get("epochGradeDate", 0) >= time.time() - 30 * 86400]
    if recent:
        up = sum(1 for u in recent if u.get("action") == "up")
        down = sum(1 for u in recent if u.get("action") == "down")
        if up or down:
            out.append(f"Analysts in the last 30 days: {up} upgrade(s), {down} downgrade(s).")
    return out, {"fpe": fpe, "eps_g": eps_g, "rev_g": rev_g, "target": target}


# ---------------------------------------------------------------- 🏛️ Washington: new rules for the industry

def industry_terms(subs):
    sic = int(subs.get("sic") or 0)
    terms = list(INDUSTRY_TERMS.get(sic, []))
    desc = (subs.get("sicDescription") or "").lower()
    desc = re.sub(r"^services-|, ?nec$|\(.*?\)", "", desc).strip()
    desc = desc.rstrip("s")
    if desc and len(desc.split()) <= 3 and desc not in terms:
        terms.append(desc)
    return terms[:2]


def future_dates(text, today):
    out = []
    for m in re.finditer(rf"({MONTH_RE}) (\d{{1,2}}), (20\d\d)", text or ""):
        try:
            d = datetime.strptime(" ".join(m.groups()), "%B %d %Y").date()
        except ValueError:
            continue
        if d > today:
            out.append(d)
    return sorted(set(out))


KEY_DATE_WORDS = r"(effective|take effect|takes effect|beginning|begins|no later than|prohibit|compliance date|deadline)"


def key_date(doc, today):
    """A future date the rule itself names as effective / compliance / prohibition date."""
    if doc.get("effective_on") and doc["effective_on"] > today.isoformat():
        return date.fromisoformat(doc["effective_on"])
    texts = [doc.get("abstract") or ""]
    if doc.get("raw_text_url"):
        try:
            texts.append(requests.get(doc["raw_text_url"], timeout=25).text[:400000])
        except Exception:
            pass
    found = []
    for text in texts:
        text = re.sub(r"\s+", " ", text)
        for m in re.finditer(KEY_DATE_WORDS, text, re.I):
            window = text[max(0, m.start() - 160): m.end() + 160]
            found += future_dates(window, today)
        if found:
            break
    return min(found) if found else None


def federal_rules(subs, company, today):
    """[(line, key date or None)] new rules / proposed rules (last 9 months) about the industry or company."""
    out, seen = [], set()
    queries = [(t, "title") for t in industry_terms(subs)] + [(company, "any")]
    for term, where in queries:
        params = [("conditions[term]", f'"{term}"'), ("conditions[type][]", "RULE"),
                  ("conditions[type][]", "PRORULE"),
                  ("conditions[publication_date][gte]", (today - timedelta(days=270)).isoformat()),
                  ("per_page", "20"), ("order", "relevance")]
        for f in ("title", "publication_date", "effective_on", "html_url", "abstract", "agencies", "type",
                  "raw_text_url"):
            params.append(("fields[]", f))
        try:
            r = requests.get(FR_URL, params=params, timeout=25)
            docs = r.json().get("results") or []
            log(f"  Federal Register {term}: {len(docs)} docs")
        except Exception as e:
            log(f"  Federal Register unavailable: {e}")
            continue
        for d in docs:
            title = d.get("title") or ""
            if d["html_url"] in seen:
                continue
            # industry terms must be what the rule is ABOUT (in the title), not a word in the fine print
            if where == "title" and term.lower() not in title.lower():
                continue
            if where == "any" and term.lower() not in (title + " " + (d.get("abstract") or "")).lower():
                continue
            seen.add(d["html_url"])
            agency = ((d.get("agencies") or [{}])[0].get("name") or "").replace("Department", "Dept.")
            when = key_date(d, today)
            kind = "Final rule" if d.get("type") == "Rule" else "Proposed rule"
            line = f"[{title[:110]}]({d['html_url']}) · {kind}, {agency or 'federal agency'}, " \
                   f"published {d['publication_date']}"
            if when:
                line += f" · **key date {when:%b %d, %Y}**"
            out.append((line, when))
            if len(out) >= 3:
                return out
    return out


# ---------------------------------------------------------------- 👤 insiders

def sold_total(y):
    cutoff = time.time() - 365 * 86400
    return sum(raw(t, "value") or 0 for t in (raw(y, "insiderTransactions", "transactions") or [])
               if (t.get("transactionText") or "").lower().startswith("sale") and (raw(t, "startDate") or 0) >= cutoff)


def insider_sales(y, buyer_name):
    rows = raw(y, "insiderTransactions", "transactions") or []
    cutoff = time.time() - 365 * 86400
    sold, n, last = 0.0, 0, None
    for t in rows:
        text = (t.get("transactionText") or "").lower()
        if not text.startswith("sale") or raw(t, "startDate") is None or raw(t, "startDate") < cutoff:
            continue
        v = raw(t, "value") or 0
        sold += v
        n += 1
        d = datetime.fromtimestamp(raw(t, "startDate"), timezone.utc).date()
        if not last or d > last[0]:
            last = (d, t.get("filerName", "").title(), v)
    if not n:
        return ["No insider sold shares in the last 12 months."]
    line = f"Insiders sold **{money(sold)}** in the last 12 months ({n} sales)"
    if last:
        line += f", latest {last[1]} on {last[0]:%b %d}"
    return [line + "."]


# ---------------------------------------------------------------- the rules: facts -> thesis

def thesis(buy, kind, move, val, rules, results, rank, cluster):
    """One-sentence best guess + TL;DR."""
    who = "Several insiders are" if cluster else "The insider is"
    if kind == "crash" and move:
        cause = f" {move['why']}" if move.get("why") else ""
        base = (f"The market dumped the stock{cause} ({pct(move['ch'])} on {move['day']:%b %d}). "
                f"{who} buying right after: they likely think the sell-off is overdone.")
        tl = f"Stock {pct(move['ch'])} in a day{cause}, and {who.lower().replace(' is', ' buys').replace(' are', ' buy')} the dip."
    elif kind == "downtrend":
        base = f"{who} buying a stock that has been falling for months: they likely think the bottom is close."
        tl = "Beaten-down stock, and an insider buys near the lows."
    elif kind == "strength":
        base = f"{who} buying even after a big run-up: they likely expect growth the market hasn't priced in yet."
        tl = "An insider keeps buying after a big run-up."
    else:
        base = f"{who} putting their own money in: they likely expect the business to do better than the market thinks."
        tl = "An insider puts their own money in."
    reasons = []
    if val.get("rev_g") and val["rev_g"] >= 0.15:
        reasons.append(f"sales are expected to grow {val['rev_g'] * 100:.0f}% next year")
    if val.get("fpe") and val["fpe"] < 15:
        reasons.append(f"the stock costs only ~{val['fpe']:.0f}× next year's earnings")
    dated = [w for _, w in rules if w]
    if dated:
        reasons.append(f"a new federal rule for its industry has a key date on {min(dated):%b %d, %Y}")
    if rank and rank[0] <= 3:
        reasons.append("it is the biggest company in its industry by sales" if rank[0] == 1
                       else f"it is #{rank[0]} in its industry by sales")
    if reasons:
        r = reasons[:3]
        joined = r[0] if len(r) == 1 else ", ".join(r[:-1]) + " and " + r[-1]
        base += f" Why they might be right: {joined}."
        tl += f" {reasons[0][0].upper() + reasons[0][1:]}."
    return base, tl


def risks(buy, y, val, kind, rows, sold=0.0):
    out = []
    if sold and sold >= 10 * buy["value"]:
        out.append(f"Insiders sold **{money(sold)}** over the last year, far more than this {money(buy['value'])} buy.")
    fd = (y or {}).get("financialData") or {}
    debt, ebitda = raw(fd, "totalDebt"), raw(fd, "ebitda")
    if debt and ebitda and ebitda > 0 and debt / ebitda >= 2.5:
        out.append(f"Debt is **{debt / ebitda:.1f}×** yearly EBITDA.")
    fcf = raw(fd, "freeCashflow")
    if fcf is not None and fcf < 0:
        out.append(f"Free cash flow is negative ({money(fcf)}).")
    short = buy.get("short") or {}
    if short.get("pct") and short["pct"] >= 0.08:
        out.append(f"{short['pct'] * 100:.0f}% of shares are sold short: many traders bet against it.")
    if kind == "strength":
        out.append("The stock has already run up a lot; any disappointment can hit hard.")
    if val.get("eps_g") is not None and val["eps_g"] < 0:
        out.append(f"Analysts expect earnings to **fall** {pct(val['eps_g'])} next year.")
    if not out:
        out.append("No obvious red flags in the numbers, but one buy is still one person's opinion.")
    return out


def breaks_if(rows, catalysts_):
    out = ["The insider sells."]
    if rows:
        low = min(r[3] for r in rows[-60:])
        out.append(f"The stock closes below its recent low (${low:,.2f}).")
    if catalysts_:
        out.append("The next earnings report disappoints again.")
    return out


# ---------------------------------------------------------------- build the post

def bullets(lines):
    return "\n".join(f"• {x}" for x in lines)


def build(session, buy, rows, filing_url=None, in_cluster=False):
    """Return two embeds: the summary (read in 10 seconds) and the full analysis."""
    today = datetime.now(timezone.utc).date()
    issuer = buy.get("issuer_cik")
    try:
        subs = dd.submissions(session, issuer)
    except Exception:
        subs = {}
    y = yahoo(buy["ticker"])
    company = dd.short_company(buy["company"])
    price = rows[-1][4] if rows else None

    def safe(name, fn, default):
        try:
            return fn()
        except Exception as e:
            log(f"  signal section '{name}' failed: {e}")
            return default

    comp, rank = safe("company", lambda: company_lines(session, buy, subs, y), ([], None))
    setup_facts, kind, move = safe("setup", lambda: setup(rows, subs, buy), ([], None, None))
    pr = safe("press release", lambda: press_release(session, issuer, subs, today), None)
    res, deals = safe("results", lambda: results_facts(pr), ([], []))
    val_lines, val = safe("valuation", lambda: valuation_facts(y, price), ([], {}))
    rules = safe("federal register", lambda: federal_rules(subs, company, today), [])
    wash = safe("washington", lambda: dd.lobbying(company, today) + dd.contracts(company, today), [])
    try:
        sprees = dd.group_sprees(dd.owner_buys(session, buy["owner"]["cik"], issuer)) if buy["owner"].get("cik") else []
        long_rows = fetch_prices(buy["ticker"], "5y")[0]
        spy = fetch_prices("SPY", "5y")[0]
    except Exception:
        sprees, long_rows, spy = [], rows or [], []
    skin = safe("skin", lambda: dd.skin_in_the_game(session, buy, issuer, sprees, long_rows), [])
    record = safe("win rate", lambda: dd.win_rate(buy, sprees, long_rows, spy)[0], [])
    sales = safe("insider sales", lambda: insider_sales(y, buy["owner"]["name"]), [])
    cats = safe("catalysts", lambda: dd.catalysts(session, buy, issuer, subs, dd.sector_of(int(subs.get("sic") or 0)),
                                                   today), [])
    if deals:
        cats.append(f"Pending acquisition(s) expected to close in {deals[0]} (company's earnings release).")
    cats += [f"Rule key date **{w:%b %d, %Y}**" for _, w in rules if w]

    guess, tldr = thesis(buy, kind, move, val, rules, res, rank, in_cluster)

    # --- summary embed
    role = buy.get("owner_label") or buy["owner"]["name"]
    head = [f"**TL;DR:** {tldr}", ""]
    if price and buy.get("avg_price"):
        head.append(f"⏱️ Insider paid **${buy['avg_price']:,.2f}** · now **${price:,.2f}** "
                    f"({pct1(price / buy['avg_price'] - 1)})"
                    + (f" · [Form 4 ↗]({filing_url})" if filing_url else ""))
    if record:
        head.append(f"📈 {record[0]}")
    head.append("🎯 **Horizon:** 3–6 months (where insider buys historically worked best)")
    summary = {
        "title": f"🟢 ${buy['ticker']} · {role} bought {money(buy['value'])}"
                 + (f" · Score {buy['score']}/10" if buy.get("score") else "")
                 + (" · 🔥 cluster" if in_cluster else ""),
        "url": filing_url,
        "description": "\n".join(head)[:4000],
        "color": 0x23A55A,
    }

    # --- full analysis embed
    parts = []
    if comp:
        parts.append("**🏢 THE COMPANY**\n" + "\n".join(comp))
    what = setup_facts + ([f"From the latest earnings release ({pr[1]}): “{res[0]}”"] if res else [])
    if what:
        parts.append("**📉 WHAT JUST HAPPENED**\n" + bullets(what))
    parts.append("**🧠 OUR BEST GUESS WHY** *(nobody knows for sure)*\n" + guess)
    support = res[1:] + val_lines + [l for l, _ in rules] + wash
    if support:
        parts.append("**🔎 THE FACTS BEHIND IT**\n" + bullets(support[:7]))
    if skin or record[1:] or sales:
        parts.append("**👤 THE INSIDER**\n" + bullets(skin + record[1:] + sales))
    if cats:
        parts.append("**📅 DATES TO WATCH**\n" + bullets(cats[:5]))
    parts.append("**⚠️ RISKS**\n" + bullets(risks(buy, y, val, kind, rows, sold_total(y))))
    parts.append("**❌ THESIS BREAKS IF**\n" + bullets(breaks_if(rows, cats)))
    src = ["SEC filings"] + (["Federal Register"] if rules else []) + (["Senate LDA / USAspending"] if wash else []) \
        + (["Yahoo Finance"] if y else [])
    detail = {
        "description": "\n\n".join(parts)[:4000],
        "color": 0x23A55A,
        "footer": {"text": "📡 Insider Radar · " + ", ".join(src) + " · Our guess, not a fact · Not financial advice"},
    }
    if pr:
        detail["description"] = (detail["description"] + f"\n\n📄 [Earnings release]({pr[0]})")[:4000]
    return [summary, detail]


def wanted(buy, in_cluster=False):
    return dd.wanted(buy, in_cluster)


# ---------------------------------------------------------------- manual run

def main():
    import json
    from bot import add_score, fetch_form4_xml, load_state, nice_name, parse_form4, readable_filing_url, role_text
    from company import company_info
    from preview import latest_buy_filings
    from repost import issuer_cik

    ticker = (os.environ.get("TICKER") or "").strip().upper().lstrip("$")
    target = (os.environ.get("TARGET") or "test").strip().lower()
    webhook = (os.environ.get("WEBHOOK_CLUSTER") if target == "public" else os.environ.get("WEBHOOK_PROOF")) or ""
    if not ticker:
        sys.exit("TICKER is required.")
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
    embeds = build(session, buy, rows, readable_filing_url(session, base, acc))
    for e in embeds:
        log(json.dumps(e, indent=1, ensure_ascii=False))
    if webhook.strip():
        r = requests.post(webhook.strip(), json={"username": "Insider Radar", "embeds": embeds}, timeout=30)
        log(f"Discord: HTTP {r.status_code} {r.text[:200]}")


if __name__ == "__main__":
    main()
