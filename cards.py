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
FOOT = "Not financial advice"
AVATAR = "https://raw.githubusercontent.com/vincaswick-ai/insider-radar/main/logo.png"


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

def post_card(webhook, content, embed, name=None, image=None):
    """Send the embed as a Discord message. Markdown links found in `content` go into the embed.
    image = PNG bytes shown inside the card (weekly chart)."""
    links = re.findall(r"\[([^\]]+)\]\(<?([^)>]+)>?\)", content or "")
    if links:
        if not embed.get("url"):
            embed["url"] = links[0][1]
        line = " · ".join(f"[{t}]({u})" for t, u in links)
        embed["fields"] = embed.get("fields", []) + [f("🔗 Links", line, False)]
    if image:
        embed["image"] = {"url": "attachment://chart.png"}
    payload = {"username": "Insider Radar", "avatar_url": AVATAR, "embeds": [embed], "allowed_mentions": {"parse": []}}
    for _ in range(5):
        if image:
            r = requests.post(webhook, data={"payload_json": json.dumps(payload)},
                              files={"files[0]": ("chart.png", image, "image/png")}, timeout=60)
        else:
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


# ---------------------------------------------------------------- shared pieces (one format everywhere)

def header(ticker, exchange=None, sp500=False, icon="💼"):
    """'💼 $HGV (NYSE) · ⭐ S&P 500'"""
    return f"{icon} ${ticker}" + (f" ({exchange})" if exchange else "") + (" · ⭐ S&P 500" if sp500 else "")


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def track_line(rec):
    """🔁 Track record line from score.track_record() output (or None)."""
    if not rec:
        return "🔁 **Track record:** no earlier open-market buys of this stock on file."
    nth = ordinal(rec["count"] + 1)
    since = rec["first"].strftime("%b %Y") if hasattr(rec["first"], "strftime") else str(rec["first"])
    if rec.get("ret") is not None:
        last = rec["last"].strftime("%b %d, %Y") if hasattr(rec["last"], "strftime") else str(rec["last"])
        return (f"🔁 **Track record:** their last buy ({last}) is **{rec['ret'] * 100:+.0f}%** since. "
                f"This is their **{nth} buy** since {since}.")
    return f"🔁 **Track record:** this is their **{nth} buy** since {since}."


def stake_text(stake_change, new_position):
    if new_position:
        return "🆕 **New position**"
    if stake_change is None:
        return "—"
    return f"📈 **+{stake_change:.0f}%**" + ("\nmore than doubled" if stake_change >= 100 else "")


def high_text(drop):
    if drop is None:
        return "—"
    if drop < 0.02:
        return "🟢 **at** its 1-year high"
    return f"🔻 **−{drop * 100:.0f}%**\nbelow its 1-year high"


def cap_text(market_cap, size):
    return f"**{money(market_cap)}**\n{size}" if market_cap else "—"


def owns_text(owned, price):
    return f"{owned:,.0f} sh\n≈ {money(owned * price)}" if owned else "—"


# ---------------------------------------------------------------- 1. insider buy (#insider-buys, #big-caps)

def insider_card(d, icon="💼", color=BLUE, label=None):
    """The standard alert. d: ticker, exchange, sp500, company, name, role, value, shares, price, date_text,
    stake_change, new_position, owned_after, drop, market_cap, size, record, others, plan, industry, tags."""
    own = "" if d.get("big_holder") else " with their own money"
    desc = (f"**{d['name']}** ({d.get('role') or 'Insider'}) bought **{money(d['value'])}** of "
            f"**{d['company']}** stock{own}.\n\n" + track_line(d.get("record")))
    fields = [
        f("Amount", f"**${d['value']:,.0f}**\n{d['shares']:,.0f} sh @ ${d['price']:,.2f}"),
        f("Bought", f"{d.get('date_text') or '—'}\nopen market"),
        f("Now owns", owns_text(d.get("owned_after"), d["price"])),
        f("Stake change", stake_text(d.get("stake_change"), d.get("new_position"))),
        f("vs 1-year high", high_text(d.get("drop"))),
        f("Market cap", cap_text(d.get("market_cap"), d.get("size"))),
    ]
    notes = []
    if d.get("others"):
        notes.append(f"👥 {d['others']} other insider{'s' if d['others'] != 1 else ''} bought this stock in the last 14 days")
    if d.get("plan"):
        notes.append("🗓 Pre-planned trade (Rule 10b5-1), so it says less")
    for t in d.get("tags") or []:
        notes.append({"cluster": "👥 Part of a **cluster** · see <#1567837534740815892>",
                      "conviction": "🔥 Triggered the **Clusters Strategy** · see <#1567258350054412429>",
                      "event": "📅 Bought **before a known event** · see <#1567621546644602940>"}.get(t, ""))
    notes = [n for n in notes if n]
    if notes:
        fields.append(f("\u200b", "\n".join(notes), False))
    foot = " · ".join(x for x in [label, d.get("industry"), "SEC Form 4", FOOT] if x)
    return {"color": color, "title": header(d["ticker"], d.get("exchange"), d.get("sp500"), icon),
            "description": desc, "fields": fields, "footer": {"text": foot}}


def buy_card(d):
    return insider_card(d)


def bigcap_card(d):
    return insider_card(d, icon="🏦", color=GREEN, label="Big cap")


# ---------------------------------------------------------------- 2. clusters (#clusters) + strategy (#clusters-strategy)

def _insider_lines(people, n=6):
    lines = []
    for p in people[:n]:
        st = p.get("stake_text")
        lines.append(f"**{p['name']}** ({p.get('role') or 'Insider'}) · **{money(p['value'])}**"
                     + (f" · {p['date']}" if p.get("date") else "") + (f" · {st}" if st else ""))
    if len(people) > n:
        lines.append(f"…and {len(people) - n} more")
    return "\n".join(lines)


def cluster_card(d):
    """d: ticker, exchange, sp500, company, insiders[{name, role, value, date, stake_text}], total, days,
    drop, market_cap, size, industry"""
    fields = [
        f("👥 Who bought", _insider_lines(d["insiders"]), False),
        f("Insiders", f"**{len(d['insiders'])}**\nin {d['days']} day{'s' if d['days'] != 1 else ''}"),
        f("Total", f"**{money(d['total'])}**\nopen market"),
        f("Avg price", f"${d['avg_price']:,.2f}" if d.get("avg_price") else "—"),
        f("vs 1-year high", high_text(d.get("drop"))),
        f("Market cap", cap_text(d.get("market_cap"), d.get("size"))),
        blank(),
    ]
    return {"color": ORANGE, "title": header(d["ticker"], d.get("exchange"), d.get("sp500"), "👥"),
            "description": f"**{len(d['insiders'])} different insiders** at **{d['company']}** bought stock "
                           f"within 14 days, **{money(d['total'])}** in total.",
            "fields": fields,
            "footer": {"text": " · ".join(x for x in ["Cluster buy", d.get("industry"), "SEC Form 4", FOOT] if x)}}


def conviction_card(d):
    """Clusters Strategy signal. Same layout as a cluster, plus the trade plan and live results."""
    s = d.get("stats")
    fields = [
        f("👥 Who bought", _insider_lines(d["insiders"]), False),
        f("Insiders", f"**{d['n']}**\nin {d['days']} day{'s' if d['days'] != 1 else ''}"),
        f("Total", f"**{money(d['total'])}**\nopen market"),
        f("vs 1-year high", high_text(d.get("drop"))),
        f("📋 Entry", f"Next open\n{d['entry']}"), f("Exit", f"Close {d['exit']}\n5 trading days"),
        f("Market cap", cap_text(d.get("market_cap"), d.get("size"))),
    ]
    if s:
        fields += [f("📊 Live signals", str(s["n"])), f("Beat the S&P", f"{s['wins']} / {s['n']}"),
                   f("Avg vs S&P", f"{dot(s['avg'])} **{pct(s['avg'])}**")]
    else:
        fields.append(f("📊 Strategy so far", "First live signals. The result is posted here after 5 trading days.", False))
    return {
        "color": ORANGE,
        "title": header(d["ticker"], d.get("exchange"), d.get("sp500"), "🔥") + f" · SIGNAL #{d['number']:03d}",
        "description": (f"**{d['n']} insiders** at **{d.get('company') or d['ticker']}** bought **{money(d['total'])}** "
                        "within 7 days. Our tested rule: 3+ insiders with big money or big stake increases."),
        "fields": fields,
        "footer": {"text": "Clusters Strategy · every signal is tracked, wins and losses · " + FOOT},
    }


# ---------------------------------------------------------------- 3. result + weekly (#clusters-strategy)

def result_card(d):
    t = d["totals"]
    return {
        "color": GREEN if d["excess"] > 0 else RED,
        "author": {"name": f"📊 SIGNAL RESULT · CLUSTERS STRATEGY #{d['number']:03d}"},
        "title": f"${d['ticker']} closed: {pct(d['ret'])} vs S&P {pct(d['spy'])}",
        "fields": [
            f(f"Entry · {d['entry_day']}", f"${d['entry']:,.2f}"), f(f"Exit · {d['exit_day']}", f"${d['exit']:,.2f}"),
            f("Result", f"{'✅' if d['excess'] > 0 else '❌'} **{pct(d['ret'])}**\n{'beat' if d['excess'] > 0 else 'lagged'} S&P by **{abs(d['excess']) * 100:.1f} pts**"),
            f("🧮 Signals", f"**{t['n']}**"), f("Beat the S&P", f"**{t['wins']} / {t['n']}**"),
            f("Avg vs S&P", f"{dot(t['avg'])} **{pct(t['avg'])}**"),
        ],
        "footer": {"text": "Every signal counts, wins and losses · Not financial advice"},
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
        "author": {"name": "📊 WEEKLY UPDATE · CLUSTERS STRATEGY"},
        "title": "This week in the strategy",
        "fields": fields,
        "footer": {"text": "Backtest: about +1.5% vs S&P per signal, ~50% win rate · Not financial advice"},
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
        "footer": {"text": "STOCK Act disclosure · amounts are ranges · Not financial advice"},
    }


# ---------------------------------------------------------------- 5. buys before events (#buys-before-events)

def case_card(d):
    """Insider card + what is coming next. d = insider_card fields + number, events[{when, text}], market."""
    card = insider_card(d, icon="📅", color=YELLOW, label=f"Case #{d['number']:03d}")
    card["title"] += f" · CASE #{d['number']:03d}"
    nxt = "\n".join(f"⏳ **{e['when']}** · {e['text']}" for e in d["events"][:3])
    extra = [f("📅 What's coming", nxt, False)]
    if d.get("market"):
        m = d["market"]
        extra.append(f("🎲 Market odds", f"*{m['question']}*\n**{m['yes_label']} {m['yes'] * 100:.0f}%** {bar(m['yes'])} "
                                         f"{m['no'] * 100:.0f}% {m['no_label']} · on {m['when']}", False))
    # keep the 6 standard fields first, then the event block, then notes
    std, rest = card["fields"][:6], card["fields"][6:]
    card["fields"] = std + extra + rest
    card["footer"]["text"] = f"Case #{d['number']:03d} · we show what's coming next, not why they bought · " + FOOT
    return card


def case_update_card(d):
    return {
        "color": GREEN if d["ret"] >= 0 else RED,
        "author": {"name": f"✅ CASE #{d['number']:03d} · RESULT"},
        "title": f"The event happened: {d['event']}",
        "fields": [
            f("Insider's price", f"${d['buy_price']:,.2f}"), f("Now", f"${d['now_price']:,.2f}"),
            f("Since the buy", f"{dot(d['ret'])} **{pct(d['ret'])}** in {d['days']} days"
                               + (f"\nS&P {pct(d['spy'])}" if d.get("spy") is not None else "")),
        ] + ([f("Note", d["note"], False)] if d.get("note") else []),
        "footer": {"text": "Every case gets a result, good or bad · Not financial advice"},
    }


# ---------------------------------------------------------------- 6. top investors (#top-investors)

def investor_card(d):
    """d: who, fund, period, filed, total, n_positions, new[(ticker, name, value, pct)], added[...], cut[...], sold[...],
    top[(ticker, name, value, pct)], url"""
    def rows(items, sign=""):
        out = []
        for tk, nm, val, p in items[:6]:
            out.append(f"**{('$' + tk) if tk else nm}** · {money(val)}" + (f" · {sign}{p}" if p else ""))
        if len(items) > 6:
            out.append(f"…and {len(items) - 6} more")
        return "\n".join(out) or "—"
    fields = [f("Portfolio", f"**{money(d['total'])}**\n{d['n_positions']} stocks"),
              f("Quarter", d["period"]), f("Filed", f"{d['filed']}\nSEC 13F")]
    if d["new"]:
        fields.append(f("🆕 New positions", rows(d["new"]), False))
    if d["added"]:
        fields.append(f("📈 Added to", rows(d["added"], "+"), False))
    if d["cut"]:
        fields.append(f("📉 Cut", rows(d["cut"]), False))
    if d["sold"]:
        fields.append(f("❌ Sold everything", rows(d["sold"]), False))
    fields.append(f("🏆 Top holdings", rows(d["top"]), False))
    return {"color": PURPLE, "title": f"🧠 {d['who']}",
            "description": f"**{d['fund']}** filed its 13F for **{d['period']}**. "
                           "Reported up to 45 days after the quarter ends, so these moves are not live.",
            "fields": fields, "url": d.get("url"),
            "footer": {"text": "SEC Form 13F · US stocks only, no options/shorts · " + FOOT}}
