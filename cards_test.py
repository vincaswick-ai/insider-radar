"""Manual test: render every card type, post them to #bot-testing (WEBHOOK_PROOF), and print what the Case Files radar sees."""
import os
import cards
import casefiles

hook = os.environ.get("WEBHOOK_PROOF", "").strip()
print("== radar sources")
c = casefiles.calendars()
print("earnings dates:", len(c.get("earnings", {})), "e.g.", list(c.get("earnings", {}).items())[:5])
print("FDA tickers:", len(c.get("fda", {})), "e.g.", list(c.get("fda", {}).items())[:5])
print("polymarket Fed:", casefiles.polymarket("Fed decision in December"))
print("polymarket Nvidia:", casefiles.polymarket("Nvidia"))
if hook:
    cards.post_card(hook, "🧪 **Card test** · insider buy", cards.buy_card(dict(
        ticker="TFC", exchange="NYSE", industry="Banks", company_short="Truist Financial", role_short="CEO",
        name="Michael P. Lyons", role="President & CEO", value=1209050, shares=25000, price=48.36, date_text="Oct 28",
        stake_text="", meaning="The CEO put $1.2M of their own money into the stock.", footer_right="Truist Financial · ⭐ S&P 500", tags=[])))
    cards.post_card(hook, "🧪 **Card test** · conviction", cards.conviction_card(dict(
        ticker="TEST", company="Test Corp", n=3, total=6.4e6, days=5, number=1, entry="Nov 05", exit="Nov 12", stats=None,
        insiders=[dict(name="Jane Doe", role="CEO", value=3.1e6, stake_text="+48%"), dict(name="John Roe", role="CFO", value=1.9e6, stake_text="+120%"),
                  dict(name="Mary Poe", role="Director", value=1.4e6, stake_text="new position")])))
    cards.post_card(hook, "🧪 **Card test** · case file", cards.case_card(dict(
        number=0, ticker="TEST", sector="Solar", exchange="NASDAQ", title="$TEST · Test's CEO buys $267K, 1 day before the midterms",
        summary="Format test.", who="Test Person", role="CEO", value=267514, shares=10000, price=26.75, drop_text="−20% in one day",
        company_desc="Solar equipment", filed="Nov 02", timeline=[dict(when="Oct 28 · what hurt the company", icon="🔴", text="Revenue **−28.5%**."),
                  dict(when="Nov 02 · today", icon="🟢", text="CEO buys **$267K** near the low."),
                  dict(when="Nov 03 · in 1 day ⏳", icon="🟡", text="**US midterm elections.** Control of Congress decides solar tax credits.")],
        scenarios=[["If Democrats win Congress", "Solar tax credits back on the table."], ["If Republicans hold", "Weak US demand likely continues."]],
        market=dict(question="Which party wins the House?", yes_label="DEM", yes=0.64, no_label="GOP", no=0.36, when="Nov 02", url="https://polymarket.com"))))
    cards.post_card(hook, "", cards.politician_card(dict(label="Sen. Test Person (R-XX)", chamber="Senate", filed="Jan 16",
        trades=[dict(word="BUY", ticker="CVX", amount="$15K–$50K", date="Dec 29", move=0.101, match=True),
                dict(word="BUY", ticker="RTX", amount="$15K–$50K", date="Dec 29", move=0.095, match=True)],
        committees=["Armed Services"], delay=18, late=False, match_note="$RTX is in Defense, an area the Armed Services Committee oversees.")))
    cards.post_card(hook, "", cards.result_card(dict(ticker="TEST", number=1, entry_day="2026-11-05", exit_day="2026-11-12",
        entry=41.2, exit=43.75, ret=0.062, spy=0.008, excess=0.054, totals=dict(n=14, wins=9, avg=0.022))))
print("done")
