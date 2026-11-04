"""
Insider Radar - SEC Form 4 insider buy alerts for Discord.

What it does on every run:
  1. Reads the latest Form 4 filings from SEC EDGAR (public, free).
  2. Opens each new filing and keeps only real open-market BUYS
     (transaction code "P") above a minimum dollar value.
  3. Posts a plain-English alert to a Discord channel via webhook.
  4. Detects "cluster buys" (several different insiders buying the
     same stock within a few days) and posts those to a second channel.
  5. Remembers what it has already processed in state.json.

Configuration (environment variables / GitHub secrets):
  WEBHOOK_INSIDER_BUYS  (required) Discord webhook URL for insider buys
  WEBHOOK_CLUSTER       (optional) Discord webhook URL for cluster alerts
  WEBHOOK_CONVICTION    (optional) Discord webhook URL for #conviction-alerts (Strategy #1)
  SEC_USER_AGENT        (required by SEC) e.g. "InsiderRadar yourname@example.com"
  MIN_VALUE             (optional) minimum buy size in USD, default 50000
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

import cards
import casefiles
import conviction_live
from chart import make_chart, make_cluster_chart
from company import company_info
from score import drop_from_high, insider_score, score_bar, short_interest, track_record

# ---------------------------------------------------------------- settings

FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
    "&type=4&company=&dateb=&owner=include&count=100&output=atom&start={start}"
)
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

MIN_VALUE = float(os.environ.get("MIN_VALUE", "50000"))
# SEC's "type=4" feed also contains other forms starting with 4 (424B2, 40-17G...),
# so only a small share of each 100-entry page is a real Form 4. We therefore keep
# paging back until we reach the time of the previous run.
MAX_FEED_PAGES = 30            # hard cap: up to 3,000 feed entries per run
FEED_OVERLAP_MIN = 15          # re-check a little before last run's newest entry
# Only post stocks listed on these exchanges (skips risky OTC / penny stocks).
# If the exchange can't be determined, the alert is still posted.
ALLOWED_EXCHANGES = {"NYSE", "NASDAQ", "NYSE American", "NYSE Arca", "Cboe"}
SKIP_OTC = os.environ.get("SKIP_OTC", "1") != "0"
MAX_POSTS_PER_RUN = 15         # safety cap so a backlog never spams the channel
BOOTSTRAP_POSTS = 3            # on the very first run, post only a few examples
CLUSTER_WINDOW_DAYS = 14
CLUSTER_MIN_INSIDERS = 3
CONVICTION_MIN_EACH = conviction_live.MIN_EACH   # buys this small are stored (not posted) for Conviction Clusters
KEEP_SEEN = 6000
KEEP_BUYS_DAYS = 30
REQUEST_PAUSE = 0.15           # SEC allows max 10 requests/second

USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()

ATOM = "{http://www.w3.org/2005/Atom}"


# ---------------------------------------------------------------- helpers

def log(msg):
    print(msg, flush=True)


def http_get(url, session):
    time.sleep(REQUEST_PAUSE)
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    state["seen"] = state["seen"][-KEEP_SEEN:]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_BUYS_DAYS)).date().isoformat()
    state["buys"] = [b for b in state["buys"] if b.get("date", "") >= cutoff]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def money(value):
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


BAD_TICKERS = {"NONE", "N/A", "NA", "-", "--", "NULL"}   # filings for unlisted securities


def clean_ticker(raw):
    """
    Filers sometimes type the symbol oddly: 'NYSE:FLG', '$FLG', 'FLG, FLG-PA',
    'Nasdaq: ABC'. Return just the plain symbol ('FLG'), or '' if none.
    """
    t = (raw or "").strip().upper().replace("$", "").replace('"', "")
    if t in BAD_TICKERS:
        return t
    if ":" in t:
        t = t.rsplit(":", 1)[1]
    t = re.split(r"[\s,;/]+", t.strip())[0] if t.strip() else ""
    return t if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", t) else ""

ENTITY_WORDS = {"INC", "LLC", "LP", "LLP", "LTD", "LIMITED", "CORP", "CORPORATION", "CO", "COMPANY",
                "FUND", "FUNDS", "TRUST", "CAPITAL", "MANAGEMENT", "PARTNERS", "HOLDINGS", "GROUP",
                "BANK", "INSURANCE", "ADVISORS", "ADVISERS", "INVESTMENTS", "INVESTMENT", "FOUNDATION",
                "VENTURES", "PLC", "SA", "AG", "NV", "GMBH", "ASSOCIATES", "OPPORTUNITIES", "MASTER"}
NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "MD", "PHD"}


def _is_initial(word):
    return len(word.rstrip(".")) == 1


def _cap(word):
    if word.upper().rstrip(".") in NAME_SUFFIXES - {"JR", "SR"}:
        return word.upper()
    if word.isupper() or word.islower():
        return word.title()
    return word


def nice_name(raw):
    """
    SEC usually writes people as 'LAST FIRST MIDDLE [JR]'. Turn that into
    'First Middle Last Jr'. Company / fund names are kept in their order.
    """
    parts = raw.replace(",", " ").split()
    if not parts:
        return raw
    words_up = {p.upper().replace(".", "") for p in parts}
    if words_up & ENTITY_WORDS or len(parts) > 5:
        return " ".join(_cap(p) for p in parts)          # a company or fund
    suffix = []
    while len(parts) > 2 and parts[-1].upper().rstrip(".") in NAME_SUFFIXES:
        suffix.insert(0, parts.pop())
    # ALL-CAPS names follow SEC's LAST FIRST MIDDLE convention; mixed-case ones
    # are sometimes already written naturally ("Neil S Subin").
    natural = (not raw.isupper() and len(parts) >= 3
               and _is_initial(parts[1]) and not _is_initial(parts[-1]))
    if len(parts) >= 2 and not natural:
        parts = parts[1:] + parts[:1]                    # LAST FIRST M -> FIRST M LAST
    return " ".join(_cap(p) for p in parts + suffix)


FUND_RE = re.compile(r"\b(fund|funds|municipal\w*|closed[- ]end|etf|acquisition corp(oration)?|acquisition co|"
                     r"income trust|opportunities trust|bdc)\b", re.I)


def is_entity(raw):
    words_up = {p.upper().replace(".", "") for p in raw.replace(",", " ").split()}
    return bool(words_up & ENTITY_WORDS)


def is_noise(buy):
    """Closed-end funds, ETFs and SPACs: insider buying there says nothing about a business."""
    company = buy.get("company") or ""
    if FUND_RE.search(company):
        return "fund / SPAC"
    if re.fullmatch(r"[A-Z]{4}[UWR]", buy["ticker"]) and "acquisition" in company.lower():
        return "SPAC unit"
    return None


def only_big_holder(buy):
    """True if the buyer is just a 10% owner / fund (not an officer or director)."""
    roles = [r.lower() for r in buy["owner"]["roles"]]
    return all("10%" in r for r in roles) or (is_entity(buy["owner"]["name"]) and "director" not in roles)


def nice_company(raw):
    if raw.isupper():
        words = []
        for w in raw.split():
            if w in {"INC", "INC.", "CORP", "CORP.", "LLC", "LTD", "PLC", "CO", "CO.", "LP", "NV", "SA", "AG"}:
                words.append(w.title() if w not in {"LLC", "PLC", "LP", "NV", "SA", "AG"} else w)
            elif len(w) <= 3 and not re.search(r"[AEIOUY]", w):
                words.append(w)            # abbreviations like HF, PNC, JPM stay upper case
            else:
                words.append(w.title())
        return " ".join(words)
    return raw


# ---------------------------------------------------------------- EDGAR feed

def fetch_feed(session, seen, last_time, bootstrap):
    """
    Return (filings, newest_time) where filings is a list of (accession, cik)
    for new original Form 4 filings, newest first, and newest_time is the
    timestamp of the newest feed entry (saved for the next run).
    """
    cutoff = None
    if last_time:
        cutoff = datetime.fromisoformat(last_time) - timedelta(minutes=FEED_OVERLAP_MIN)
    found, order = {}, []
    newest_time = None
    pages_read = 0
    for page in range(MAX_FEED_PAGES):
        xml_text = http_get(FEED_URL.format(start=page * 100), session).text
        entries, page_newest, page_oldest = parse_feed(xml_text)
        pages_read += 1
        if page_newest is None:          # empty page: end of feed
            break
        if newest_time is None:
            newest_time = page_newest
        hit_seen = False
        for acc, cik in entries:
            if acc in seen:
                hit_seen = True
                continue
            if acc not in found:
                found[acc] = cik
                order.append(acc)
        if bootstrap:
            break                        # first run: one page is plenty
        if cutoff is not None:
            if page_oldest <= cutoff:    # reached the previous run's time
                break
        elif hit_seen:                   # old state without a timestamp
            break
    log(f"Read {pages_read} feed page(s).")
    return [(acc, found[acc]) for acc in order], newest_time


def parse_feed(xml_text):
    """Return (form4_entries, newest_time, oldest_time) for one feed page."""
    root = ET.fromstring(xml_text)
    out = []
    times = []
    for entry in root.findall(f"{ATOM}entry"):
        updated = (entry.findtext(f"{ATOM}updated") or "").strip()
        if updated:
            try:
                times.append(datetime.fromisoformat(updated))
            except ValueError:
                pass
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        if not title.startswith("4 "):          # skip "4/A", 424B2, 40-17G ...
            continue
        entry_id = entry.findtext(f"{ATOM}id") or ""
        m_acc = re.search(r"accession-number=(\d{10}-\d{2}-\d{6})", entry_id)
        link = entry.find(f"{ATOM}link")
        href = link.get("href", "") if link is not None else ""
        m_cik = re.search(r"/edgar/data/(\d+)/", href)
        if m_acc and m_cik:
            out.append((m_acc.group(1), m_cik.group(1)))
    if not times:
        return out, None, None
    return out, max(times), min(times)


def readable_filing_url(session, base, accession):
    """Link to the human-readable Form 4 on sec.gov (falls back to the filing index)."""
    index_url = f"{base}/{accession}-index.htm"
    try:
        html = http_get(f"{base}/{accession}-index.html", session).text
        m = re.search(r'href="(/Archives/edgar/data/[^"]+/xslF345X\d+/[^"]+\.xml)"', html)
        if m:
            return "https://www.sec.gov" + m.group(1)
    except Exception as e:
        log(f"  readable link not found for {accession}: {e}")
    return index_url


def fetch_form4_xml(session, accession, cik):
    acc_nodash = accession.replace("-", "")
    base = ARCHIVE_URL.format(cik=int(cik), acc=acc_nodash)
    listing = http_get(base + "/index.json", session).json()
    names = [i["name"] for i in listing.get("directory", {}).get("item", [])]
    xml_files = [n for n in names if n.lower().endswith(".xml") and "index" not in n.lower()]
    if not xml_files:
        return None, base
    return http_get(f"{base}/{xml_files[0]}", session).text, base


# ---------------------------------------------------------------- Form 4 parsing

def _text(node, path):
    if node is None:
        return ""
    found = node.find(path)
    if found is None:
        return ""
    value = found.find("value")
    target = value if value is not None else found
    return (target.text or "").strip()


def _num(node, path):
    try:
        return float(_text(node, path).replace(",", ""))
    except ValueError:
        return 0.0


def parse_form4(xml_text):
    """Return a dict describing the filing's open-market purchases, or None."""
    root = ET.fromstring(xml_text)

    ticker = clean_ticker(_text(root, "issuer/issuerTradingSymbol"))
    company = _text(root, "issuer/issuerName")
    try:
        issuer_cik = int(_text(root, "issuer/issuerCik") or 0) or None
    except ValueError:
        issuer_cik = None

    owners = []
    for ro in root.findall("reportingOwner"):
        name = _text(ro, "reportingOwnerId/rptOwnerName")
        try:
            owner_cik = int(_text(ro, "reportingOwnerId/rptOwnerCik") or 0) or None
        except ValueError:
            owner_cik = None
        rel = ro.find("reportingOwnerRelationship")
        roles = []
        if _text(rel, "isOfficer") in ("1", "true"):
            title = _text(rel, "officerTitle")
            roles.append(title if title else "Officer")
        if _text(rel, "isDirector") in ("1", "true"):
            roles.append("Director")
        if _text(rel, "isTenPercentOwner") in ("1", "true"):
            roles.append("10% Owner")
        if not roles and _text(rel, "isOther") in ("1", "true"):
            roles.append(_text(rel, "otherText") or "Insider")
        owners.append({"name": name, "roles": roles, "cik": owner_cik})

    # Pre-planned trades (Rule 10b5-1) are a weaker signal: newer filings have a
    # checkbox for it, older ones only mention it in a footnote.
    plan = _text(root, "aff10b5One").lower() in ("1", "true")
    if not plan:
        notes = " ".join((f.text or "") for f in root.iter("footnote"))
        plan = bool(re.search(r"(?<!not )(?<!not made )pursuant to (?:a |an )?(?:rule )?10b5-1", notes, re.I))

    shares = value = 0.0
    owned_after = 0.0
    dates = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        acq = _text(tx, "transactionAmounts/transactionAcquiredDisposedCode")
        if code != "P" or acq == "D":
            continue
        s = _num(tx, "transactionAmounts/transactionShares")
        p = _num(tx, "transactionAmounts/transactionPricePerShare")
        if s <= 0 or p <= 0:
            continue
        shares += s
        value += s * p
        owned_after = max(owned_after, _num(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction"))
        d = _text(tx, "transactionDate")
        if d:
            dates.append(d[:10])

    if shares <= 0 or not owners or not ticker or ticker in BAD_TICKERS:
        return None

    owned_before = owned_after - shares
    stake_change = (shares / owned_before * 100) if owned_before > 0 else None
    # only a real "new position" if the filing reports the holding after the buy
    new_position = owned_after > 0 and owned_before <= shares * 0.001

    return {
        "ticker": ticker,
        "company": company,
        "issuer_cik": issuer_cik,
        "owner": owners[0],
        "co_owners": owners[1:],
        "shares": shares,
        "value": value,
        "avg_price": value / shares,
        "owned_after": owned_after,
        "stake_change": stake_change,
        "trade_date": min(dates) if dates else "",
        "trade_date_end": max(dates) if dates else "",
        "plan_10b5_1": plan,
        "new_position": new_position,
    }


# ---------------------------------------------------------------- plain English

def role_text(roles):
    if not roles:
        return "an insider"
    short = {
        "chief executive officer": "CEO", "chief financial officer": "CFO",
        "chief operating officer": "COO", "chief technology officer": "CTO",
        "president and chief executive officer": "President & CEO",
    }
    cleaned = [short.get(r.lower(), r) for r in roles]
    return ", ".join(dict.fromkeys(cleaned))


def role_short(roles):
    """One word for titles: CEO, CFO, President, Chair, COO, Director, major holder, insider."""
    low = " ".join(roles).lower()
    for key, word in (("chief executive", "CEO"), ("ceo", "CEO"), ("chief financial", "CFO"), ("cfo", "CFO"),
                      ("president", "President"), ("chair", "Chair"), ("chief operating", "COO"),
                      ("director", "director"), ("10%", "major holder")):
        if key in low:
            return word
    return "insider"


def company_short(raw):
    name = nice_company(raw or "")
    name = re.sub(r"[,.]?\s+(Inc|Corp|Corporation|Co|Company|Ltd|Plc|PLC|LLC|LP|NV|SA|AG|Holdings?|Group|Incorporated)\.?$", "", name, flags=re.I)
    name = re.sub(r"[,.]?\s+(Inc|Corp|Co|Holdings?)\.?$", "", name, flags=re.I)
    return name.strip() or raw


def meaning_line(buy, rshort, others):
    who = "A major holder" if rshort == "major holder" else f"The {rshort}" if rshort != "insider" else "An insider"
    line = f"{who} put {money(buy['value'])} of {'its' if rshort == 'major holder' else 'their'} own money into the stock"
    sc = buy.get("stake_change")
    if buy.get("new_position"):
        line += ", opening a brand-new position"
    elif sc is not None and sc >= 100:
        line += f", more than doubling their stake (+{sc:.0f}%)"
    elif sc is not None and sc >= 10:
        line += f", growing their stake by {sc:.0f}%"
    line += "."
    extra = []
    d = buy.get("drop")
    if d is not None and d >= 0.15:
        extra.append(f"the stock is {d * 100:.0f}% below its 1-year high")
    if others:
        extra.append(f"{others} other insider{'s' if others != 1 else ''} bought in the last 14 days")
    if extra:
        x = " and ".join(extra)
        line += " " + x[0].upper() + x[1:] + "."
    if buy.get("plan_10b5_1"):
        line += " Pre-planned trade (10b5-1), so it says less."
    return line


def ticker_label(buy):
    """'NYSE: BPRE' when the exchange is known, otherwise just 'BPRE'."""
    ex = buy.get("exchange")
    return f"{ex}: {buy['ticker']}" if ex else buy["ticker"]


def summary_line(buy):
    name = nice_name(buy["owner"]["name"])
    who = f"**{name}** ({role_text(buy['owner']['roles'])})"
    own = "" if only_big_holder(buy) else " with their own money"
    line = (f"{who} bought **{money(buy['value'])}** of "
            f"**{nice_company(buy['company'])}** (**{ticker_label(buy)}**) stock{own}.")
    sc = buy["stake_change"]
    if buy.get("new_position"):
        line += " This looks like a **brand-new position** for them."
    elif sc is None:
        pass
    elif sc >= 100:
        line += f" That **more than doubles** their stake (+{sc:.0f}%)."
    elif sc >= 10:
        line += f" Their stake grew by **{sc:.0f}%**."
    return line


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def score_text(buy):
    if not buy.get("score"):
        return ""
    reasons = " · ".join(buy.get("score_why") or [])
    return (f"🎯 **Insider Score: {buy['score']}/10**  {score_bar(buy['score'])}"
            + (f"\n*{reasons}*" if reasons else "") + "\n\n")


def record_text(buy):
    rec = buy.get("record")
    if not rec:
        return ""
    nth = ordinal(rec["count"] + 1)
    since = rec["first"].strftime("%b %Y")
    line = f"\n\n🔁 **Track record:** "
    if rec.get("ret") is not None:
        sign = "+" if rec["ret"] >= 0 else ""
        line += (f"their last buy ({rec['last']:%b %d, %Y}) is **{sign}{rec['ret'] * 100:.0f}%** since. "
                 f"This is their **{nth} buy** since {since}.")
    else:
        line += f"this is their **{nth} buy** since {since}."
    return line


def short_text(short):
    if not short:
        return None
    parts = []
    if short.get("pct") is not None:
        parts.append(f"{short['pct'] * 100:.1f}% of shares")
    else:
        parts.append(f"{short['shares'] / 1e6:,.1f}M shares")
    if short.get("days_to_cover"):
        parts.append(f"{float(short['days_to_cover']):.1f} days to cover")
    return " · ".join(parts)


def build_embed(buy, filed_date, filing_url):
    """Short, phone-friendly alert: one summary line + one facts line + chart."""
    day = buy["trade_date"]
    try:
        day = datetime.fromisoformat(day).strftime("%b %d")
    except (TypeError, ValueError):
        pass
    facts = [f"💵 avg price ${buy['avg_price']:,.2f}", f"📅 bought {day or 'n/a'}"]
    if buy.get("drop") is not None and buy["drop"] >= 0.005:
        facts.append(f"📉 {buy['drop'] * 100:.0f}% below 1-year high")
    cap = cap_text(buy)
    if cap:
        facts.append(f"🏢 {cap}")
    short = buy.get("short") or {}
    if short.get("pct") and short["pct"] >= 0.10:
        facts.append(f"🩳 {short['pct'] * 100:.0f}% of shares shorted")
    if buy.get("plan_10b5_1"):
        facts.append("🗓 pre-planned trade (10b5-1)")
    title = f"💼 ${buy['ticker']}" + (f" ({buy['exchange']})" if buy.get("exchange") else "")
    if buy.get("sp500"):
        title += " · ⭐ S&P 500"
    return {
        "title": title,
        "url": filing_url,
        "description": (score_text(buy) + summary_line(buy) + record_text(buy)
                        + "\n\n" + " · ".join(facts)
                        + f"\n📄 [SEC filing]({filing_url})"),
        "color": 0x2ECC71,
        "footer": {"text": "Insider Radar · Source: SEC Form 4 · Not financial advice"},
    }


def cap_text(buy):
    """'$2.13B · Mid cap' or None."""
    if not buy.get("market_cap"):
        return None
    return f"{money(buy['market_cap'])} · {buy['size']}"


def build_cluster_embed(ticker, company, buys, exchange=None, info=None):
    info = info or {}
    names = []
    total = 0.0
    for b in buys:
        total += b["value"]
        line = f"• **{b['name']}** ({b['role']}) · {money(b['value'])} on {b['date']}"
        if b.get("url"):
            line += f" · [📄 SEC filing]({b['url']})"
        names.append(line)
    title = f"🔥 Cluster Buy · ${ticker}" + (f" ({exchange})" if exchange else "")
    if info.get("sp500"):
        title += " · ⭐ S&P 500"
    cap = cap_text(info)
    return {
        "title": title,
        "description": (
            f"**{len(buys)} different insiders** at **{nice_company(company)}** "
            f"(**{f'{exchange}: ' if exchange else ''}{ticker}**) bought stock "
            f"within {CLUSTER_WINDOW_DAYS} days, **{money(total)}** in total.\n\n" + "\n".join(names)
            + (f"\n\nMarket cap: **{cap}**" if cap else "")
        ),
        "color": 0xE67E22,
        "footer": {"text": "📡 Insider Radar · Source: SEC Form 4 · Not financial advice"},
    }


# ---------------------------------------------------------------- Discord

def post_discord(webhook, embed, image=None):
    """Post one embed. If image (PNG bytes) is given, it is shown inside the embed."""
    payload = {"username": "Insider Radar", "embeds": [embed]}
    if image:
        embed["image"] = {"url": "attachment://chart.png"}
    for _ in range(3):
        if image:
            r = requests.post(webhook, data={"payload_json": json.dumps(payload)},
                              files={"files[0]": ("chart.png", image, "image/png")}, timeout=30)
        else:
            r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        time.sleep(1)      # stay well under Discord's webhook rate limit
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


def post_embeds(webhook, embeds):
    """Post several embeds as one message (no image)."""
    payload = {"username": "Insider Radar", "embeds": embeds}
    for _ in range(3):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        time.sleep(1)
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


# ---------------------------------------------------------------- cluster logic

def find_cluster(state, ticker, today):
    """Return the recent buys for this ticker if it now counts as a cluster."""
    since = (datetime.fromisoformat(today) - timedelta(days=CLUSTER_WINDOW_DAYS)).date().isoformat()
    latest_per_insider = {}
    for b in state["buys"]:
        if b["ticker"] == ticker and b["date"] >= since and b["value"] >= MIN_VALUE:
            latest_per_insider[b["name"]] = b
    if len(latest_per_insider) < CLUSTER_MIN_INSIDERS:
        return None
    key = f"{ticker}:{len(latest_per_insider)}"
    if key in state.get("clusters_posted", []):
        return None
    state.setdefault("clusters_posted", []).append(key)
    state["clusters_posted"] = state["clusters_posted"][-500:]
    return sorted(latest_per_insider.values(), key=lambda b: b["date"])


def other_insiders(state, ticker, name, day):
    """How many OTHER insiders bought this stock in the cluster window."""
    since = (datetime.fromisoformat(day) - timedelta(days=CLUSTER_WINDOW_DAYS)).date().isoformat()
    return len({b["name"] for b in state["buys"]
                if b["ticker"] == ticker and b["date"] >= since and b["name"] != name and b["value"] >= MIN_VALUE})


def add_score(session, buy, rows, state, name, today):
    """Fill buy['drop'], ['record'], ['short'], ['score'], ['score_why']. Never raises."""
    try:
        buy["drop"] = drop_from_high(rows)
        buy["record"] = track_record(session, buy["owner"].get("cik"), buy.get("issuer_cik"),
                                     buy["trade_date"], rows)
        buy["short"] = short_interest(buy["ticker"], buy.get("shares_out"))
        others = other_insiders(state, buy["ticker"], name, buy["trade_date"] or today)
        buy["score"], buy["score_why"] = insider_score(buy, buy["drop"], others, buy["record"], buy["short"])
        log(f"  score {buy['score']}/10 ({', '.join(buy['score_why'])})")
    except Exception as e:
        log(f"  score skipped: {e}")


# ---------------------------------------------------------------- main

def run():
    webhook = os.environ.get("WEBHOOK_INSIDER_BUYS", "").strip()
    cluster_webhook = os.environ.get("WEBHOOK_CLUSTER", "").strip()
    conviction_webhook = os.environ.get("WEBHOOK_CONVICTION", "").strip()
    case_webhook = os.environ.get("WEBHOOK_CASE_FILES", "").strip()
    if not webhook:
        sys.exit("WEBHOOK_INSIDER_BUYS is not set.")
    if not USER_AGENT or "@" not in USER_AGENT:
        sys.exit('SEC_USER_AGENT must look like "InsiderRadar yourname@example.com" (SEC requires it).')

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})

    state = load_state()
    bootstrap = state is None
    if bootstrap:
        state = {"seen": [], "buys": [], "clusters_posted": []}
        log("First run: will post only a few example alerts.")
    seen = set(state["seen"])

    new_filings, newest_time = fetch_feed(session, seen, state.get("last_feed_time"), bootstrap)
    log(f"New Form 4 filings to check: {len(new_filings)}")

    today = datetime.now(timezone.utc).date().isoformat()
    posted = 0
    buys_found = 0
    limit = BOOTSTRAP_POSTS if bootstrap else MAX_POSTS_PER_RUN

    # oldest first, so alerts appear in the channel in time order
    for accession, cik in reversed(new_filings):
        state["seen"].append(accession)
        seen.add(accession)
        try:
            xml_text, base = fetch_form4_xml(session, accession, cik)
            if not xml_text:
                continue
            buy = parse_form4(xml_text)
        except Exception as e:          # one bad filing must not stop the run
            log(f"  skip {accession}: {e}")
            continue
        if not buy or buy["value"] < min(MIN_VALUE, CONVICTION_MIN_EACH):
            continue
        buys_found += 1

        # price chart + which exchange the stock trades on
        image, buy["exchange"], last_price, rows = make_chart(buy["ticker"], buy["trade_date"], buy["trade_date_end"], buy["avg_price"])
        name = nice_name(buy["owner"]["name"])
        if SKIP_OTC and buy["exchange"] and buy["exchange"] not in ALLOWED_EXCHANGES:
            log(f"  skip {buy['ticker']} {name} {money(buy['value'])}: listed on {buy['exchange']}")
            continue
        # sanity check: insider's price must be near the real market price
        # (some foreign filers report prices in their home currency)
        if last_price and not (0.33 <= buy["avg_price"] / last_price <= 3.0):
            log(f"  skip {buy['ticker']} {name}: price ${buy['avg_price']:,.2f} vs market ${last_price:,.2f} looks wrong")
            continue
        noise = is_noise(buy)
        if noise:
            log(f"  skip {buy['ticker']} {name}: {noise}")
            continue
        # joint filings: the same purchase reported by several related owners (family trust + company...)
        day = buy["trade_date"] or today
        dup = next((b for b in state["buys"] if b["ticker"] == buy["ticker"] and b["date"] == day
                    and b["name"] != name and abs(b["value"] - buy["value"]) <= 0.005 * buy["value"]), None)
        if dup:
            log(f"  skip {buy['ticker']} {name}: same purchase already reported by {dup['name']}")
            continue
        # market cap + S&P 500 tag
        buy.update(company_info(session, buy.get("issuer_cik"), buy["ticker"], last_price))

        filing_url = readable_filing_url(session, base, accession)
        state["buys"].append({
            "ticker": buy["ticker"], "company": buy["company"], "name": name,
            "role": role_text(buy["owner"]["roles"]), "value": round(buy["value"], 2),
            "date": buy["trade_date"] or today, "accession": accession, "url": filing_url,
            "price": round(buy["avg_price"], 4), "filed": today,
            "stake": conviction_live.stake_of(buy),
        })

        # Strategy #1: Conviction Clusters (judged on every stored buy, never stops the run)
        conv_hit = False
        if conviction_webhook and not bootstrap:
            try:
                sig = conviction_live.check(state, buy["ticker"], buy["trade_date"] or today)
                if sig:
                    buy["drop"] = drop_from_high(rows)
                    conviction_live.post_alert(conviction_webhook, sig, buy)
                    conv_hit = True
                    log(f"  CONVICTION CLUSTER {buy['ticker']}")
            except Exception as e:
                log(f"  conviction check skipped: {e}")

        if buy["value"] < MIN_VALUE:        # small buys are only stored for cluster detection
            continue
        sc = buy.get("stake_change")
        if only_big_holder(buy) and not buy.get("new_position") and (sc is None or sc < 5):
            log(f"  not posted {buy['ticker']} {name}: big holder adding {sc or 0:.1f}%")
            continue

        if posted >= limit:
            continue

        # facts behind the card (each piece is optional, never stops the run)
        add_score(session, buy, rows, state, name, today)
        others = other_insiders(state, buy["ticker"], name, buy["trade_date"] or today)
        rshort = role_short(buy["owner"]["roles"])
        cshort = company_short(buy["company"])
        prof = casefiles.profile(session, buy.get("issuer_cik"), buy["company"]) if buy.get("issuer_cik") else {}
        tags = ["conviction"] if conv_hit else []
        if case_webhook and not bootstrap:
            try:
                if casefiles.maybe_open(case_webhook, state, buy, {
                        "name": name, "role": role_text(buy["owner"]["roles"]), "role_short": rshort,
                        "company_short": cshort, "filing_url": filing_url, "exchange": buy.get("exchange"),
                        "profile": prof, "others_14d": others, "only_big_holder": only_big_holder(buy),
                        "filed": datetime.fromisoformat(today).strftime("%b %d")}):
                    tags.append("case")
            except Exception as e:
                log(f"  case file skipped: {e}")
        day_txt = buy["trade_date"]
        try:
            day_txt = datetime.fromisoformat(buy["trade_date"]).strftime("%b %d")
        except (TypeError, ValueError):
            pass
        sc = buy.get("stake_change")
        stake_txt = ("new position" if buy.get("new_position") else f"stake +{sc:.0f}%" if sc is not None and sc >= 1 else "")
        right = " · ".join(x for x in [cshort, cap_text(buy), "⭐ S&P 500" if buy.get("sp500") else None] if x)
        card = cards.buy_card({
            "ticker": buy["ticker"], "exchange": buy.get("exchange"), "industry": prof.get("label") or prof.get("industry"),
            "company_short": cshort, "role_short": rshort, "name": name, "role": role_text(buy["owner"]["roles"]),
            "value": buy["value"], "shares": buy["shares"], "price": buy["avg_price"], "date_text": day_txt,
            "stake_text": stake_txt, "meaning": meaning_line(buy, rshort, others), "footer_right": right, "tags": tags})
        content = (f"💼 **${buy['ticker']}** · {cshort}'s {rshort} buys {money(buy['value'])}"
                   + (" · 🔥 CONVICTION" if "conviction" in tags else "") + (" · 🔎 CASE FILE" if "case" in tags else "")
                   + f" · [SEC filing](<{filing_url}>)")
        cards.post_card(webhook, content, card)
        posted += 1
        log(f"  posted {buy['ticker']} {name} {money(buy['value'])}")

    # Case Files: post a result for every case whose event has passed
    if case_webhook and not bootstrap:
        try:
            casefiles.update_cases(case_webhook, state)
        except Exception as e:
            log(f"  case updates skipped: {e}")

    if newest_time is not None:
        state["last_feed_time"] = newest_time.isoformat()
    save_state(state)
    log(f"Done. Open-market buys over {money(MIN_VALUE)}: {buys_found}. Posted {posted} alert(s).")


if __name__ == "__main__":
    run()
