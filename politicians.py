"""
Politician trades for Discord (#politician-trades).

Members of Congress must report stock trades (STOCK Act "Periodic Transaction
Reports") within 45 days. Sources, both public and free:

  House   disclosures-clerk.house.gov - a yearly ZIP with an XML index of all filings,
          each PTR is a PDF. Electronic PDFs have text we can read; scanned paper
          filings don't, so for those we post a short note with the link.
  Senate  efdsearch.senate.gov - electronic PTRs are HTML tables.

Every alert shows how many days after the trade it was reported, the amount
range (Congress only reports ranges) and how the stock moved since the trade.

Environment:
  WEBHOOK_POLITICIANS   Discord webhook for #politician-trades
  POL_DEBUG=1           print what would be posted, post nothing, don't save state
"""

import io
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import requests

from chart import fetch_prices

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pol_state.json")
HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
SENATE_HOME = "https://efdsearch.senate.gov/search/home/"
SENATE_SEARCH = "https://efdsearch.senate.gov/search/"
SENATE_DATA = "https://efdsearch.senate.gov/search/report/data/"
SENATE_BASE = "https://efdsearch.senate.gov"
LEGISLATORS = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
COMMITTEES = "https://unitedstates.github.io/congress-legislators/committees-current.json"
MEMBERSHIP = "https://unitedstates.github.io/congress-legislators/committee-membership-current.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
LOOKBACK_DAYS = 10           # look at filings from the last N days each run
MAX_POSTS = 12               # safety cap per run
BOOTSTRAP_POSTS = 3
MAX_LINES = 10               # trades listed per alert
DEBUG = os.environ.get("POL_DEBUG") == "1"

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Which industries each committee oversees, as SEC SIC code ranges.
# (keyword found in the committee name, short sector name, [(first SIC, last SIC), ...])
DEFENSE = [(3480, 3489), (3720, 3729), (3730, 3732), (3760, 3769), (3812, 3812)]
FINANCE = [(6000, 6799)]
OIL_MINING = [(1000, 1499), (2911, 2911), (2990, 2999)]
UTILITIES = [(4900, 4991)]
HEALTH = [(2833, 2836), (3841, 3851), (5047, 5047), (5122, 5122), (6324, 6324), (8000, 8099), (8731, 8731)]
TELECOM = [(4800, 4899)]
TECH = [(3570, 3579), (3670, 3679), (7370, 7379)]
FARM_FOOD = [(100, 999), (2000, 2099), (2870, 2879), (3523, 3523)]
TRANSPORT = [(3710, 3716), (3720, 3729), (3740, 3743), (4000, 4799)]
COMMITTEE_SECTORS = [
    ("Armed Services", "defense", DEFENSE),
    ("Intelligence", "defense / tech", DEFENSE + TECH),
    ("Homeland Security", "defense / tech", DEFENSE + TECH),
    ("Financial Services", "finance", FINANCE),
    ("Banking", "finance", FINANCE),
    ("Energy and Commerce", "energy / health / telecom", OIL_MINING + UTILITIES + HEALTH + TELECOM),
    ("Energy and Natural Resources", "energy", OIL_MINING + UTILITIES),
    ("Natural Resources", "oil, gas & mining", OIL_MINING),
    ("Health, Education", "healthcare", HEALTH),
    ("Veterans", "healthcare", [(8000, 8099)]),
    ("Agriculture", "agriculture & food", FARM_FOOD),
    ("Transportation", "transportation", TRANSPORT),
    ("Commerce, Science", "transport / telecom / tech", TRANSPORT + TELECOM + TECH),
    ("Science, Space", "tech & space", TECH + [(3760, 3769)]),
]

TYPE_WORD = {"P": "Bought", "S": "Sold", "S (partial)": "Sold part", "E": "Exchanged"}


def log(msg):
    print(msg, flush=True)


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    state["seen"] = state["seen"][-4000:]
    state["trades"] = state.get("trades", [])[-3000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def parse_us_date(s):
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- party / state

def load_legislators(session):
    """{(last name lower, state): (full name, party letter, chamber)} for current members."""
    out = {}
    try:
        for p in session.get(LEGISLATORS, timeout=30).json():
            term = p["terms"][-1]
            name = p["name"]
            last = name["last"].lower()
            party = {"Democrat": "D", "Republican": "R"}.get(term.get("party"), "I")
            full = name.get("official_full") or f"{name['first']} {name['last']}"
            bio = p.get("id", {}).get("bioguide")
            out[(last, term["state"])] = (full, party, term["type"], bio)
            out.setdefault((last, None), (full, party, term["type"], bio))
    except Exception as e:
        log(f"legislator list unavailable: {e}")
    return out


def load_committees(session):
    """{bioguide id: ['Armed Services', ...]} - full committees only, chairs/ranking members first."""
    out = {}
    try:
        names = {}
        for c in session.get(COMMITTEES, timeout=30).json():
            n = c.get("name", "")
            for prefix in ("House Committee on the ", "House Committee on ", "Senate Committee on the ",
                           "Senate Committee on ", "Committee on the ", "Committee on ", "House ", "Senate "):
                if n.startswith(prefix):
                    n = n[len(prefix):]
                    break
            names[c.get("thomas_id")] = n
        for cid, members in session.get(MEMBERSHIP, timeout=30).json().items():
            if len(cid) != 4 or cid not in names:          # skip subcommittees like HSAS28
                continue
            for m in members:
                title = m.get("title") or ""
                label = names[cid] + (f" ({title})" if title in ("Chair", "Chairman", "Ranking Member") else "")
                out.setdefault(m.get("bioguide"), []).append((0 if title else 1, label))
        out = {k: [x[1] for x in sorted(v)] for k, v in out.items()}
    except Exception as e:
        log(f"committee list unavailable: {e}")
    return out


def member_id(legis, last, state):
    hit = legis.get((last.lower(), state)) or legis.get((last.lower(), None))
    return hit[3] if hit else None


def who(legis, first, last, state):
    """'Nancy Pelosi (D-CA)' style label."""
    hit = legis.get((last.lower(), state)) or legis.get((last.lower(), None))
    if hit:
        full, party = hit[0], hit[1]
        return f"{full} ({party}-{state})" if state else f"{full} ({party})"
    return f"{first} {last}".strip() + (f" ({state})" if state else "")


# ---------------------------------------------------------------- House

AMOUNT_RE = r"(\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+|\$[\d,]+\s*-)"
HOUSE_ROW = re.compile(
    r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\[([A-Z]{2})\]\s*(P|S\s*\(partial\)|S|E)\s+"
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+" + AMOUNT_RE)


def house_filings(session, since):
    """Recent House PTR filings: [{'id', 'first', 'last', 'state', 'filed', 'url', 'doc'}]."""
    out = []
    years = {since.year, date.today().year}
    for year in sorted(years):
        r = session.get(HOUSE_ZIP.format(year=year), timeout=60)
        if r.status_code != 200:
            log(f"House index {year}: HTTP {r.status_code}")
            continue
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(zf.read(xml_name))
        for m in root.iter("Member"):
            if (m.findtext("FilingType") or "").strip() != "P":
                continue
            filed = parse_us_date(m.findtext("FilingDate") or "")
            doc = (m.findtext("DocID") or "").strip()
            if not filed or filed < since or not doc:
                continue
            st = (m.findtext("StateDst") or "").strip()[:2]
            out.append({"id": f"H{doc}", "chamber": "House", "doc": doc, "filed": filed,
                        "first": (m.findtext("First") or "").strip(), "last": (m.findtext("Last") or "").strip(),
                        "state": st, "url": HOUSE_PDF.format(year=year, doc=doc)})
    return sorted(out, key=lambda f: f["filed"])


def pdf_text(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def house_trades(session, filing):
    """[{'ticker','type','date','amount','asset'}] or None if the PDF has no text (paper filing)."""
    r = session.get(filing["url"], timeout=60)
    r.raise_for_status()
    text = pdf_text(r.content)
    flat = re.sub(r"\s+", " ", text.replace("\x00", ""))
    if DEBUG:
        log(f"--- text of {filing['url']} ---\n{flat[:1500]}\n---")
    if len(flat) < 200 or "Transaction" not in flat and "Asset" not in flat:
        return None
    trades = []
    for m in HOUSE_ROW.finditer(flat):
        ticker, kind, typ, tdate, _ndate, amount = m.groups()
        if kind not in ("ST", "OP", "EF", "AB"):       # stocks, options, ETFs, ADRs
            continue
        typ = re.sub(r"\s+", " ", typ)
        trades.append({"ticker": ticker, "type": typ, "date": parse_us_date(tdate),
                       "amount": re.sub(r"\s+", " ", amount).replace("Over ", "Over "),
                       "option": kind == "OP"})
    return trades


# ---------------------------------------------------------------- Senate

def senate_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    page = s.get(SENATE_HOME, timeout=30)
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
    if not token:
        raise RuntimeError("Senate site: no CSRF token")
    s.post(SENATE_HOME, data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token.group(1)},
           headers={"Referer": SENATE_HOME}, timeout=30)
    return s


def senate_filings(since):
    s = senate_session()
    csrf = s.cookies.get("csrftoken") or s.cookies.get("csrf")
    out, start = [], 0
    while True:
        r = s.post(SENATE_DATA, timeout=30, headers={"Referer": SENATE_SEARCH, "X-CSRFToken": csrf or ""},
                   data={"start": str(start), "length": "100", "report_types": "[11]", "filer_types": "[]",
                         "submitted_start_date": since.strftime("%m/%d/%Y") + " 00:00:00",
                         "submitted_end_date": "", "candidate_state": "", "senator_state": "",
                         "office_id": "", "first_name": "", "last_name": "", "csrfmiddlewaretoken": csrf or ""})
        r.raise_for_status()
        rows = r.json().get("data", [])
        for first, last, office, link_html, filed in rows:
            link = re.search(r'href="([^"]+)"', link_html)
            if not link:
                continue
            path = link.group(1)
            st = re.search(r"\(([A-Z]{2})\)", office or "")
            out.append({"id": "S" + path.rstrip("/").split("/")[-1], "chamber": "Senate",
                        "first": first.strip().title(), "last": last.strip().split(",")[0].title(),
                        "state": st.group(1) if st else "", "filed": parse_us_date(filed),
                        "url": SENATE_BASE + path, "paper": "/paper/" in path})
        if len(rows) < 100:
            break
        start += 100
    return s, sorted(out, key=lambda f: f["filed"] or date.min)


def senate_trades(s, filing):
    if filing.get("paper"):
        return None
    html = s.get(filing["url"], timeout=30).text
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    trades = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1) if body else "", re.S):
        c = [re.sub(r"<[^>]+>", " ", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        c = [re.sub(r"\s+", " ", x) for x in c]
        # # | date | owner | ticker | asset | asset type | type | amount | comment
        if len(c) < 8:
            continue
        ticker = c[3].strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", ticker):
            continue
        typ = {"Purchase": "P", "Sale (Full)": "S", "Sale (Partial)": "S (partial)",
               "Sale": "S", "Exchange": "E"}.get(c[6], c[6])
        trades.append({"ticker": ticker, "type": typ, "date": parse_us_date(c[1]), "amount": c[7],
                       "option": "Option" in c[5]})
    if DEBUG:
        log(f"--- {filing['url']}: {len(trades)} trades")
    return trades


# ---------------------------------------------------------------- message

def since_trade(ticker, day, cache):
    """% move from the close on the trade day to the last close (None if unknown)."""
    if not day:
        return None
    if ticker not in cache:
        try:
            cache[ticker] = fetch_prices(ticker, "1y")[0]
        except Exception:
            cache[ticker] = []
    rows = cache[ticker]
    if not rows:
        return None
    base = None
    for r in rows:
        if r[0] <= day:
            base = r[4]
        else:
            break
    return rows[-1][4] / base - 1 if base else None


def short_money(s):
    v = float(s.replace("$", "").replace(",", ""))
    v = round(v / 1000) * 1000 if v > 1000 else v       # $1,001 -> $1K, $15,000 -> $15K
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if v >= div:
            return f"${v / div:g}{unit}"
    return f"${v:,.0f}"


def compact_amount(text):
    """'$250,001 - $500,000' -> '$250K–$500K', 'Over $50,000,000' -> 'over $50M'."""
    nums = re.findall(r"\$[\d,]+", text or "")
    try:
        if text.lower().startswith("over") and nums:
            return "over " + short_money(nums[0])
        if len(nums) == 2:
            return f"{short_money(nums[0])}–{short_money(nums[1])}"
    except ValueError:
        pass
    return text


def amount_top(text):
    nums = [float(x.replace("$", "").replace(",", "")) for x in re.findall(r"\$[\d,]+", text or "")]
    return max(nums) if nums else 0


def merge_same(trades):
    """Same stock, same side, same day (often several accounts) -> one line with a count."""
    out, index = [], {}
    for t in trades:
        key = (t["ticker"], t["type"], t["date"], t["option"])
        if key in index:
            first = out[index[key]]
            first["count"] = first.get("count", 1) + 1
            if amount_top(t["amount"]) > amount_top(first["amount"]):
                first["amount"] = t["amount"]
        else:
            index[key] = len(out)
            out.append(dict(t))
    return out


def load_sic(state, tickers):
    """Fill state['sic'] = {ticker: [sic, description]} for tickers we haven't looked up yet."""
    cache = state.setdefault("sic", {})
    todo = [t for t in tickers if t not in cache]
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not todo or not ua:
        return cache
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    try:
        ciks = {row["ticker"].upper(): int(row["cik_str"]) for row in s.get(SEC_TICKERS, timeout=30).json().values()}
    except Exception as e:
        log(f"SEC ticker list unavailable: {e}")
        return cache
    for t in todo:
        cik = ciks.get(t) or ciks.get(t.replace(".", "-"))
        if not cik:
            cache[t] = [None, ""]
            continue
        try:
            time.sleep(0.15)
            d = s.get(SEC_SUBMISSIONS.format(cik=cik), timeout=30).json()
            cache[t] = [int(d.get("sic") or 0) or None, (d.get("sicDescription") or "").strip()]
        except Exception as e:
            log(f"  no SIC for {t}: {e}")
    return cache


def committee_match(sic, committees):
    """(committee name, sector) if this industry is overseen by one of the member's committees."""
    if not sic or not committees:
        return None
    for c in committees:
        name = re.sub(r"\s*\((Chair|Chairman|Ranking Member)\)$", "", c)
        for key, sector, ranges in COMMITTEE_SECTORS:
            if key in name and any(lo <= sic <= hi for lo, hi in ranges):
                return name, sector
    return None


def pct_text(x):
    v = round(x * 100)
    return "0%" if v == 0 else f"{v:+d}%"


def build_embed(filing, label, trades, prices, committees=None, sic=None):
    trades = merge_same(trades)
    sic = sic or {}
    for t in trades:
        t["match"] = committee_match((sic.get(t["ticker"]) or [None])[0], committees)
    # committee matches first, then purchases, then the biggest amounts
    trades = sorted(trades, key=lambda t: (t["match"] is None, t["type"] != "P", -amount_top(t["amount"])))
    n_buy = sum(t.get("count", 1) for t in trades if t["type"] == "P")
    n_sell = sum(t.get("count", 1) for t in trades if t["type"].startswith("S"))
    head = " · ".join(x for x in (f"**{n_buy} buy{'s' if n_buy != 1 else ''}**" if n_buy else "",
                                  f"**{n_sell} sale{'s' if n_sell != 1 else ''}**" if n_sell else "") if x)
    lines, buys = ([head] if head else []), 0
    if committees:
        lines.append("🪑 Committees: " + ", ".join(committees[:3]))
    if lines:
        lines.append("")
    for t in trades[:MAX_LINES]:
        word = TYPE_WORD.get(t["type"], t["type"])
        icon = "🟢" if t["type"] == "P" else "🔴" if t["type"].startswith("S") else "⚪"
        buys += t["type"] == "P"
        line = f"{icon} **{word} ${t['ticker']}**{' (options)' if t['option'] else ''}{f' ×{t["count"]}' if t.get('count') else ''} · {compact_amount(t['amount'])}"
        if t["date"]:
            line += f" · {t['date']:%b %d}"
        move = since_trade(t["ticker"], t["date"], prices)
        if move is not None:
            line += f" · since then **{pct_text(move)}**"
        if t["match"]:
            line += " ⚠️"
        lines.append(line)
    if len(trades) > MAX_LINES:
        lines.append(f"…and {len(trades) - MAX_LINES} more in the filing")

    matches = {}
    for t in trades:
        if t["match"]:
            matches.setdefault(t["match"], []).append(f"${t['ticker']}")
    note = ""
    for (comm, sector), tks in matches.items():
        tks = list(dict.fromkeys(tks))
        note += (f"\n\n⚠️ **Committee match:** {', '.join(tks[:4])} {'is' if len(tks) == 1 else 'are'} "
                 f"in **{sector}**, an area the **{comm}** Committee oversees.")

    dated = [t["date"] for t in trades if t["date"]]
    delay = ""
    if dated and filing["filed"]:
        days = (filing["filed"] - min(dated)).days
        delay = f"\n\n⏱ Reported **{days} days** after the {'first ' if len(set(dated)) > 1 else ''}trade."
        if days > 45:
            delay += " That's past the 45-day legal deadline."
    return {
        "title": f"🏛️ {label} · {filing['chamber']}" + (" · ⚠️ Committee match" if matches else ""),
        "url": filing["url"],
        "description": ("\n".join(lines) + note + delay
                        + "\n\nCongress only reports amount ranges, not exact sums."
                        + f"\n\n📄 [View the official filing]({filing['url']})"),
        "color": 0xE67E22 if matches else 0x3498DB if buys else 0x95A5A6,
        "footer": {"text": "📡 Insider Radar · Source: STOCK Act disclosures · Not financial advice"},
    }


def paper_embed(filing, label):
    return {
        "title": f"🏛️ {label} · {filing['chamber']}",
        "url": filing["url"],
        "description": ("Filed a stock trade report on paper (scanned), so the trades can't be read "
                        f"automatically.\n\n📄 [View the official filing]({filing['url']})"),
        "color": 0x95A5A6,
        "footer": {"text": "📡 Insider Radar · Source: STOCK Act disclosures · Not financial advice"},
    }


def post(webhook, embed):
    payload = {"username": "Insider Radar", "embeds": [embed]}
    for _ in range(3):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        time.sleep(1)
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


# ---------------------------------------------------------------- main

def run():
    webhook = os.environ.get("WEBHOOK_POLITICIANS", "").strip()
    if not webhook and not DEBUG:
        sys.exit("WEBHOOK_POLITICIANS is not set.")
    state = load_state()
    bootstrap = state is None
    state = state or {"seen": [], "trades": []}
    seen = set(state["seen"])
    since = date.today() - timedelta(days=LOOKBACK_DAYS)

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    legis = load_legislators(session)
    comms = load_committees(session)

    sources = []
    try:
        sources += [(f, "house") for f in house_filings(session, since)]
    except Exception as e:
        log(f"House unavailable: {e}")
    s_sess = None
    try:
        s_sess, sen = senate_filings(since)
        sources += [(f, "senate") for f in sen]
    except Exception as e:
        log(f"Senate unavailable: {e}")
    sources.sort(key=lambda x: x[0]["filed"] or date.min)

    new = [x for x in sources if x[0]["id"] not in seen]
    log(f"Filings in the last {LOOKBACK_DAYS} days: {len(sources)}, new: {len(new)}")
    if bootstrap and not DEBUG:
        for f, _ in new[:-BOOTSTRAP_POSTS]:
            state["seen"].append(f["id"])
        new = new[-BOOTSTRAP_POSTS:]
        log("First run: posting only the newest few.")
    if DEBUG:
        new = sources[-6:]          # debug: show the newest filings even if already posted

    prices, posted = {}, 0
    for f, src in new:
        if posted >= MAX_POSTS:
            break
        label = who(legis, f["first"], f["last"], f["state"])
        try:
            trades = house_trades(session, f) if src == "house" else senate_trades(s_sess, f)
        except Exception as e:
            log(f"  skip {f['id']} {label}: {e}")
            continue          # not marked as seen, try again next run
        if trades is None:
            log(f"  {label}: paper filing, skipped ({f['url']})")
            state["seen"].append(f["id"])
            continue
        elif not trades:
            log(f"  {label}: no stock trades found in {f['url']}")
            state["seen"].append(f["id"])
            continue
        else:
            sic = load_sic(state, {t["ticker"] for t in trades})
            member_comms = comms.get(member_id(legis, f["last"], f["state"]))
            for t in trades:
                m = committee_match((sic.get(t["ticker"]) or [None])[0], member_comms)
                t["match"] = list(m) if m else None
            embed = build_embed(f, label, trades, prices, comms.get(member_id(legis, f["last"], f["state"])), sic)
            for t in trades:
                state["trades"].append({"who": label, "ticker": t["ticker"], "type": t["type"],
                                        "date": t["date"].isoformat() if t["date"] else None,
                                        "amount": t["amount"], "filed": f["filed"].isoformat(),
                                        "url": f["url"], "match": t.get("match")})
        if DEBUG:
            log(json.dumps(embed, indent=1, default=str))
        else:
            post(webhook, embed)
            log(f"  posted {label} ({len(trades or [])} trades)")
        state["seen"].append(f["id"])
        posted += 1

    if not DEBUG:
        save_state(state)
    log(f"Done. Posted {posted}.")


if __name__ == "__main__":
    run()
