# Paper-trading bot (Alpaca paper account)

`bot/paperbot.py` trades an Alpaca **paper** account (fake money) on its own every US trading day,
on GitHub's servers (`.github/workflows/bot.yml`). It only talks to paper-api.alpaca.markets and
refuses any account whose number doesn't start with "PA". Keys live in encrypted repo secrets.

| New York | Spain | What |
|---|---|---|
| 08:45–09:27 | 14:45–15:27 | scan ~5,900 US stocks, backtest the top 40 momentum setups, pick up to 3 opening buys (half the cash) and a 15-stock breakout watchlist; every order passes the trader-risk check |
| 09:50–15:30 | 15:50–21:30 | every 5 min: buy fresh opening-range breakouts (first 5-min close above the 9:30–9:45 high, stop at the range middle, target 2R) |
| 10:00 | 16:00 | cancel opening buys that didn't fill |
| 15:55 | 21:55 | close everything the bot opened |
| 16:05 | 22:05 | report in `reports/YYYY-MM-DD.md` |

Aggressive settings: risk 3% of equity per trade, stop for the day at 10%, up to 8 trades a day
(`trader/config.json`, synced with the equity each morning).

Files the bot writes (committed back by the workflow): `bot/state/`, `bot/logs/`, `reports/`, `trader/`.
`skills/` = trader-risk, trader-scanner, trader-entry from jakeschincariol/day-trader-skill (MIT, see `skills/LICENSE`).

Manual run: Actions tab → Paper bot → Run workflow (`status`, `plan --dry-run`, `report`...), or
`gh workflow run bot.yml -R goatmoments894/paper-trader -f cmd=status`.
Stop it: Actions tab → Paper bot → ⋯ → Disable workflow.
