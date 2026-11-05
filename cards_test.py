"""Manual test: post every card type (with real weekly charts) to #bot-testing (WEBHOOK_PROOF),
and print what the buys-before-events radar sees."""
import os
from datetime import date
import cards
import casefiles
from chart import fetch_prices, make_weekly
from score import drop_from_high

hook = os.environ.get("WEBHOOK_PROOF", "").strip()
print("== radar sources")
c = casefiles.calendars()
print("earnings dates:", len(c.get("earnings", {})), "| FDA tickers:", len(c.get("fda", {})))
for sec in ["defense", "oil", "banks", "semis", "solar", "health", "autos"]:
    print("political/macro markets for", sec, "->", [(e["date"].isoformat(), e["market"]["question"], round(e["market"]["volume"]))
                                                   for e in casefiles.sector_markets(sec, date.today())])
print("upcoming LMT:", casefiles.upcoming("LMT", "defense", date.today(), 300000))
if hook:
    rows, ex = fetch_prices("TFC", "5y")
    img = make_weekly("TFC", rows, [{"date": "2026-10-28", "price": 48.36}], ex)
    base = dict(ticker="TFC", exchange=ex, sp500=True, company="Truist Financial", name="Michael P. Lyons",
                role="President & CEO", value=1209050, shares=25000, price=48.36, date_text="Oct 28",
                stake_change=12.4, new_position=False, owned_after=226000, drop=drop_from_high(rows),
                market_cap=63.5e9, size="Large cap", others=1, plan=False, industry="Banks",
                record={"count": 2, "first": date(2023, 3, 14), "last": date(2025, 4, 8), "ret": 0.27})
    cards.post_card(hook, "🧪 insider buy · [SEC filing](<https://www.sec.gov/>)", cards.insider_card(base), image=img)
    cards.post_card(hook, "🧪 big cap · [SEC filing](<https://www.sec.gov/>)", cards.bigcap_card(base), image=img)
    buys = [{"date": "2026-10-21", "price": 46.9}, {"date": "2026-10-24", "price": 47.5}, {"date": "2026-10-28", "price": 48.36}]
    img3 = make_weekly("TFC", rows, buys, ex)
    people = [dict(name="Michael P. Lyons", role="CEO", value=1.2e6, date="Oct 28", stake_text="stake +12%"),
              dict(name="Jane Doe", role="CFO", value=410e3, date="Oct 24", stake_text="new position"),
              dict(name="John Roe", role="Director", value=250e3, date="Oct 21", stake_text="stake +40%")]
    cards.post_card(hook, "🧪 cluster", cards.cluster_card(dict(base, insiders=people, total=1.86e6, days=8, avg_price=47.6)), image=img3)
    cards.post_card(hook, "🧪 clusters strategy", cards.conviction_card(dict(base, n=3, total=1.86e6, days=8, number=1,
                    entry="Nov 06", exit="Nov 13", stats=None, insiders=people)), image=img3)
    cards.post_card(hook, "🧪 buy before event · [Live odds](<https://polymarket.com>)", cards.case_card(dict(base, number=2,
        events=[{"when": "Dec 09 (in 34 days)", "text": "**Fed interest-rate decision.** Rate decisions move bank margins and lending."}],
        market=dict(question="Fed decision in December?", yes_label="NO CHANGE", yes=0.71, no_label="CUT", no=0.29, when="Nov 05"))),
        image=img)
print("done")
