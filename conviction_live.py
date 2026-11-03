"""
Conviction Clusters, live (Strategy #1 on the server).

Same rule as the pre-registered backtest (conviction.py):
  - 3+ different insiders, $25K+ each, open-market buys within 7 days
  - judged ONCE, when the 3rd insider shows up (later insiders don't change the verdict)
  - conviction = $5M+ bought in total  OR  median insider at least doubled their stake (or new position)
  - one cluster per company per 60 days
  - trade: buy next trading day's open, sell at the close 5 trading days later

bot.py calls check() for every new buy and post_alert() when it returns a signal.
`python conviction_live.py` (run by .github/workflows/conviction.yml) does the follow-up:
  - ~1 hour after the US open: "sell today" reminders in #conviction-alerts
  - after the close: final results vs the S&P 500 in #track-record, plus running totals
  - Fridays after the close: a weekly summary in #track-record
Signals are stored in conviction_log.json (committed back to the repo).

Environment: WEBHOOK_CONVICTION (#conviction-alerts), WEBHOOK_CONV_RECORD (#track-record), MODE (remind|results|auto)
"""

import json
import os
import statistics as st
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "conviction_log.json")
PRICE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

MIN_EACH = 25_000
WINDOW_DAYS = 7
MIN_INSIDERS = 3
BIG_TOTAL = 5_000_000
DOUBLED = 1.0          # stake grew by 100%+
NEW_POSITION = 9.99
COOLDOWN_DAYS = 60
HOLD = 5
COLOR = 0xF5B700


# ---------------------------------------------------------------- storage

def load_log():
    if not os.path.exists(LOG_FILE):
        return {"signals": []}
    with open(LOG_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_log(log_data):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=1)


# ---------------------------------------------------------------- detection (called by bot.py)

def stake_of(buy):
    """Stake growth as a fraction (1.0 = doubled), NEW_POSITION for a brand-new holder, None if unknown."""
    if buy.get("new_position"):
        return NEW_POSITION
    sc = buy.get("stake_change")
    return None if sc is None else sc / 100


def check(state, ticker, day):
    """Return a signal dict if a cluster just formed for `ticker` AND it is a conviction cluster, else None.
    Every newly formed 3+ cluster is remembered (state['clusters_judged']) so it is judged only once."""
    d = datetime.fromisoformat(day).date()
    lo = (d - timedelta(days=WINDOW_DAYS)).isoformat()
    window = [b for b in state["buys"]
              if b["ticker"] == ticker and lo <= b["date"] <= day and b["value"] >= MIN_EACH]
    per = {}
    for b in sorted(window, key=lambda b: b["date"]):
        p = per.setdefault(b["name"], {"name": b["name"], "role": b.get("role", ""), "value": 0.0,
                                       "stake": None, "url": b.get("url"), "date": b["date"],
                                       "price": b.get("price")})
        p["value"] += b["value"]
        s = b.get("stake")
        if s is not None:
            p["stake"] = s if p["stake"] is None else max(p["stake"], s)
    if len(per) < MIN_INSIDERS:
        return None
    judged = state.setdefault("clusters_judged", {})
    last = judged.get(ticker)
    if last and (d - datetime.fromisoformat(last).date()).days <= COOLDOWN_DAYS:
        return None
    judged[ticker] = day
    insiders = sorted(per.values(), key=lambda p: -p["value"])
    total = sum(p["value"] for p in insiders)
    stakes = [p["stake"] for p in insiders if p["stake"] is not None]
    med = st.median(stakes) if stakes else None
    big, doubled = total >= BIG_TOTAL, med is not None and med >= DOUBLED
    if not (big or doubled):
        return None
    return {"ticker": ticker, "day": day, "insiders": insiders, "total": total, "median_stake": med,
            "why": "big" if big else "doubled", "why_both": big and doubled}


# ---------------------------------------------------------------- prices

def prices(ticker, rng="3mo"):
    """[(date, open, close, volume)] from Yahoo, oldest first."""
    for i in range(3):
        try:
            r = requests.get(PRICE_URL.format(t=ticker.replace(".", "-")),
                             params={"range": rng, "interval": "1d"}, headers={"User-Agent": UA}, timeout=20)
            if r.status_code == 429:
                time.sleep(5 + 5 * i)
                continue
            res = (r.json().get("chart", {}).get("result") or [None])[0]
            if not res:
                return []
            q = (res["indicators"]["quote"] or [{}])[0]
            out = []
            for k, t in enumerate(res.get("timestamp") or []):
                o, c, v = q["open"][k], q["close"][k], (q.get("volume") or [0] * (k + 1))[k] or 0
                if o and c:
                    out.append((datetime.fromtimestamp(t, timezone.utc).date(), o, c, v))
            return out
        except Exception:
            time.sleep(2)
    return []


def liquidity(ticker):
    rows = prices(ticker, "1mo")[-20:]
    if not rows:
        return None
    return st.mean(c * v for _, _, c, v in rows)


def liq_label(dv):
    if dv is None:
        return "💧 Liquidity unknown"
    if dv >= 1e7:
        return f"💧 Liquid (${dv / 1e6:,.0f}M traded/day)"
    if dv >= 1e6:
        return f"💧 Liquid (${dv / 1e6:,.1f}M traded/day)"
    return f"⚠️ Thin trading (${dv / 1e3:,.0f}K/day) — use limit orders"


def add_trading_days(d, n):
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


# ---------------------------------------------------------------- Discord

def post(webhook, embed=None, content=None):
    payload = {"username": "Insider Radar"}
    if embed:
        payload["embeds"] = [embed]
    if content:
        payload["content"] = content
    for _ in range(5):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return


def money(v):
    return f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}K"


def post_alert(webhook, sig, buy):
    """Post the alert to #conviction-alerts and log the signal."""
    dv = liquidity(sig["ticker"])
    ny_today = (datetime.now(timezone.utc) - timedelta(hours=5)).date()     # New York date (close enough)
    entry_day = add_trading_days(ny_today, 1)                               # first open after the alert
    sell_day = add_trading_days(entry_day, HOLD)
    lines = []
    for p in sig["insiders"][:6]:
        stake = ""
        if p["stake"] is not None:
            stake = " · new position" if p["stake"] >= NEW_POSITION else f" · stake +{p['stake'] * 100:.0f}%"
        link = f" · [filing]({p['url']})" if p.get("url") else ""
        lines.append(f"• **{p['name']}** ({p['role'] or 'Insider'}) — {money(p['value'])}{stake}{link}")
    if len(sig["insiders"]) > 6:
        lines.append(f"• …and {len(sig['insiders']) - 6} more")
    why = []
    if sig["total"] >= BIG_TOTAL:
        why.append(f"they bet **{money(sig['total'])}** together")
    if sig["median_stake"] is not None and sig["median_stake"] >= DOUBLED:
        why.append("the typical insider **at least doubled** their stake")
    drop = buy.get("drop")
    facts = [liq_label(dv)]
    if drop is not None:
        facts.append(f"📉 {drop * 100:.0f}% below its 1-year high")
    exch = f" ({buy['exchange']})" if buy.get("exchange") else ""
    embed = {
        "title": f"🎯 CONVICTION CLUSTER — ${sig['ticker']}",
        "color": COLOR,
        "description": (
            f"**{len(sig['insiders'])} insiders of {buy.get('company', sig['ticker'])}{exch}** bought their own stock "
            f"within 7 days, and " + " and ".join(why) + ".\n\n" + "\n".join(lines) + "\n\n" + " · ".join(facts) +
            f"\n\n📋 **Strategy:** buy at the next open ({entry_day:%a %b %d}) · ⏰ sell reminder on {sell_day:%a %b %d}\n"
            "Backtest 2006–2026: avg +1.5% vs S&P in 5 days, about half of signals win. How it works → <#1567258289979396206>"),
        "footer": {"text": "Insider Radar · Conviction Clusters · Source: SEC Form 4 · Not financial advice"},
    }
    post(webhook, embed)
    data = load_log()
    data["signals"].append({"ticker": sig["ticker"], "company": buy.get("company"), "day": sig["day"],
                            "alerted": date.today().isoformat(), "entry_day": entry_day.isoformat(),
                            "sell_day": sell_day.isoformat(), "insiders": len(sig["insiders"]),
                            "total": round(sig["total"]), "liquidity": round(dv) if dv else None,
                            "status": "open"})
    save_log(data)


# ---------------------------------------------------------------- follow-up (scheduled)

def result(sig):
    """Fill entry/exit prices and the result vs SPY once 5 trading days have closed. Returns True if done."""
    entry = datetime.fromisoformat(sig["entry_day"]).date()
    rows, spy = prices(sig["ticker"]), prices("SPY")
    if not rows or not spy:
        return False
    i = next((k for k, r in enumerate(rows) if r[0] >= entry), None)
    j = next((k for k, r in enumerate(spy) if r[0] == (rows[i][0] if i is not None else None)), None)
    if i is None or j is None or i + HOLD >= len(rows) or j + HOLD >= len(spy):
        return False
    stock = rows[i + HOLD][2] / rows[i][1] - 1
    bench = spy[j + HOLD][2] / spy[j][1] - 1
    sig.update({"entry_day": rows[i][0].isoformat(), "exit_day": rows[i + HOLD][0].isoformat(),
                "entry": rows[i][1], "exit": rows[i + HOLD][2], "ret": stock, "spy": bench,
                "excess": stock - bench, "status": "closed"})
    return True


def totals(signals):
    done = [s for s in signals if s.get("status") == "closed"]
    if not done:
        return None
    ex = [s["excess"] for s in done]
    return {"n": len(done), "wins": sum(x > 0 for x in ex), "avg": st.mean(ex), "sum_ret": st.mean(s["ret"] for s in done)}


def totals_line(t):
    return (f"**Running record:** {t['n']} signal{'s' if t['n'] != 1 else ''} · {t['wins']} beat the S&P "
            f"({t['wins'] / t['n'] * 100:.0f}%) · avg **{t['avg'] * 100:+.2f}% vs S&P** per signal")


def run():
    remind_hook = os.environ.get("WEBHOOK_CONVICTION", "").strip()
    record_hook = os.environ.get("WEBHOOK_CONV_RECORD", "").strip()
    mode = os.environ.get("MODE", "auto")
    now = datetime.now(timezone.utc)
    if mode == "auto":
        mode = "remind" if now.hour < 18 else "results"
    data = load_log()
    today = now.date()
    changed = False

    if mode == "remind" and remind_hook:
        for s in data["signals"]:
            if s["status"] == "open" and s.get("sell_day") == today.isoformat() and not s.get("reminded"):
                post(remind_hook, content=(f"⏰ **SELL DAY — ${s['ticker']}** (Conviction Cluster from {s['alerted']}). "
                                           "The strategy exits at today's close. Result goes to <#1567258509056417882> tonight."))
                s["reminded"] = True
                changed = True

    if mode == "results":
        for s in data["signals"]:
            if s["status"] == "open" and today.isoformat() >= s.get("sell_day", "9999") and result(s):
                changed = True
                t = totals(data["signals"])
                mark = "✅ beat the market" if s["excess"] > 0 else "❌ lagged the market"
                if record_hook:
                    post(record_hook, content=(
                        f"**${s['ticker']}** — bought {s['entry_day']} at ${s['entry']:,.2f}, sold {s['exit_day']} at "
                        f"${s['exit']:,.2f}\nStock **{s['ret'] * 100:+.2f}%** vs S&P {s['spy'] * 100:+.2f}% → "
                        f"{mark} by **{s['excess'] * 100:+.2f}%**\n{totals_line(t)}"))
        if today.weekday() == 4 and record_hook and data.get("last_weekly") != today.isoformat():
            t = totals(data["signals"])
            open_n = sum(s["status"] == "open" for s in data["signals"])
            week = [s for s in data["signals"] if s.get("status") == "closed"
                    and s["exit_day"] >= (today - timedelta(days=6)).isoformat()]
            body = ["📊 **Weekly update — Conviction Clusters**"]
            if week:
                body += [f"This week: " + ", ".join(f"${s['ticker']} {s['excess'] * 100:+.1f}%" for s in week)]
            else:
                body += ["No signals closed this week."]
            body += [totals_line(t) if t else "No closed signals yet — the live test just started.",
                     f"Open signals: {open_n}", "Backtest expectation: about +1.5% vs S&P per signal, ~50% win rate."]
            post(record_hook, content="\n".join(body))
            data["last_weekly"] = today.isoformat()
            changed = True

    if changed:
        save_log(data)
    print(f"mode={mode} signals={len(data['signals'])} changed={changed}")


if __name__ == "__main__":
    run()
