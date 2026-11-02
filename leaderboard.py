"""
Weekly leaderboard: whose buys actually made money?

  1. Politicians: every stock the House members bought in the last 12 months
     (from the House Clerk's PTR filings), how each one did since the trade day,
     compared with the S&P 500 (SPY) over the same days. Members with 3+ buys only.
  2. Insiders buying right now: everyone in our recent alerts who bought the same
     company before, ranked by how those earlier buys have done.

Parsed House filings are cached in lb_cache.json so a weekly run only reads new PDFs.

Environment:
  SEC_USER_AGENT        required by SEC
  WEBHOOK_LEADERBOARD   public channel (falls back to WEBHOOK_RECAP)
  WEBHOOK_PROOF         test channel (#bot-testing)
  TARGET                "public" (default on schedule) or "test"
  LB_DAYS               politician lookback in days (default 365)
"""

import io
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

import politicians as pol
from bot import fetch_form4_xml, load_state, money, nice_name, parse_form4
from chart import BG, GRID, MUTED, PANEL, TEXT, fetch_prices
from repost import issuer_cik
from score import SPREE_DAYS, close_on, past_buys

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "lb_cache.json")
LB_DAYS = int(os.environ.get("LB_DAYS") or 365)
MIN_POL_BUYS = 5
MIN_AGE_DAYS = 60          # an earlier insider buy must be this old to count as a track record
TOP_N = 5
LINE_COLORS = ["#f0b232", "#5865f2", "#23a55a", "#eb459e", "#00a8fc"]
MEDALS = ["🥇", "🥈", "🥉"]


def log(msg):
    print(msg, flush=True)


def pct(x):
    return f"{x * 100:+.0f}%"


class Prices:
    """Daily closes per ticker, fetched once (2 years so year-old trades are covered)."""

    def __init__(self):
        self.rows = {}

    def get(self, ticker):
        if ticker not in self.rows:
            try:
                self.rows[ticker] = fetch_prices(ticker, "2y")[0]
            except Exception:
                self.rows[ticker] = []
            time.sleep(0.2)
        return self.rows[ticker]

    def since(self, ticker, day):
        """Return from the close on `day` to the last close, or None."""
        rows = self.get(ticker)
        if not rows or not day or day < rows[0][0]:
            return None
        base = close_on(rows, day)
        return rows[-1][4] / base - 1 if base else None


# ---------------------------------------------------------------- politicians

def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"docs": {}}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))


def politician_board(prices):
    session = requests.Session()
    session.headers.update({"User-Agent": pol.UA})
    legis = pol.load_legislators(session)
    today = date.today()
    since = today - timedelta(days=LB_DAYS)
    filings = pol.house_filings(session, since)
    cache = load_cache()
    new = 0
    log(f"House filings since {since}: {len(filings)} ({len(cache['docs'])} cached)")
    for f in filings:
        if f["id"] in cache["docs"]:
            continue
        try:
            trades = pol.house_trades(session, f) or []
        except Exception as e:
            log(f"  skip {f['url']}: {e}")
            continue
        label = pol.who(legis, f["first"], f["last"], f["state"])
        cache["docs"][f["id"]] = {
            "who": label, "filed": f["filed"].isoformat(),
            "buys": [{"t": t["ticker"], "d": t["date"].isoformat(), "a": t["amount"]}
                     for t in trades if t["type"] == "P" and not t.get("option") and t["date"]],
        }
        new += 1
        if new % 50 == 0:
            log(f"  read {new} new filings")
            save_cache(cache)
    save_cache(cache)
    log(f"Read {new} new filings")

    members = {}
    for doc in cache["docs"].values():
        for b in doc["buys"]:
            d = date.fromisoformat(b["d"])
            if d < since or d > today - timedelta(days=7):      # too fresh to judge
                continue
            r = prices.since(b["t"], d)
            spy = prices.since("SPY", d)
            if r is None or spy is None or abs(r) > 5:         # bad price data
                continue
            members.setdefault(doc["who"], []).append({"ticker": b["t"], "day": d, "ret": r, "spy": spy})

    rows = []
    for who, buys in members.items():
        # the same stock bought several times counts once (first buy), so one big
        # position doesn't fill the whole average
        first = {}
        for b in sorted(buys, key=lambda x: x["day"]):
            first.setdefault(b["ticker"], b)
        picks = list(first.values())
        if len(picks) < MIN_POL_BUYS:
            continue
        avg = sum(p["ret"] for p in picks) / len(picks)
        edge = sum(p["ret"] - p["spy"] for p in picks) / len(picks)
        wins = sum(1 for p in picks if p["ret"] - p["spy"] > 0) / len(picks)
        best = max(picks, key=lambda p: p["ret"])
        rows.append({"who": who, "n": len(picks), "avg": avg, "edge": edge, "wins": wins, "best": best,
                     "picks": picks})
    rows.sort(key=lambda r: -r["edge"])
    log(f"\n===== politicians ({len(rows)} with {MIN_POL_BUYS}+ different stocks bought) =====")
    for r in rows:
        log(f"{r['edge'] * 100:+6.1f}% vs SPY | avg {pct(r['avg'])} | {r['n']} stocks | "
            f"beat SPY {r['wins'] * 100:.0f}% | best {r['best']['ticker']} {pct(r['best']['ret'])} | {r['who']}")
    return rows


# ---------------------------------------------------------------- insiders

def sprees(days):
    """Group buy days (newest first) into buying sprees; return each spree's first day."""
    out, group = [], []
    for d in days:
        if group and (group[-1] - d).days > SPREE_DAYS:
            out.append(group[-1])
            group = []
        group.append(d)
    if group:
        out.append(group[-1])
    return out


def insider_board(prices):
    session = requests.Session()
    session.headers.update({"User-Agent": os.environ.get("SEC_USER_AGENT", ""),
                            "Accept-Encoding": "gzip, deflate"})
    state = load_state() or {"buys": []}
    since = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
    latest = {}
    for b in state["buys"]:
        if b["date"] < since or not b.get("accession") or b["value"] < 50000:
            continue
        key = (b["ticker"], b["name"])
        if key not in latest or b["date"] > latest[key]["date"]:
            latest[key] = b
    log(f"\nChecking track records of {len(latest)} recent insider buyers")

    ciks, rows = {}, []
    for (ticker, _), b in latest.items():
        try:
            if ticker not in ciks:
                ciks[ticker] = issuer_cik(session, ticker)
            xml_text, _ = fetch_form4_xml(session, b["accession"], ciks[ticker])
            buy = parse_form4(xml_text) if xml_text else None
            if not buy:
                continue
            day = date.fromisoformat(buy["trade_date"])
            past = past_buys(session, buy["owner"].get("cik"), ciks[ticker], day - timedelta(days=SPREE_DAYS))
            starts = [d for d in sprees(past) if (day - d).days >= MIN_AGE_DAYS]
            rets = [r for r in (prices.since(ticker, d) for d in starts) if r is not None]
            if not rets:
                continue
            name = nice_name(buy["owner"]["name"])
            if any(r["name"] == name and r["ticker"] == ticker for r in rows):
                continue
            rows.append({"name": name, "role": b["role"], "ticker": ticker,
                         "value": buy["value"], "date": buy["trade_date"], "n": len(rets),
                         "avg": sum(rets) / len(rets), "wins": sum(1 for r in rets if r > 0) / len(rets),
                         "first": starts[-1]})
        except Exception as e:
            log(f"  skip {ticker} {b['name']}: {e}")
    rows.sort(key=lambda r: (-(r["wins"]), -r["avg"]))
    log(f"\n===== insiders buying now with a track record ({len(rows)}) =====")
    for r in rows:
        log(f"avg {pct(r['avg'])} | {r['n']} earlier buys since {r['first']} | won {r['wins'] * 100:.0f}% | "
            f"{r['name']} ({r['role']}) ${r['ticker']} bought {money(r['value'])} on {r['date']}")
    return rows


# ---------------------------------------------------------------- charts

def short_who(who):
    """'Josh Gottheimer (D-NJ)' -> 'Gottheimer (D-NJ)'."""
    name, _, tag = who.partition(" (")
    last = [w for w in name.replace(",", "").split() if w not in ("Jr.", "Jr", "Sr.", "II", "III", "IV")][-1]
    return f"{last} ({tag}" if tag else last


def copy_curve(picks, prices, days, ticker=None):
    """% return over time of $1,000 put into every stock the member bought, on the day they bought it.
    ticker='SPY' = the same money on the same days put into the S&P 500 instead."""
    out = []
    for d in days:
        invested = value = 0.0
        for p in picks:
            if p["day"] > d:
                continue
            rows = prices.get(ticker or p["ticker"])
            base, now = close_on(rows, p["day"]), close_on(rows, d)
            if base and now:
                invested += 1000
                value += 1000 * now / base
        out.append(value / invested - 1 if invested else None)
    return out


def _fig(width=8, height=4.4):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(width, height), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(color=GRID, lw=0.6, alpha=0.6)
    return plt, fig, ax


def _png(plt, fig):
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def politician_chart(rows, prices):
    import matplotlib.dates as mdates
    import matplotlib.ticker as mticker
    spy = prices.get("SPY")
    start = date.today() - timedelta(days=LB_DAYS)
    days = [r[0] for r in spy if r[0] >= start]
    if len(days) < 20:
        return None
    plt, fig, ax = _fig()
    ax.axhline(0, color=MUTED, lw=2, ls="--", label="S&P 500 (same money, same days)")
    for i, r in enumerate(rows[:TOP_N]):
        mine = copy_curve(r["picks"], prices, days)
        spx = copy_curve(r["picks"], prices, days, "SPY")
        pts = [(d, (m - x) * 100) for d, m, x in zip(days, mine, spx) if m is not None and x is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=LINE_COLORS[i], lw=2.2,
                    label=f"#{i + 1} {short_who(r['who'])}  {pts[-1][1]:+.1f}%")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:+.0f}%"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    leg = ax.legend(loc="upper left", frameon=False, fontsize=9.5)
    for t in leg.get_texts():
        t.set_color(TEXT)
    ax.set_title("Copying their stock buys: how far ahead of the S&P 500 you would be", color=TEXT,
                 fontsize=11, loc="left")
    fig.text(0.99, 0.01, "Insider Radar · House PTR filings, last 12 months · not financial advice",
             color=MUTED, fontsize=7, ha="right")
    return _png(plt, fig)


def insider_chart(rows):
    top = rows[:TOP_N]
    if not top:
        return None
    plt, fig, ax = _fig(8, 3.6)
    labels = [f"#{i + 1} {r['name']} · {r['ticker']}" for i, r in enumerate(top)][::-1]
    vals = [r["avg"] * 100 for r in top][::-1]
    colors = LINE_COLORS[:len(top)][::-1]
    bars = ax.barh(labels, vals, color=colors, height=0.6)
    for b, v in zip(bars, vals):
        ax.text(b.get_width() + max(vals) * 0.01, b.get_y() + b.get_height() / 2, f"{v:+.0f}%",
                va="center", color=TEXT, fontsize=10, fontweight="bold")
    ax.set_xlim(min(0, min(vals)) * 1.1, max(vals) * 1.15)
    ax.xaxis.set_visible(False)
    ax.grid(False)
    ax.tick_params(axis="y", colors=TEXT, labelsize=10, length=0)
    ax.set_title("How their earlier buys of the same stock did (average)", color=TEXT, fontsize=11, loc="left")
    fig.text(0.99, 0.01, "Insider Radar · SEC Form 4 · not financial advice", color=MUTED, fontsize=7, ha="right")
    return _png(plt, fig)


# ---------------------------------------------------------------- Discord

def pol_embed(rows):
    lines = []
    for i, r in enumerate(rows[:TOP_N]):
        mark = MEDALS[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"{mark} **{r['who']}** · **{pct(r['edge'])}** vs S&P 500 · {r['n']} stocks · "
                     f"best ${r['best']['ticker']} {pct(r['best']['ret'])}")
    return {
        "title": "🏛️ Politician leaderboard: last 12 months",
        "description": "Who in the House picks stocks best? Ranked by how much each stock they bought "
                       "beat the S&P 500 over the same days.\n\n" + "\n".join(lines),
        "color": 0xF0B232,
        "footer": {"text": f"House members with {MIN_POL_BUYS}+ different stocks bought · "
                           "Senate not included · Not financial advice"},
    }


def insider_embed(rows):
    lines = []
    for i, r in enumerate(rows[:TOP_N]):
        mark = MEDALS[i] if i < 3 else f"`{i + 1}.`"
        times = "buy" if r["n"] == 1 else "buys"
        lines.append(f"{mark} **{r['name']}** ({r['role']}, ${r['ticker']}) · {r['n']} earlier {times} "
                     f"**{pct(r['avg'])}** · just bought {money(r['value'])}")
    return {
        "title": "🧑‍💼 Insiders buying now with the best track record",
        "description": "Insiders from our alerts in the last 30 days who bought the same company before. "
                       "How did their earlier buys do?\n\n" + "\n".join(lines),
        "color": 0x23A55A,
        "footer": {"text": "Earlier buys a few days apart count as one · Not financial advice"},
    }


def post(webhook, embeds, images):
    """images: list of (filename, png bytes or None), one per embed."""
    files = {}
    for i, (embed, (fname, png)) in enumerate(zip(embeds, images)):
        if png:
            embed["image"] = {"url": f"attachment://{fname}"}
            files[f"files[{i}]"] = (fname, png, "image/png")
    payload = {"username": "Insider Radar", "embeds": embeds}
    for _ in range(3):
        if files:
            r = requests.post(webhook, data={"payload_json": json.dumps(payload)}, files=files, timeout=60)
        else:
            r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        log(f"chart failed: {e}")
        return None


def main():
    target = (os.environ.get("TARGET") or "public").strip().lower()
    if target == "public":
        webhook = os.environ.get("WEBHOOK_LEADERBOARD") or os.environ.get("WEBHOOK_RECAP") or ""
    else:
        webhook = os.environ.get("WEBHOOK_PROOF") or ""
    prices = Prices()
    embeds, images = [], []
    try:
        p = politician_board(prices)
        if p:
            embeds.append(pol_embed(p))
            images.append(("politicians.png", safe(politician_chart, p, prices)))
    except Exception as e:
        log(f"politician leaderboard failed: {e}")
    try:
        i = insider_board(prices)
        if i:
            embeds.append(insider_embed(i))
            images.append(("insiders.png", safe(insider_chart, i)))
    except Exception as e:
        log(f"insider leaderboard failed: {e}")
    if not embeds:
        log("Nothing to post.")
        return
    if not webhook.strip():
        log(f"No webhook for target '{target}', leaderboard not posted.")
        return
    post(webhook.strip(), embeds, images)
    log(f"Posted leaderboard to {target}.")


if __name__ == "__main__":
    main()
