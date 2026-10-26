# 📡 Insider Radar bot

Posts SEC Form 4 insider **buys** to Discord, in plain English.

- Checks SEC EDGAR every 15 minutes (Mon–Fri) using GitHub Actions — free, no server needed.
- Keeps only real open-market purchases (code `P`) above `MIN_VALUE` (default $50,000).
- Ignores sales, stock awards, option exercises and amendments.
- Skips OTC / penny stocks — only NYSE, NASDAQ, NYSE American, NYSE Arca and Cboe listings are posted (set variable `SKIP_OTC` to `0` to allow OTC).
- Reads back through SEC's feed until it reaches the previous run, so nothing is missed even if GitHub runs late.
- Posts a 🔥 cluster alert when 3+ different insiders buy the same stock within 14 days.
- Remembers processed filings in `state.json` (committed back automatically).

## Setup

1. **Secrets** — repo → Settings → Secrets and variables → Actions → *New repository secret*:
   | Name | Value |
   |---|---|
   | `WEBHOOK_INSIDER_BUYS` | Discord webhook URL of the alerts channel |
   | `WEBHOOK_CLUSTER` | Discord webhook URL of the cluster channel (optional) |
   | `SEC_USER_AGENT` | `InsiderRadar your-email@example.com` (SEC requires a contact) |
2. Optional **variable** `MIN_VALUE` (same page, *Variables* tab), e.g. `100000`.
3. Actions tab → *Insider alerts* → **Run workflow** to test right away.

The first run posts only 3 example alerts so the channel isn't flooded.

## Files

- `bot.py` — the bot
- `.github/workflows/insider-alerts.yml` — the 15-minute schedule
- `requirements.txt` — Python packages

*Not financial advice. Data from public SEC filings.*
