"""
Price chart for an insider buy alert.

Draws ~6 months of daily candles, shades the days the insider bought,
and draws a dashed line at the insider's average buy price.
If anything fails (no price data, unknown ticker), make_chart() returns None
and the bot simply posts the alert without a chart.
"""

import io
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests

PRICE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=6mo&interval=1d"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

BG = "#1e1f22"
PANEL = "#2b2d31"
GRID = "#3a3c42"
TEXT = "#dbdee1"
MUTED = "#949ba4"
UP = "#23a55a"
DOWN = "#f23f43"
BUY = "#f0b232"


def fetch_prices(ticker):
    """Return list of (date, open, high, low, close) or [] if unavailable."""
    url = PRICE_URL.format(ticker=ticker.replace(".", "-"))
    r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=20)
    r.raise_for_status()
    result = (r.json().get("chart", {}).get("result") or [None])[0]
    if not result:
        return []
    ts = result.get("timestamp") or []
    q = (result.get("indicators", {}).get("quote") or [{}])[0]
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        rows.append((datetime.fromtimestamp(t, tz=timezone.utc).date(), o, h, l, c))
    return rows


def render_chart(ticker, rows, buy_start, buy_end, avg_price):
    """Return PNG bytes."""
    dates = [mdates.date2num(r[0]) for r in rows]
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=120)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)

    width = 0.6
    for d, (_, o, h, l, c) in zip(dates, rows):
        color = UP if c >= o else DOWN
        ax.vlines(d, l, h, color=color, linewidth=0.8)
        ax.add_patch(plt.Rectangle((d - width / 2, min(o, c)), width, max(abs(c - o), 1e-6),
                                   facecolor=color, edgecolor=color, linewidth=0.5))

    # insider buy window + average price
    if buy_start:
        s = mdates.date2num(datetime.fromisoformat(buy_start).date())
        e = mdates.date2num(datetime.fromisoformat(buy_end or buy_start).date())
        ax.axvspan(s - 0.6, e + 0.6, color=BUY, alpha=0.18, zorder=0)
        ax.annotate("Insider bought here", xy=((s + e) / 2, avg_price),
                    xytext=(0, 34), textcoords="offset points", ha="center",
                    color=BUY, fontsize=9, fontweight="bold",
                    arrowprops=dict(arrowstyle="-|>", color=BUY, lw=1.4))
        ax.plot([(s + e) / 2], [avg_price], marker="o", markersize=8,
                markerfacecolor=BUY, markeredgecolor=BG, markeredgewidth=1.5, zorder=5)
    ax.axhline(avg_price, color=BUY, linestyle="--", linewidth=1, alpha=0.8)
    ax.text(dates[0], avg_price, f" avg buy \\${avg_price:,.2f}", color=BUY, fontsize=8,
            va="bottom", ha="left")

    last = rows[-1][4]
    change = (last / avg_price - 1) * 100
    sign = "+" if change >= 0 else ""
    ax.set_title(f"\\${ticker}  ·  6 months, daily  ·  now \\${last:,.2f} ({sign}{change:.1f}% vs insider)",
                 color=TEXT, fontsize=11, loc="left", pad=10, fontweight="bold")

    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.yaxis.tick_right()
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"\\${v:,.2f}" if v < 100 else f"\\${v:,.0f}"))
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.5)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xlim(dates[0] - 1, dates[-1] + 2)

    fig.text(0.01, 0.015, "Insider Radar · Source: SEC Form 4 · Not financial advice",
             color=MUTED, fontsize=7)
    fig.tight_layout(rect=(0, 0.03, 1, 1))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def make_chart(ticker, buy_start, buy_end, avg_price):
    """PNG bytes, or None if the chart can't be made. Never raises."""
    try:
        rows = fetch_prices(ticker)
        if len(rows) < 10:
            return None
        return render_chart(ticker, rows, buy_start, buy_end, avg_price)
    except Exception as e:
        print(f"  chart skipped for {ticker}: {e}", flush=True)
        return None
