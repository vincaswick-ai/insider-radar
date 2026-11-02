"""
Insider Radar paper portfolio + weekly recap.

Portfolio rules (simple and honest, so the results can be shown publicly):
  - Every alert with Insider Score >= MIN_SCORE is "bought" for $1,000 (paper money)
    at the first close ON OR AFTER the day we posted it (we can't buy at the insider's price).
  - Positions are held HOLD_DAYS calendar days, then "sold" at that day's close.
  - Every position is compared with $1,000 put into the S&P 500 (SPY) on the same day.

Runs every weekday after the US close (adds new positions, closes old ones, saves
portfolio.json). On Fridays - or when RECAP=1 - it also posts the weekly recap.

Environment:
  WEBHOOK_RECAP     Discord webhook for #weekly-recap (public)
  WEBHOOK_PROOF     test webhook (#bot-testing), used when TARGET=test
  TARGET            "public" (default on schedule) or "test"
  RECAP             "1" to post the recap today even if it's not Friday
"""

import io
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

from chart import BG, GRID, MUTED, PANEL, TEXT, UP, DOWN, fetch_prices

HERE = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_FILE = os.path.join(HERE, "portfolio.json")
STATE_FILE = os.path.join(HERE, "state.json")
POL_FILE = os.path.join(HERE, "pol_state.json")

MIN_SCORE = 7
POSITION = 1000.0
HOLD_DAYS = 90
BENCH = "SPY"


def log(msg):
    print(msg, flush=True)


def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def day(s):
    return datetime.fromisoformat(str(s)[:10]).date()


def money(v):
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{sign}${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{sign}${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{sign}${v / 1e3:.0f}K"
    return f"{sign}${v:,.0f}"


def n(count, word):
    return f"**{count}** {word}{'' if count == 1 else 's'}"


def pct(x):
    v = round(x * 100, 1)
    return "0%" if v == 0 else f"{v:+.1f}%"


# ---------------------------------------------------------------- prices

PRICES = {}


def prices(ticker):
    """{date: close} for ~2 years (cached)."""
    if ticker not in PRICES:
        try:
            rows, _ = fetch_prices(ticker, "2y")
            PRICES[ticker] = {r[0]: r[4] for r in rows}
            time.sleep(0.3)
        except Exception as e:
            log(f"  no prices for {ticker}: {e}")
            PRICES[ticker] = {}
    return PRICES[ticker]


def close_on_or_after(ticker, d):
    p = prices(ticker)
    later = sorted(x for x in p if x >= d)
    return (later[0], p[later[0]]) if later else (None, None)


def close_on_or_before(ticker, d):
    p = prices(ticker)
    earlier = sorted(x for x in p if x <= d)
    return (earlier[-1], p[earlier[-1]]) if earlier else (None, None)


# ---------------------------------------------------------------- portfolio

def update_portfolio(pf, state, today):
    """Add new 7+ alerts, close positions older than HOLD_DAYS. Returns list of new positions."""
    have = {p["accession"] for p in pf["positions"]}
    new = []
    for b in state.get("buys", []):
        if (b.get("score") or 0) < MIN_SCORE or not b.get("accession") or b["accession"] in have:
            continue
        posted = day(b.get("filed") or b["date"])
        if b.get("filed") is None:
            posted += timedelta(days=2)          # older entries: Form 4 is due within 2 days
        if posted > today:
            continue
        entry_day, entry = close_on_or_after(b["ticker"], posted)
        _, spy = close_on_or_after(BENCH, posted)
        if not entry or not spy:
            log(f"  can't price {b['ticker']} yet, will retry")
            continue
        pos = {"ticker": b["ticker"], "company": b.get("company", ""), "name": b["name"], "role": b["role"],
               "score": b["score"], "accession": b["accession"], "url": b.get("url"),
               "entry_date": entry_day.isoformat(), "entry": round(entry, 4), "spy_entry": round(spy, 4),
               "exit_date": None, "exit": None, "spy_exit": None}
        pf["positions"].append(pos)
        have.add(b["accession"])
        new.append(pos)
        log(f"  bought {b['ticker']} ({b['score']}/10) at ${entry:,.2f} on {entry_day}")

    for p in pf["positions"]:
        if p["exit_date"]:
            continue
        due = day(p["entry_date"]) + timedelta(days=HOLD_DAYS)
        if today >= due:
            d, px = close_on_or_before(p["ticker"], due)
            _, spy = close_on_or_before(BENCH, due)
            if px and spy:
                p.update(exit_date=d.isoformat(), exit=round(px, 4), spy_exit=round(spy, 4))
                log(f"  sold {p['ticker']} at ${px:,.2f} after {HOLD_DAYS} days")
    return new


def position_return(p, today):
    """(stock return, SPY return) for a position, open or closed."""
    if p["exit_date"]:
        return p["exit"] / p["entry"] - 1, p["spy_exit"] / p["spy_entry"] - 1
    _, px = close_on_or_before(p["ticker"], today)
    _, spy = close_on_or_before(BENCH, today)
    if not px or not spy:
        return None, None
    return px / p["entry"] - 1, spy / p["spy_entry"] - 1


def totals(pf, today):
    """Portfolio and SPY total return on all money put in so far."""
    invested = val = spy_val = 0.0
    rows = []
    for p in pf["positions"]:
        r, s = position_return(p, today)
        if r is None:
            continue
        invested += POSITION
        val += POSITION * (1 + r)
        spy_val += POSITION * (1 + s)
        rows.append((p, r, s))
    if not invested:
        return None
    return {"invested": invested, "ret": val / invested - 1, "spy": spy_val / invested - 1,
            "rows": rows, "wins": sum(r > 0 for _, r, _ in rows), "beat": sum(r > s for _, r, s in rows)}


def history(pf, today):
    """Daily (date, portfolio %, SPY %) since the first position, for the chart."""
    if not pf["positions"]:
        return []
    start = min(day(p["entry_date"]) for p in pf["positions"])
    days = sorted(d for d in prices(BENCH) if start <= d <= today)
    out = []
    for d in days:
        inv = val = sv = 0.0
        for p in pf["positions"]:
            e = day(p["entry_date"])
            if e > d:
                continue
            end = min(d, day(p["exit_date"])) if p["exit_date"] else d
            _, px = close_on_or_before(p["ticker"], end)
            _, spy = close_on_or_before(BENCH, end)
            if not px or not spy:
                continue
            inv += POSITION
            val += POSITION * px / p["entry"]
            sv += POSITION * spy / p["spy_entry"]
        if inv:
            out.append((d, val / inv - 1, sv / inv - 1))
    return out


def render_chart(hist):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)
    xs = [h[0] for h in hist]
    p = [h[1] * 100 for h in hist]
    s = [h[2] * 100 for h in hist]
    ax.plot(xs, p, color=UP if p[-1] >= s[-1] else DOWN, lw=2.4, label=f"Insider Radar portfolio  {p[-1]:+.1f}%")
    ax.plot(xs, s, color=MUTED, lw=1.8, ls="--", label=f"S&P 500 (SPY)  {s[-1]:+.1f}%")
    ax.axhline(0, color=GRID, lw=1)
    ax.grid(color=GRID, lw=0.6, alpha=0.6)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:+.0f}%"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    leg = ax.legend(loc="upper left", frameon=False, fontsize=10)
    for t in leg.get_texts():
        t.set_color(TEXT)
    ax.set_title("Copying insider buys with paper money (Insider Score 7+)", color=TEXT, fontsize=11, loc="left")
    fig.text(0.99, 0.01, "Insider Radar · paper portfolio · not financial advice", color=MUTED,
             fontsize=7, ha="right")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------- weekly recap

def week_buys(state, start, end):
    return [b for b in state.get("buys", []) if start <= day(b.get("filed") or b["date"]) <= end]


def build_recap(pf, state, pol, today, new_this_week):
    start = today - timedelta(days=6)
    buys = week_buys(state, start, today)
    scored = sorted([b for b in buys if b.get("score")], key=lambda b: (-b["score"], -b["value"]))
    biggest = max(buys, key=lambda b: b["value"]) if buys else None
    trades = [t for t in pol.get("trades", []) if t.get("filed") and start <= day(t["filed"]) <= today]
    pol_buys = sum(t["type"] == "P" for t in trades)
    pol_sells = sum(t["type"].startswith("S") for t in trades)
    matches = [t for t in trades if t.get("match")]
    people = len({t["who"] for t in trades})
    tot = totals(pf, today)

    # executive summary: the three facts worth knowing
    facts = []
    if buys:
        facts.append(f"{n(len(buys), 'insider buy')} over $50K {'was' if len(buys) == 1 else 'were'} filed this week.")
    if biggest:
        facts.append(f"Biggest buy: **{biggest['name']}** ({biggest['role']}) put **{money(biggest['value'])}** "
                     f"into **${biggest['ticker']}**.")
    if trades:
        facts.append(f"{n(people, 'member')} of Congress reported {n(pol_buys, 'buy')} and {n(pol_sells, 'sale')}"
                     + (f", **{len(matches)}** in areas their own committee oversees." if matches else "."))
    if tot:
        verdict = "ahead of" if tot["ret"] > tot["spy"] else "behind"
        facts.append(f"Our paper portfolio is **{pct(tot['ret'])}**, {verdict} the S&P 500 (**{pct(tot['spy'])}**).")

    lines = ["**This week at a glance**"] + [f"• {f}" for f in facts[:4]]

    if scored:
        lines += ["", "**🎯 Top insider buys by Insider Score**"]
        for b in scored[:3]:
            line = f"**{b['score']}/10** · ${b['ticker']} · {b['name']} ({b['role']}) · {money(b['value'])}"
            if b.get("url"):
                line += f" · [filing]({b['url']})"
            lines.append(line)

    if matches:
        lines += ["", "**⚠️ Committee matches**"]
        seen = set()
        for t in matches:
            key = (t["who"], t["ticker"])
            if key in seen:
                continue
            seen.add(key)
            word = "bought" if t["type"] == "P" else "sold"
            lines.append(f"{t['who']} {word} ${t['ticker']} ({t['match'][0]} Committee)")
            if len(seen) >= 3:
                break

    if tot:
        lines += ["", "**📈 Insider Radar paper portfolio**",
                  f"{len(tot['rows'])} positions · {tot['wins']} up · {tot['beat']} beating the S&P 500"]
        best = max(tot["rows"], key=lambda x: x[1])
        worst = min(tot["rows"], key=lambda x: x[1])
        lines.append(f"Best: ${best[0]['ticker']} **{pct(best[1])}** · Worst: ${worst[0]['ticker']} **{pct(worst[1])}**")
        if new_this_week:
            lines.append("New this week: " + ", ".join(f"${p['ticker']} ({p['score']}/10)" for p in new_this_week[:6]))
        lines.append(f"*Rules: every alert with Insider Score {MIN_SCORE}+ gets $1,000 of paper money at the next "
                     f"close and is held {HOLD_DAYS} days. Compared with the same $1,000 in SPY.*")

    return {
        "title": f"📰 Weekly recap · {start:%b %d} – {today:%b %d}",
        "description": "\n".join(lines),
        "color": 0x5865F2,
        "footer": {"text": "📡 Insider Radar · Paper money only · Not financial advice"},
    }


def post(webhook, embed, image=None):
    payload = {"username": "Insider Radar", "embeds": [embed]}
    if image:
        embed["image"] = {"url": "attachment://portfolio.png"}
    for _ in range(3):
        if image:
            r = requests.post(webhook, data={"payload_json": json.dumps(payload)},
                              files={"files[0]": ("portfolio.png", image, "image/png")}, timeout=30)
        else:
            r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


def run():
    today = datetime.now(timezone.utc).date()
    pf = load(PORTFOLIO_FILE, {"started": today.isoformat(), "positions": []})
    state = load(STATE_FILE, {"buys": []})
    pol = load(POL_FILE, {"trades": []})

    update_portfolio(pf, state, today)
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(pf, f, indent=1)
    tot = totals(pf, today)
    if tot:
        log(f"Portfolio: {len(tot['rows'])} positions, {pct(tot['ret'])} vs SPY {pct(tot['spy'])}")
    else:
        log("Portfolio is empty so far.")

    if today.weekday() != 4 and os.environ.get("RECAP") != "1":
        return
    target = os.environ.get("TARGET", "public").strip().lower()
    webhook = (os.environ.get("WEBHOOK_RECAP") if target == "public" else os.environ.get("WEBHOOK_PROOF") or "").strip()
    if not webhook:
        log(f"No webhook for target '{target}', recap not posted.")
        return
    week_start = today - timedelta(days=6)
    new_this_week = [p for p in pf["positions"] if day(p["entry_date"]) >= week_start]
    hist = history(pf, today)
    image = render_chart(hist) if len(hist) >= 2 else None
    post(webhook, build_recap(pf, state, pol, today, new_this_week), image)
    log(f"Recap posted to {target}.")


if __name__ == "__main__":
    run()
