"""
Public track record of every #signals post.

  add(...)   called by bot.py when a signal is posted: saves ticker, time and price
             to signals_log.json (committed to git, so every entry has a timestamp
             that can't be changed quietly later).
  main()     weekly report: how EVERY signal did since it was posted vs the S&P 500,
             losers included. Posts to WEBHOOK_TRACK (public) or WEBHOOK_PROOF (test).

Env: TARGET=test|public, SEED=1 (also add earlier strong alerts from state.json,
marked as "from alert history").
"""

import io
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

from chart import BG, GRID, MUTED, PANEL, TEXT, UP, DOWN, fetch_prices
from score import close_on

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "signals_log.json")
STATE_FILE = os.path.join(HERE, "state.json")
CHECKPOINTS = [(30, "1M"), (91, "3M"), (182, "6M")]
SEED_MIN_SCORE = 7
MAX_LINES = 25


def log(msg):
    print(msg, flush=True)


def _day(s):
    try:
        return datetime.fromisoformat(str(s)[:10]).date()
    except ValueError:
        return None


def load():
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(entries):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=1)


def add(ticker, company, insider, role, value, insider_price, signal_price, kind="insider",
        score=None, url=None, posted=None, source="live"):
    """Append one signal (skips duplicates of the same ticker + insider within 14 days)."""
    entries = load()
    posted = posted or datetime.now(timezone.utc).isoformat(timespec="minutes")
    pday = _day(posted)
    for e in entries:
        if e["ticker"] == ticker and e.get("insider") == insider and pday and _day(e["posted"]) \
                and abs((_day(e["posted"]) - pday).days) <= 14:
            return False
    entries.append({
        "id": len(entries) + 1, "posted": posted, "ticker": ticker, "company": company,
        "insider": insider, "role": role, "kind": kind, "score": score,
        "value": round(value or 0), "insider_price": round(insider_price or 0, 4),
        "signal_price": round(signal_price, 4) if signal_price else None,
        "url": url, "source": source,
    })
    save(entries)
    return True


def seed_from_state():
    """Add earlier strong alerts (score 7+) still in state.json, marked as history."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return 0
    n = 0
    for b in state.get("buys", []):
        if (b.get("score") or 0) >= SEED_MIN_SCORE and b.get("value", 0) >= 100_000:
            if add(b["ticker"], b.get("company"), b.get("name"), b.get("role"), b.get("value"),
                   b.get("price"), None, score=b.get("score"), url=b.get("url"),
                   posted=b.get("filed") or b.get("date"), source="history"):
                n += 1
    return n


# ---------------------------------------------------------------- results

class Prices:
    def __init__(self):
        self.rows = {}

    def get(self, t):
        if t not in self.rows:
            try:
                self.rows[t] = fetch_prices(t, "2y")[0]
            except Exception:
                self.rows[t] = []
            time.sleep(0.2)
        return self.rows[t]


def result(e, prices):
    """Return dict with ret, spy, edge and checkpoint edges, or None if no prices."""
    rows, spy = prices.get(e["ticker"]), prices.get("SPY")
    day = _day(e["posted"])
    if not rows or not spy or not day:
        return None
    base = e.get("signal_price") or close_on(rows, day)
    sbase = close_on(spy, day)
    if not base or not sbase:
        return None
    ret = rows[-1][4] / base - 1
    sret = spy[-1][4] / sbase - 1
    out = {"ret": ret, "spy": sret, "edge": ret - sret, "days": (rows[-1][0] - day).days, "cp": {}}
    for d, label in CHECKPOINTS:
        if out["days"] >= d:
            p, s = close_on(rows, day + timedelta(days=d)), close_on(spy, day + timedelta(days=d))
            if p and s:
                out["cp"][label] = (p / base - 1) - (s / sbase - 1)
    return out


def pct(x):
    return f"{x * 100:+.1f}%"


def build(entries, prices):
    done = []
    for e in entries:
        r = result(e, prices)
        if r:
            done.append((e, r))
    if not done:
        return None, None
    wins = sum(1 for _, r in done if r["edge"] > 0)
    avg_edge = sum(r["edge"] for _, r in done) / len(done)
    avg_ret = sum(r["ret"] for _, r in done) / len(done)

    lines = []
    for e, r in sorted(done, key=lambda x: x[0]["posted"], reverse=True)[:MAX_LINES]:
        mark = "✅" if r["edge"] > 0 else "❌"
        hist = " ·  *history*" if e.get("source") == "history" else ""
        cps = " · ".join(f"{k} {pct(v)}" for k, v in r["cp"].items())
        lines.append(f"{mark} `{e['posted'][:10]}` **${e['ticker']}** {pct(r['ret'])} "
                     f"(S&P {pct(r['spy'])}) → **{pct(r['edge'])}**"
                     + (f" · {cps}" if cps else "") + hist)
    more = len(done) - MAX_LINES
    if more > 0:
        lines.append(f"…and {more} older signals in the public log")

    cp_lines = []
    for _, label in CHECKPOINTS:
        vals = [r["cp"][label] for _, r in done if label in r["cp"]]
        if vals:
            cp_lines.append(f"**{label}:** {sum(1 for v in vals if v > 0)}/{len(vals)} beat the S&P · "
                            f"avg {pct(sum(vals) / len(vals))}")

    embed = {
        "title": "📒 Track record: every signal we posted",
        "description": (
            f"**{len(done)} signals · {wins} beat the S&P 500 ({wins / len(done):.0%}) · "
            f"avg signal {pct(avg_ret)}, {pct(avg_edge)} more than the S&P**\n"
            + ("\n".join(cp_lines) + "\n" if cp_lines else "")
            + "\nEvery signal is counted, losers too. Price = close on the day we posted it.\n\n"
            + "\n".join(lines)),
        "color": 0x23A55A if avg_edge > 0 else 0xF23F43,
        "footer": {"text": "Logged with a timestamp the moment it's posted · Not financial advice"},
    }
    return embed, chart(done)


def chart(done):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        rows = sorted(done, key=lambda x: x[1]["edge"])[-20:] if len(done) > 20 else \
            sorted(done, key=lambda x: x[1]["edge"])
        labels = [f"${e['ticker']} {e['posted'][5:10]}" for e, _ in rows]
        vals = [r["edge"] * 100 for _, r in rows]
        fig, ax = plt.subplots(figsize=(8, max(2.6, 0.32 * len(rows) + 1)), dpi=150)
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(PANEL)
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.barh(labels, vals, color=[UP if v > 0 else DOWN for v in vals], height=0.6)
        ax.axvline(0, color=MUTED, lw=1)
        ax.tick_params(colors=TEXT, labelsize=8.5, length=0)
        ax.set_axisbelow(True)
        ax.grid(axis="x", color=GRID, lw=0.6, alpha=0.6)
        ax.set_title("Each signal vs the S&P 500 since we posted it (%)", color=TEXT, fontsize=11, loc="left")
        fig.text(0.99, 0.01, "Insider Radar · not financial advice", color=MUTED, fontsize=7, ha="right")
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=BG)
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log(f"chart failed: {e}")
        return None


def post(webhook, embed, png):
    payload = {"username": "Insider Radar", "embeds": [embed]}
    files = {}
    if png:
        embed["image"] = {"url": "attachment://record.png"}
        files["files[0]"] = ("record.png", png, "image/png")
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


def main():
    if os.environ.get("SEED") == "1":
        log(f"Seeded {seed_from_state()} signals from alert history.")
    entries = load()
    log(f"Signals in log: {len(entries)}")
    embed, png = build(entries, Prices())
    if not embed:
        log("No signals with prices yet.")
        return
    target = (os.environ.get("TARGET") or "public").lower()
    hook = (os.environ.get("WEBHOOK_TRACK") if target == "public" else os.environ.get("WEBHOOK_PROOF")) or ""
    if not hook.strip():
        log(f"No webhook for {target}.")
        print(json.dumps(embed, indent=1))
        return
    post(hook.strip(), embed, png)
    log(f"Posted track record to {target}.")


if __name__ == "__main__":
    main()
