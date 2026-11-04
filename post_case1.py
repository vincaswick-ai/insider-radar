"""One-off: post Case File #001 (Enphase before the 2026 midterms) and its result to #case-files."""
import os
import cards

hook = os.environ["WEBHOOK_CASE_FILES"].strip()
case = cards.case_card({
    "number": 1, "ticker": "ENPH", "sector": "Solar", "exchange": "NASDAQ",
    "title": "$ENPH · Enphase's CEO buys $267K, 1 day before the midterms",
    "summary": "The CEO bought shares with his own money a week after the stock crashed, and one day before "
               "an election that decides the future of his industry's tax credits.",
    "who": "Badri Kothandaraman", "role": "President & CEO", "value": 267514, "shares": 10000, "price": 26.75,
    "drop_text": "🔴 **−20%** in one day\n(Oct 28, after earnings)", "company_desc": "Solar inverters & batteries for homes",
    "filed": "Nov 02",
    "timeline": [
        {"icon": "🔴", "when": "Oct 27 · what hurt the company",
         "text": "Q3 revenue **−28.5%**. Management: US demand fell after the residential solar tax credit expired."},
        {"icon": "🟢", "when": "Nov 02", "text": "CEO buys **$267K** near the post-earnings low."},
        {"icon": "🟡", "when": "Nov 03 · 1 day later ⏳",
         "text": "**US midterm elections.** Control of Congress decides whether solar tax credits come back. "
                 "The same day, director Shanker Trivedi bought 1,000 shares at $30.59."},
    ],
    "scenarios": [["If Democrats win Congress", "Solar tax credits back on the table; demand could recover."],
                  ["If Republicans hold", "No credit; weak US demand likely continues."]],
    "market": None,
})
cards.post_card(hook, "[CEO's SEC Form 4](<https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001463101&type=4>)", case)
update = cards.case_update_card({
    "number": 1, "ticker": "ENPH", "event": "Democrats won both the House and the Senate (Nov 03)",
    "buy_price": 26.75, "now_price": 33.64, "ret": 33.64 / 26.75 - 1, "spy": None, "days": 2,
    "note": "Solar stocks rallied the day after the vote. Price as of Nov 04 during trading.",
})
cards.post_card(hook, "", update)
print("posted case #001")
