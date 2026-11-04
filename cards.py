"""
Insider Radar message cards.

Every Discord alert is a short text line (with clickable links) plus one PNG card
that looks like the approved mockups. Cards are HTML rendered by the Chrome that is
already installed on GitHub's runners (Playwright, channel="chrome").

If rendering ever fails, post_card() still posts the text line, so no alert is lost.
"""

import atexit
import html
import json
import time

import requests

_pw = None
_browser = None

GREEN, RED, BLUE, YELLOW, PURPLE, ORANGE = "#57f287", "#ed4245", "#00a8fc", "#fee75c", "#9b59b6", "#f0b232"

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#313338;font-family:Inter,"Noto Sans",sans-serif;color:#dbdee1;width:820px;padding:14px}
.embed{background:#2b2d31;border-left:4px solid var(--c);border-radius:4px;padding:14px 18px 16px}
.au{font-size:13px;color:#b5bac1;font-weight:700;letter-spacing:.02em;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.t{font-size:20px;font-weight:800;color:#fff;margin:6px 0 4px;line-height:1.25}
.d{font-size:15px;line-height:1.5}
.h{font-size:12.5px;font-weight:800;color:#fff;letter-spacing:.06em;margin:16px 0 6px;text-transform:uppercase}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px 18px;margin-top:8px}
.f .k{font-size:12.5px;font-weight:700;color:#fff}.f .v{font-size:14.5px;margin-top:2px;line-height:1.4}
.g{color:#57f287}.r{color:#ed4245}.y{color:#fee75c}.b{color:#00a8fc}.m{color:#949ba4}
.tl{border-left:2px solid #3f4147;margin-left:6px;padding-left:16px}
.ev{position:relative;margin:10px 0}
.ev:before{content:"";position:absolute;left:-23px;top:5px;width:12px;height:12px;border-radius:50%;background:var(--dc,#4e5058);border:2px solid #2b2d31}
.ev .w{font-size:12.5px;color:#949ba4;font-weight:700;text-transform:uppercase}
.ev .x{font-size:15px;line-height:1.45}
.bx{background:#1e1f22;border-radius:6px;padding:10px 14px;margin-top:8px;font-size:14.5px;line-height:1.5}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px}
.foot{font-size:12px;color:#949ba4;margin-top:14px;display:flex;justify-content:space-between;gap:20px}
.chip{display:inline-block;font-size:11px;font-weight:800;padding:2px 7px;border-radius:4px;letter-spacing:.03em}
.c-conv{background:#f0b232;color:#000}.c-case{background:#fee75c;color:#000}.c-match{background:#fee75c;color:#000}
table{border-collapse:collapse;margin-top:4px}td,th{padding:4px 14px 4px 0;font-size:14.5px;text-align:left}th{font-size:12px;color:#949ba4}
.odds{display:flex;align-items:center;gap:12px;margin-top:8px}
.ob{flex:1;height:14px;border-radius:7px;background:#ed4245;overflow:hidden}.ob i{display:block;height:100%;background:#00a8fc}
.big{font-size:28px;font-weight:800}
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


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


def cls(x):
    return "g" if (x or 0) >= 0 else "r"


# ---------------------------------------------------------------- rendering

def _browser_get():
    global _pw, _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        try:
            _browser = _pw.chromium.launch(channel="chrome")       # Chrome preinstalled on GitHub runners
        except Exception:
            _browser = _pw.chromium.launch()                        # Playwright's own Chromium (local runs)
        atexit.register(_close)
    return _browser


def _close():
    global _pw, _browser
    try:
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _browser = _pw = None


def render(body_html):
    """HTML fragment -> PNG bytes (2x for sharp text on phones)."""
    page = _browser_get().new_page(viewport={"width": 820, "height": 400}, device_scale_factor=2)
    try:
        page.set_content(f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
                         f"<body>{body_html}</body></html>")
        page.wait_for_timeout(50)
        return page.locator(".embed").screenshot(type="png")
    finally:
        page.close()


def post_card(webhook, content, body_html, name="card.png"):
    """Post the text line + the rendered card. Falls back to text only if rendering fails."""
    png = None
    try:
        png = render(body_html)
    except Exception as e:
        print(f"  card render failed, posting text only: {e}", flush=True)
    payload = {"username": "Insider Radar", "content": content[:1990],
               "allowed_mentions": {"parse": []}}
    for _ in range(5):
        if png:
            r = requests.post(webhook, data={"payload_json": json.dumps(payload)},
                              files={"files[0]": (name, png, "image/png")}, timeout=30)
        else:
            r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        time.sleep(1)
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


def _embed(color, inner):
    return f'<div class="embed" style="--c:{color}">{inner}</div>'


def _tags(tags):
    out = []
    for t in tags or []:
        if t == "conviction":
            out.append('<span class="chip c-conv">🔥 CONVICTION</span>')
        elif t == "case":
            out.append('<span class="chip c-case">🔎 CASE FILE</span>')
    return " ".join(out)


def _grid(cells):
    return '<div class="grid">' + "".join(
        f'<div class="f"><div class="k">{esc(k)}</div><div class="v">{v}</div></div>' for k, v in cells) + "</div>"


# ---------------------------------------------------------------- 1. insider buy (#all-buys)

def buy_card(d):
    """d: ticker, exchange, industry, company, name, role, value, shares, price, date, meaning,
          stake_text, cap, tags, sp500"""
    head = " · ".join(x for x in ["💼 INSIDER BUY", (d.get("industry") or "").upper(), d.get("exchange")] if x)
    who_short = d.get("role_short") or d.get("role") or "Insider"
    title = f"${esc(d['ticker'])} · {esc(d['company_short'])}'s {esc(who_short)} buys {money(d['value'])} of stock"
    cells = [("Who", f"{esc(d['name'])}<br><span class='m'>{esc(d.get('role') or '')}</span>"),
             ("Amount", f"<b class='g'>${d['value']:,.0f}</b><br>{d['shares']:,.0f} sh @ ${d['price']:,.2f}"),
             ("Bought", f"{esc(d.get('date_text') or '')} · own money<br>{esc(d.get('stake_text') or '')}")]
    inner = (f'<div class="au">{head} {_tags(d.get("tags"))}</div><div class="t">{title}</div>{_grid(cells)}'
             f'<div class="bx" style="margin-top:12px">💡 <b>What this means:</b> {esc(d["meaning"])}</div>'
             f'<div class="foot"><span>Insider Radar · Source: SEC Form 4 · Not financial advice</span>'
             f'<span>{esc(d.get("footer_right") or "")}</span></div>')
    return _embed(BLUE, inner)


# ---------------------------------------------------------------- 2. conviction signal (#conviction)

def conviction_card(d):
    """d: ticker, company, n, total, days, insiders[{name, role, value, stake}], entry, exit, stats{n,wins,avg}|None, number"""
    rows = "".join(
        f"<tr><td>{esc(p['name'])}</td><td>{esc(p.get('role') or 'Insider')}</td><td class='g'><b>${p['value']:,.0f}</b></td>"
        f"<td class='{'g' if p.get('stake_text', '').startswith('+') else ''}'>{esc(p.get('stake_text') or '—')}</td></tr>"
        for p in d["insiders"][:6])
    if len(d["insiders"]) > 6:
        rows += f"<tr><td class='m' colspan='4'>…and {len(d['insiders']) - 6} more</td></tr>"
    s = d.get("stats")
    stats = (_grid([("Live signals", str(s["n"])), ("Beat the S&P", f"{s['wins']} / {s['n']}"),
                    ("Avg vs S&P", f"<span class='{cls(s['avg'])}'>{pct(s['avg'])}</span>")])
             if s else '<div class="d m">First live signals. Results appear in #track-record after 5 trading days.</div>')
    inner = (f'<div class="au">🔥 CONVICTION CLUSTER · SIGNAL #{d["number"]:03d}</div>'
             f'<div class="t">${esc(d["ticker"])} · {d["n"]} insiders bought {money(d["total"])} in {d["days"]} day{"s" if d["days"] != 1 else ""}</div>'
             f'<div class="d">Our tested rule just triggered: 3+ insiders buying within 7 days, with big money or big stake increases. {esc(d.get("company") or "")}</div>'
             f'<div class="h">👥 Who bought</div><table><tr><th>Insider</th><th>Role</th><th>Amount</th><th>Stake</th></tr>{rows}</table>'
             f'<div class="h">📋 How the strategy trades it</div>'
             + _grid([("Entry", f"Next open · {esc(d['entry'])}"), ("Exit", f"Close · {esc(d['exit'])}<br><span class='m'>(5 trading days)</span>"),
                      ("Benchmark", "vs S&P 500")])
             + f'<div class="h">📊 Strategy so far</div>{stats}'
             f'<div class="foot"><span>Tracked live, results in #track-record · Not financial advice</span><span>Signal open 🟠</span></div>')
    return _embed(ORANGE, inner)


# ---------------------------------------------------------------- 3. result + weekly (#track-record)

def result_card(d):
    """d: ticker, number, entry_day, exit_day, entry, exit, ret, spy, excess, totals{n,wins,avg}"""
    t = d["totals"]
    inner = (f'<div class="au">📊 SIGNAL RESULT · CONVICTION #{d["number"]:03d}</div>'
             f'<div class="t">${esc(d["ticker"])} closed: {pct(d["ret"])} vs S&P {pct(d["spy"])}</div>'
             + _grid([(f"Entry · {d['entry_day']}", f"${d['entry']:,.2f}"), (f"Exit · {d['exit_day']}", f"${d['exit']:,.2f}"),
                      ("Result", f"<b class='{cls(d['ret'])}'>{pct(d['ret'])}</b> · {'beat' if d['excess'] > 0 else 'lagged'} S&P by "
                                 f"<b class='{cls(d['excess'])}'>{abs(d['excess']) * 100:.1f} pts</b>")])
             + '<div class="h">🧮 Running total (all live signals)</div>'
             + _grid([("Signals", f"<span class='big'>{t['n']}</span>"), ("Beat the S&P", f"<span class='big'>{t['wins']} / {t['n']}</span>"),
                      ("Avg vs S&P", f"<span class='big {cls(t['avg'])}'>{pct(t['avg'])}</span>")])
             + f'<div class="foot"><span>Every signal is counted, wins and losses · Not financial advice</span>'
               f'<span>Signal closed {"✅" if d["excess"] > 0 else "❌"}</span></div>')
    return _embed(GREEN if d["excess"] > 0 else RED, inner)


def weekly_card(d):
    """d: week_lines[(ticker, excess)], totals|None, open_n"""
    t = d.get("totals")
    week = ("".join(f"<tr><td><b>${esc(tk)}</b></td><td class='{cls(x)}'><b>{pct(x)}</b> vs S&P</td></tr>" for tk, x in d["week"])
            if d["week"] else "<tr><td class='m'>No signals closed this week.</td></tr>")
    tot = (_grid([("Signals", f"<span class='big'>{t['n']}</span>"), ("Beat the S&P", f"<span class='big'>{t['wins']} / {t['n']}</span>"),
                  ("Avg vs S&P", f"<span class='big {cls(t['avg'])}'>{pct(t['avg'])}</span>")])
           if t else '<div class="d m">No closed signals yet. The live test just started.</div>')
    inner = (f'<div class="au">📊 WEEKLY UPDATE · CONVICTION CLUSTERS</div><div class="t">This week in the strategy</div>'
             f'<table>{week}</table><div class="h">🧮 Running total</div>{tot}'
             f'<div class="d" style="margin-top:10px">Open signals: <b>{d["open_n"]}</b> · Backtest expectation: about +1.5% vs S&P per signal, ~50% win rate.</div>'
             f'<div class="foot"><span>Every signal is counted, wins and losses · Not financial advice</span><span>Weekly</span></div>')
    return _embed(BLUE, inner)


# ---------------------------------------------------------------- 4. politician trade (#politicians)

def politician_card(d):
    """d: label, chamber, filed, trades[{word, ticker, amount, date, move, match}], committees, delay, late, match_note"""
    lines = []
    for t in d["trades"][:6]:
        color = "g" if t["word"] == "BUY" else "r" if t["word"] == "SELL" else "m"
        extra = f" · since: <b class='{cls(t['move'])}'>{pct(t['move'], True)}</b>" if t.get("move") is not None else ""
        lines.append(f"<div><b class='{color}'>{t['word']}</b> ${esc(t['ticker'])} · {esc(t['amount'])}"
                     f"{' · ' + esc(t['date']) if t.get('date') else ''}{extra}{' ⚠️' if t.get('match') else ''}</div>")
    if len(d["trades"]) > 6:
        lines.append(f"<div class='m'>…and {len(d['trades']) - 6} more in the filing</div>")
    comm = ", ".join(d.get("committees") or []) or "—"
    match = '<br><span class="chip c-match">SECTOR MATCH</span>' if d.get("match_note") else ""
    delay = (f"{d['delay']} days after the trade" + (" <b class='r'>(past the 45-day deadline)</b>" if d.get("late") else "")) if d.get("delay") is not None else "—"
    tickers = list(dict.fromkeys(t["ticker"] for t in d["trades"]))
    verb = "bought" if all(t["word"] == "BUY" for t in d["trades"]) else "sold" if all(t["word"] == "SELL" for t in d["trades"]) else "traded"
    title = f"{esc(d['label'])} {verb} " + ", ".join(f"${esc(x)}" for x in tickers[:3]) + (f" +{len(tickers) - 3} more" if len(tickers) > 3 else "")
    inner = (f'<div class="au">🏛 POLITICIAN TRADE · {esc(d["chamber"]).upper()} · DISCLOSED {esc(d["filed"]).upper()}</div>'
             f'<div class="t">{title}</div>'
             + _grid([("Trades", f"{len(d['trades'])} in this filing"), ("Reported", delay), ("Committees", esc(comm) + match)])
             + f'<div class="bx" style="margin-top:10px;line-height:1.7">{"".join(lines)}</div>'
             + (f'<div class="bx" style="margin-top:10px">⚠️ <b>Committee match:</b> {esc(d["match_note"])}</div>' if d.get("match_note") else "")
             + '<div class="foot"><span>Source: STOCK Act disclosures · Congress reports amount ranges only · Not financial advice</span><span></span></div>')
    return _embed(PURPLE, inner)


# ---------------------------------------------------------------- 5. case file (#case-files)

def case_card(d):
    """d: number, ticker, sector, exchange, title, summary, who, role, value, shares, price, drop_text,
          company_desc, filed, timeline[{when, text, color}], scenarios(up, down)|None,
          market{question, yes_label, yes, no_label, no, when}|None"""
    tl = "".join(f'<div class="ev" style="--dc:{e.get("color", "#4e5058")}"><div class="w">{esc(e["when"])}</div>'
                 f'<div class="x">{e["text"]}</div></div>' for e in d["timeline"])
    sc = ""
    if d.get("scenarios"):
        up, down = d["scenarios"]
        sc = (f'<div class="h">⚖️ What could happen</div><div class="two">'
              f'<div class="bx"><b class="g">{esc(up[0])}</b><br>{esc(up[1])}</div>'
              f'<div class="bx"><b class="r">{esc(down[0])}</b><br>{esc(down[1])}</div></div>')
    mk = ""
    if d.get("market"):
        m = d["market"]
        mk = (f'<div class="h">🎲 What the market expects</div><div class="bx">Polymarket · "{esc(m["question"])}"'
              f'<div class="odds"><b class="b">{esc(m["yes_label"])} {m["yes"] * 100:.0f}%</b><div class="ob"><i style="width:{m["yes"] * 100:.0f}%"></i></div>'
              f'<b class="r">{m["no"] * 100:.0f}% {esc(m["no_label"])}</b></div>'
              f'<div class="m" style="font-size:12.5px;margin-top:6px">Odds when this case opened ({esc(m["when"])}) · they change live, link above</div></div>')
    inner = (f'<div class="au">🔎 CASE FILE #{d["number"]:03d} · {esc(d.get("sector") or "").upper()} · {esc(d.get("exchange") or "")}</div>'
             f'<div class="t">{esc(d["title"])}</div><div class="d">{esc(d["summary"])}</div>'
             '<div class="h">💼 The buy</div>'
             + _grid([("Who", f"{esc(d['who'])}<br><span class='m'>{esc(d['role'])}</span>"),
                      ("Amount", f"<b class='g'>${d['value']:,.0f}</b><br>{d['shares']:,.0f} sh @ ${d['price']:,.2f}"),
                      ("Type", "Open-market buy (P)<br>own money"),
                      ("Stock before buy", d.get("drop_text") or "—"),
                      ("Company", esc(d.get("company_desc") or "—")),
                      ("Filed", f"{esc(d['filed'])} · SEC Form 4")])
             + f'<div class="h">🧩 Why now? The connections</div><div class="tl">{tl}</div>{sc}{mk}'
             '<div class="foot"><span>We don\'t know why insiders buy. We show what\'s coming next. · Not financial advice</span><span>Case open 🟡</span></div>')
    return _embed(YELLOW, inner)


def case_update_card(d):
    """d: number, ticker, event, buy_price, now_price, ret, spy, days, note"""
    inner = (f'<div class="au">✅ CASE FILE #{d["number"]:03d} · UPDATE</div>'
             f'<div class="t">The event happened: {esc(d["event"])}</div>'
             + _grid([("Insider's price", f"${d['buy_price']:,.2f}"), ("Now", f"${d['now_price']:,.2f}"),
                      ("Since the buy", f"<b class='{cls(d['ret'])}'>{pct(d['ret'])}</b> in {d['days']} days"
                                        + (f"<br><span class='m'>S&P {pct(d['spy'])}</span>" if d.get("spy") is not None else ""))])
             + (f'<div class="d" style="margin-top:10px">{esc(d["note"])}</div>' if d.get("note") else "")
             + '<div class="foot"><span>Every case gets a result, good or bad · Not financial advice</span><span>Case closed 🟢</span></div>')
    return _embed(GREEN if d["ret"] >= 0 else RED, inner)
