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

from chart import make_chart
from company import company_info

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


def nice_company(raw):
    if raw.isupper():
        words = []
        for w in raw.split():
            if w in {"INC", "INC.", "CORP", "CORP.", "LLC", "LTD", "PLC", "CO", "CO.", "LP", "NV", "SA", "AG"}:
                words.append(w.title() if w not in {"LLC", "PLC", "LP", "NV", "SA", "AG"} else w)
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

    ticker = _text(root, "issuer/issuerTradingSymbol").upper()
    company = _text(root, "issuer/issuerName")
    try:
        issuer_cik = int(_text(root, "issuer/issuerCik") or 0) or None
    except ValueError:
        issuer_cik = None

    owners = []
    for ro in root.findall("reportingOwner"):
        name = _text(ro, "reportingOwnerId/rptOwnerName")
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
        owners.append({"name": name, "roles": roles})

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


def ticker_label(buy):
    """'NYSE: BPRE' when the exchange is known, otherwise just 'BPRE'."""
    ex = buy.get("exchange")
    return f"{ex}: {buy['ticker']}" if ex else buy["ticker"]


def summary_line(buy):
    name = nice_name(buy["owner"]["name"])
    who = f"**{name}** ({role_text(buy['owner']['roles'])})"
    line = (f"{who} bought **{money(buy['value'])}** of "
            f"**{nice_company(buy['company'])}** (**{ticker_label(buy)}**) stock with their own money.")
    sc = buy["stake_change"]
    if sc is None:
        line += " This looks like a **brand-new position** for them."
    elif sc >= 100:
        line += f" That **more than doubles** their stake (+{sc:.0f}%)."
    elif sc >= 10:
        line += f" Their stake grew by **{sc:.0f}%**."
    return line


def build_embed(buy, filed_date, filing_url):
    trade = buy["trade_date"]
    if buy["trade_date_end"] and buy["trade_date_end"] != trade:
        trade = f"{trade} to {buy['trade_date_end']}"
    fields = [
        {"name": "Shares", "value": f"{buy['shares']:,.0f}", "inline": True},
        {"name": "Avg price", "value": f"${buy['avg_price']:,.2f}", "inline": True},
        {"name": "Total", "value": money(buy["value"]), "inline": True},
        {"name": "Bought on", "value": trade or "n/a", "inline": True},
        {"name": "Disclosed on", "value": filed_date, "inline": True},
    ]
    if buy["stake_change"] is not None:
        fields.append({"name": "Stake change", "value": f"+{buy['stake_change']:.1f}%", "inline": True})
    cap = cap_text(buy)
    if cap:
        fields.append({"name": "Market cap", "value": cap, "inline": True})
    title = f"💼 Insider Buy · ${buy['ticker']}" + (f" ({buy['exchange']})" if buy.get("exchange") else "")
    if buy.get("sp500"):
        title += " · ⭐ S&P 500"
    return {
        "title": title,
        "url": filing_url,
        "description": summary_line(buy),
        "color": 0x2ECC71,
        "fields": fields,
        "footer": {"text": "📡 Insider Radar · Source: SEC Form 4 · Not financial advice"},
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
        names.append(f"• **{b['name']}** ({b['role']}) · {money(b['value'])} on {b['date']}")
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


# ---------------------------------------------------------------- cluster logic

def find_cluster(state, ticker, today):
    """Return the recent buys for this ticker if it now counts as a cluster."""
    since = (datetime.fromisoformat(today) - timedelta(days=CLUSTER_WINDOW_DAYS)).date().isoformat()
    latest_per_insider = {}
    for b in state["buys"]:
        if b["ticker"] == ticker and b["date"] >= since:
            latest_per_insider[b["name"]] = b
    if len(latest_per_insider) < CLUSTER_MIN_INSIDERS:
        return None
    key = f"{ticker}:{len(latest_per_insider)}"
    if key in state.get("clusters_posted", []):
        return None
    state.setdefault("clusters_posted", []).append(key)
    state["clusters_posted"] = state["clusters_posted"][-500:]
    return sorted(latest_per_insider.values(), key=lambda b: b["date"])


# ---------------------------------------------------------------- main

def run():
    webhook = os.environ.get("WEBHOOK_INSIDER_BUYS", "").strip()
    cluster_webhook = os.environ.get("WEBHOOK_CLUSTER", "").strip()
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
        if not buy or buy["value"] < MIN_VALUE:
            continue
        buys_found += 1

        # price chart + which exchange the stock trades on
        image, buy["exchange"], last_price = make_chart(buy["ticker"], buy["trade_date"], buy["trade_date_end"], buy["avg_price"])
        name = nice_name(buy["owner"]["name"])
        if SKIP_OTC and buy["exchange"] and buy["exchange"] not in ALLOWED_EXCHANGES:
            log(f"  skip {buy['ticker']} {name} {money(buy['value'])}: listed on {buy['exchange']}")
            continue
        # market cap + S&P 500 tag
        buy.update(company_info(session, buy.get("issuer_cik"), buy["ticker"], last_price))

        state["buys"].append({
            "ticker": buy["ticker"], "company": buy["company"], "name": name,
            "role": role_text(buy["owner"]["roles"]), "value": round(buy["value"], 2),
            "date": buy["trade_date"] or today, "accession": accession,
        })

        if posted >= limit:
            continue
        filing_url = f"{base}/{accession}-index.htm"
        post_discord(webhook, build_embed(buy, today, filing_url), image)
        posted += 1
        log(f"  posted {buy['ticker']} {name} {money(buy['value'])}")

        if cluster_webhook and not bootstrap:
            cluster = find_cluster(state, buy["ticker"], buy["trade_date"] or today)
            if cluster:
                post_discord(cluster_webhook, build_cluster_embed(buy["ticker"], buy["company"], cluster, buy.get("exchange"), buy))
                log(f"  cluster alert {buy['ticker']}")

    if newest_time is not None:
        state["last_feed_time"] = newest_time.isoformat()
    save_state(state)
    log(f"Done. Open-market buys over {money(MIN_VALUE)}: {buys_found}. Posted {posted} alert(s).")


if __name__ == "__main__":
    run()
