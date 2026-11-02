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

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
LOOKBACK_DAYS = 10           # look at filings from the last N days each run
MAX_POSTS = 12               # safety cap per run
BOOTSTRAP_POSTS = 3
MAX_LINES = 10               # trades listed per alert
DEBUG = os.environ.get("POL_DEBUG") == "1"

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
            out[(last, term["state"])] = (full, party, term["type"])
            out.setdefault((last, None), (full, party, term["type"]))
    except Exception as e:
        log(f"legislator list unavailable: {e}")
    return out


def who(legis, first, last, state):
    """'Nancy Pelosi (D-CA)' style label."""
    hit = legis.get((last.lower(), state)) or legis.get((last.lower(), None))
    if hit:
        full, party, _ = hit
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


def build_embed(filing, label, trades, prices):
    lines, buys = [], 0
    for t in trades[:MAX_LINES]:
        word = TYPE_WORD.get(t["type"], t["type"])
        icon = "🟢" if t["type"] == "P" else "🔴" if t["type"].startswith("S") else "⚪"
        buys += t["type"] == "P"
        line = f"{icon} **{word} ${t['ticker']}**{' (options)' if t['option'] else ''} · {compact_amount(t['amount'])}"
        if t["date"]:
            line += f" · {t['date']:%b %d}"
        move = since_trade(t["ticker"], t["date"], prices)
        if move is not None:
            line += f" · since then **{move * 100:+.0f}%**"
        lines.append(line)
    if len(trades) > MAX_LINES:
        lines.append(f"…and {len(trades) - MAX_LINES} more in the filing")

    dated = [t["date"] for t in trades if t["date"]]
    delay = ""
    if dated and filing["filed"]:
        days = (filing["filed"] - min(dated)).days
        delay = f"\n\n⏱ Reported **{days} days** after the {'first ' if len(set(dated)) > 1 else ''}trade."
        if days > 45:
            delay += " That's past the 45-day legal deadline."
    return {
        "title": f"🏛️ {label} · {filing['chamber']}",
        "url": filing["url"],
        "description": ("\n".join(lines) + delay
                        + "\n\nCongress only reports amount ranges, not exact sums."
                        + f"\n\n📄 [View the official filing]({filing['url']})"),
        "color": 0x3498DB if buys else 0x95A5A6,
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
        new = new[-6:]

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
            embed = paper_embed(f, label)
        elif not trades:
            log(f"  {label}: no stock trades found in {f['url']}")
            state["seen"].append(f["id"])
            continue
        else:
            embed = build_embed(f, label, trades, prices)
            for t in trades:
                state["trades"].append({"who": label, "ticker": t["ticker"], "type": t["type"],
                                        "date": t["date"].isoformat() if t["date"] else None,
                                        "amount": t["amount"], "filed": f["filed"].isoformat(),
                                        "url": f["url"]})
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
