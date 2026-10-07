# RWAPerp Trading Lab V2.2.2 — PAPER ONLY

Final paper-trading research build.

## Core setup
- PAPER ONLY; real order execution disabled.
- 5 maximum open positions.
- Standard position size: 7% notional.
- Exceptional setup: 12% notional for score >=94 + TREND, maximum one exceptional position.
- Maximum gross exposure: 40%.
- Hard stop: -15% adverse price movement.
- No fixed take-profit.
- Trailing starts at +10% and uses the configured 5/7/10/15% retracement ladder.
- Maximum 2 positions in the same high-correlation cluster.

## Stale position rule
A position that has not improved its best profit by at least 0.05 percentage points for 60 minutes, and has not activated trailing, is closed with reason `STALE_NO_PROGRESS_60M` to free a slot.

## Persistence
Neon PostgreSQL is the durable source of truth when `DATABASE_URL` is configured. Render local JSON is only a cache.

- Entries/exits force an immediate save.
- Normal checkpoints are limited to one write per 5 minutes.
- After a Render restart, the dashboard can read trade history/state from Neon even before the local cache is rebuilt.
- Skipping a checkpoint because 5 minutes have not elapsed is treated as success, not as a DB failure.
- If Neon cannot be initialized/read/saved when `DATABASE_URL` is configured, the agent fails closed instead of trading against an untrusted local-only state.

Required Render environment variable:
`DATABASE_URL` = Neon PostgreSQL connection string.

## Dashboard
- Equity chart: 24H / 7D / ALL-TIME with X/Y axes.
- Scanner: markets refreshed, candidates, selected, portfolio rejections and rejection reasons.
- ALL-TIME trade history.
- Daily P/L.
- Strategy stats and score buckets.
- Neon persistence status and last durable save time.

## Render
Build command:
`pip install -r requirements.txt`

Start command:
`gunicorn -w 1 -b 0.0.0.0:$PORT webapp:app`

## Research protocol
Collect 100–200 paper trades before judging the strategy by win rate, average winner/loser, profit factor, drawdown and score-bucket performance.
