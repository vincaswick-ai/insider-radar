"""Check every webhook secret: which Discord channel it posts to. Reads only (GET), never posts, never prints URLs."""
import os
import requests

EXPECTED = {
    "WEBHOOK_INSIDER_BUYS": "1563980426421477457",
    "WEBHOOK_BIG_CAPS": "1567837594027302953",
    "WEBHOOK_CLUSTERS": "1567837534740815892",
    "WEBHOOK_CASE_FILES": "1567621546644602940",
    "WEBHOOK_POLITICIANS": "1563980440950546604",
    "WEBHOOK_INVESTORS": "1567837627128742011",
    "WEBHOOK_CONVICTION": "1567258350054412429",
    "WEBHOOK_PROOF": "1564309318927650967",
}
for name, want in EXPECTED.items():
    url = os.environ.get(name, "").strip()
    if not url:
        print(f"{name:22} MISSING")
        continue
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            print(f"{name:22} BROKEN (HTTP {r.status_code})")
            continue
        got = r.json().get("channel_id")
        print(f"{name:22} {'OK' if got == want else 'WRONG CHANNEL'}  -> channel {got}")
    except Exception as e:
        print(f"{name:22} ERROR {type(e).__name__}")
