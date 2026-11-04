"""
Insider Radar message cards: real Discord messages (embeds), laid out like the approved mockups.

Each *_card() returns an embed dict. post_card() sends it as a normal Discord message:
links in `content` ([text](<url>)) are moved inside the embed, so the message is just the card.
"""

import json
import re
import time

import requests

GREEN, RED, BLUE, YELLOW, PURPLE, ORANGE = 0x57F287, 0xED4245, 0x00A8FC, 0xFEE75C, 0x9B59B6, 0xF0B232
FOOT = "Insider Radar · Not financial advice"


def esc(x):
    """Plain text for Discord markdown (kept for callers that used to escape HTML)."""
    return str(x if x is not None else "").replace("*", "\\*").replace("_", "\\_")


def money(v):
    if v is None:
        return "n/a"
    v = float(v)
    if v >= 1e9:
        return f"${v / 1e9:,.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:,.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:,.0f}K"
    return f"${v:,.0f}"


def pct(x, plus=True):
    return "n/a" if x is None else (f"{x * 100:+.1f}%" if plus else f"{x * 100:.1f}%")


def dot(x):
    return "🟢" if (x or 0) >= 0 else "🔴"


def f(name, value, inline=True):
    return {"name": name, "value": (str(value) or "—")[:1024], "inline": inline}


def blank():
    return {"name": "​", "value": "​", "inline": True}


def bar(yes, n=10):
    k = max(0, min(n, round(yes * n)))
    return "🟦" * k + "🟥" * (n - k)


# ---------------------------------------------------------------- posting

def post_card(webhook, content, embed, name=None):
    """Send the embed as a Discord message. Markdown links found in `content` go into the embed."""
    links = re.findall(r"\[([^\]]+)\]\(<?([^)>]+)>?\)", content or "")
    if links:
        if not embed.get("url"):
            embed["url"] = links[0][1]
        line = " · ".join(f"[{t}]({u})" for t, u in links)
        embed["fields"] = embed.get("fields", []) + [f("🔗 Links", line, False)]
    payload = {"username": "Insider Radar", "embeds": [embed], "allowed_mentions": {"parse": []}}
    for _ in range(5):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.status_code >= 400:
            print(f"  Discord rejected the card: {r.status_code} {r.text[:300]}", flush=True)
        r.raise_for_status()
        time.sleep(1)
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


def _tags(tags):
    out = []
    for t in tags or []:
        out.append("🔥 CONVICTION" if t == "conviction" else "🔎 CASE FILE" if t == "case" else "")
    return "  ·  ".join(x for x in out if x)


# ---------------------------------------------------------------- 1. insider buy (#all-buys)

def buy_card(d):
    head = " · ".join(x for x in ["💼 INSIDER BUY", (d.get("industry") or "").upper(), d.get("exchange")] if x)
    tags = _tags(d.get("tags"))
    who_short = d.get("role_short") or "insider"
    return {
        "color": BLUE,
        "author": {"name": head + (f"  ·  {tags}" if tags else "")},
        "title": f"${d['ticker']} · {d['company_short']}'s {who_short} buys {money(d['value'])} of stock",
        "fields": [
            f("Who", f"{d['name']}\n*{d.get('role') or ''}*"),
            f("Amount", f"**${d['value']:,.0f}**\n{d['shares']:,.0f} sh @ ${d['price']:,.2f}"),
            f("Bought", f"{d.get('date_text') or ''} · own money" + (f"\n{d['stake_text']}" if d.get("stake_text") else "")),
            f("💡 What this means", d["meaning"], False),
        ],
        "footer": {"text": f"Insider Radar · Source: SEC Form 4 · Not financial advice"
                           + (f"  |  {d['footer_right']}" if d.get("footer_right") else "")},
    }


# ---------------------------------------------------------------- 2. conviction signal (#conviction)

def conviction_card(d):
    lines = [f"**{p['name']}** · {p.get('role') or 'Insider'} · **{money(p['value'])}**"
             + (f" · {p['stake_text']}" if p.get("stake_text") else "") for p in d["insiders"][:6]]
    if len(d["insiders"]) > 6:
        lines.append(f"…and {len(d['insiders']) - 6} more")
    s = d.get("stats")
    fields = [f("👥 Who bought", "\n".join(lines), False),
              f("📋 Entry", f"Next open · {d['entry']}"), f("Exit", f"Close · {d['exit']}\n*(5 trading days)*"),
              f("Benchmark", "vs S&P 500")]
    if s:
        fields += [f("📊 Live signals", str(s["n"])), f("Beat the S&P", f"{s['wins']} / {s['n']}"),
                   f("Avg vs S&P", f"{dot(s['avg'])} **{pct(s['avg'])}**")]
    else:
        fields.append(f("📊 Strategy so far", "First live signals. Results appear in #track-record after 5 trading days.", False))
    return {
        "color": ORANGE,
        "author": {"name": f"🔥 CONVICTION CLUSTER · SIGNAL #{d['number']:03d}"},
        "title": f"${d['ticker']} · {d['n']} insiders bought {money(d['total'])} in {d['days']} day{'s' if d['days'] != 1 else ''}",
        "description": "Our tested rule just triggered: **3+ insiders** buying within 7 days, with big money or big stake increases."
                       + (f"\n*{d['company']}*" if d.get("company") else ""),
        "fields": fields,
        "footer": {"text": "Tracked live, results in #track-record · Not financial advice  |  Signal open 🟠"},
    }


# ---------------------------------------------------------------- 3. result + weekly (#track-record)

def result_card(d):
    t = d["totals"]
    return {
        "color": GREEN if d["excess"] > 0 else RED,
        "author": {"name": f"📊 SIGNAL RESULT · CONVICTION #{d['number']:03d}"},
        "title": f"${d['ticker']} closed: {pct(d['ret'])} vs S&P {pct(d['spy'])}",
        "fields": [
            f(f"Entry · {d['entry_day']}", f"${d['entry']:,.2f}"), f(f"Exit · {d['exit_day']}", f"${d['exit']:,.2f}"),
            f("Result", f"{dot(d['ret'])} **{pct(d['ret'])}**\n{'beat' if d['excess'] > 0 else 'lagged'} S&P by **{abs(d['excess']) * 100:.1f} pts**"),
            f("🧮 Signals", f"**{t['n']}**"), f("Beat the S&P", f"**{t['wins']} / {t['n']}**"),
            f("Avg vs S&P", f"{dot(t['avg'])} **{pct(t['avg'])}**"),
        ],
        "footer": {"text": f"Every signal is counted, wins and losses · Not financial advice  |  Signal closed {'✅' if d['excess'] > 0 else '❌'}"},
    }


def weekly_card(d):
    t = d.get("totals")
    week = "\n".join(f"{dot(x)} **${tk}** {pct(x)} vs S&P" for tk, x in d["week"]) or "No signals closed this week."
    fields = [f("This week", week, False)]
    if t:
        fields += [f("🧮 Signals", f"**{t['n']}**"), f("Beat the S&P", f"**{t['wins']} / {t['n']}**"),
                   f("Avg vs S&P", f"{dot(t['avg'])} **{pct(t['avg'])}**")]
    else:
        fields.append(f("🧮 Running total", "No closed signals yet. The live test just started.", False))
    fields.append(f("Open signals", str(d["open_n"])))
    return {
        "color": BLUE,
        "author": {"name": "📊 WEEKLY UPDATE · CONVICTION CLUSTERS"},
        "title": "This week in the strategy",
        "fields": fields,
        "footer": {"text": "Backtest expectation: about +1.5% vs S&P per signal, ~50% win rate · Not financial advice"},
    }


# ---------------------------------------------------------------- 4. politician trade (#politicians)

def politician_card(d):
    lines = []
    for t in d["trades"][:8]:
        icon = "🟢" if t["word"] == "BUY" else "🔴" if t["word"] == "SELL" else "⚪"
        line = f"{icon} **{t['word']}** ${t['ticker']} · {t['amount']}" + (f" · {t['date']}" if t.get("date") else "")
        if t.get("move") is not None:
            line += f" · since **{pct(t['move'])}**"
        lines.append(line + (" ⚠️" if t.get("match") else ""))
    if len(d["trades"]) > 8:
        lines.append(f"…and {len(d['trades']) - 8} more in the filing")
    tickers = list(dict.fromkeys(t["ticker"] for t in d["trades"]))
    words = {t["word"] for t in d["trades"]}
    verb = "bought" if words == {"BUY"} else "sold" if words == {"SELL"} else "traded"
    delay = "—" if d.get("delay") is None else (f"{d['delay']} days after the trade"
                                               + ("\n⚠️ **past the 45-day deadline**" if d.get("late") else ""))
    fields = [f("Trades", f"{len(d['trades'])} in this filing"), f("Reported", delay),
              f("Committees", (", ".join(d.get("committees") or []) or "—") + ("\n⚠️ **SECTOR MATCH**" if d.get("match_note") else "")),
              f("📋 The trades", "\n".join(lines), False)]
    if d.get("match_note"):
        fields.append(f("⚠️ Committee match", d["match_note"], False))
    return {
        "color": PURPLE,
        "author": {"name": f"🏛 POLITICIAN TRADE · {d['chamber'].upper()} · DISCLOSED {d['filed'].upper()}"},
        "title": f"{d['label']} {verb} " + ", ".join(f"${x}" for x in tickers[:3]) + (f" +{len(tickers) - 3} more" if len(tickers) > 3 else ""),
        "fields": fields,
        "footer": {"text": "Source: STOCK Act disclosures · Congress reports amount ranges only · Not financial advice"},
    }


# ---------------------------------------------------------------- 5. case file (#case-files)

def case_card(d):
    tl = "\n".join(f"{e.get('icon', '⚪')} **{e['when']}** — {e['text']}" for e in d["timeline"])
    fields = [
        f("Who", f"{d['who']}\n*{d['role']}*"),
        f("Amount", f"**${d['value']:,.0f}**\n{d['shares']:,.0f} sh @ ${d['price']:,.2f}"),
        f("Type", "Open-market buy (P)\nown money"),
        f("Stock before buy", d.get("drop_text") or "—"),
        f("Company", d.get("company_desc") or "—"),
        f("Filed", f"{d['filed']} · SEC Form 4"),
        f("🧩 Why now? The connections", tl, False),
    ]
    if d.get("scenarios"):
        up, down = d["scenarios"]
        fields += [f(f"🟢 {up[0]}", up[1]), f(f"🔴 {down[0]}", down[1]), blank()]
    if d.get("market"):
        m = d["market"]
        fields.append(f("🎲 What the market expects",
                        f"Polymarket · *\"{m['question']}\"*\n**{m['yes_label']} {m['yes'] * 100:.0f}%** {bar(m['yes'])} "
                        f"{m['no'] * 100:.0f}% {m['no_label']}\n-# Odds when this case opened ({m['when']}). They change live: "
                        + (f"[see current odds]({m['url']})" if m.get("url") else "see link below"), False))
    return {
        "color": YELLOW,
        "author": {"name": " · ".join(x for x in [f"🔎 CASE FILE #{d['number']:03d}", (d.get("sector") or "").upper(), d.get("exchange")] if x)},
        "title": d["title"],
        "description": d["summary"],
        "fields": fields,
        "footer": {"text": "We don't know why insiders buy. We show what's coming next. · Not financial advice  |  Case open 🟡"},
    }


def case_update_card(d):
    return {
        "color": GREEN if d["ret"] >= 0 else RED,
        "author": {"name": f"✅ CASE FILE #{d['number']:03d} · UPDATE"},
        "title": f"The event happened: {d['event']}",
        "fields": [
            f("Insider's price", f"${d['buy_price']:,.2f}"), f("Now", f"${d['now_price']:,.2f}"),
            f("Since the buy", f"{dot(d['ret'])} **{pct(d['ret'])}** in {d['days']} days"
                               + (f"\nS&P {pct(d['spy'])}" if d.get("spy") is not None else "")),
        ] + ([f("Note", d["note"], False)] if d.get("note") else []),
        "footer": {"text": "Every case gets a result, good or bad · Not financial advice  |  Case closed"},
    }
