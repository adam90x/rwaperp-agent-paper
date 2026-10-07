RWAPerp Trading Lab V2.1 — PAPER ONLY

Current test configuration:
- PAPER ONLY; real order execution disabled.
- Hard -15% adverse price stop per position.
- No fixed take-profit and no time stop.
- Trailing starts at +10% profit and follows the 5/7/10/15% retracement ladder.
- Max 5 open positions; standard 7% notional, exceptional 12% notional for score >=94 + TREND (max one).
- Max 2 positions in the same high-correlation cluster.
- Score 0–100 with thresholds 78/82/85/88 by slot.
- Volume, momentum, trend, spread, depth, funding, edge and confirmation filters.
- BTC regime context: strongly opposite BTC trend blocks an entry; aligned BTC adds score.
- Overextension guard blocks entries that are too far beyond recent breakout levels or too stretched vs ATR.
- Cooldown is triggered after 3 consecutive losing trades (30 minutes), not after every single loss.
- Daily loss and max equity drawdown protection remain enabled.
- Full trade journal records entry score/reasons, BTC regime, overextension, correlation count, peak profit, hold time and exit reason.
- Dashboard: 24H/7D/ALL-TIME equity, open-position candles, history, daily P/L, drawdown, scanner funnel, strategy stats and score buckets.

Test protocol:
Do not judge the strategy from 7 trades. Collect 100–200 paper trades, then evaluate win rate, average winner/loser, profit factor, drawdown and score-bucket performance.

V2.1 patch: dashboard axis labels/IDs fixed; expected-move potential filter added.


V2.2 persistence:
- Neon PostgreSQL is the durable source of truth for paper state.
- Render local JSON files remain only as a fallback/cache.
- State is restored from Neon on startup.
- Entries and exits force an immediate durable save.
- A low-frequency checkpoint limits database activity.
- Render restarts/redeploys therefore no longer reset the paper portfolio when DATABASE_URL is configured.
- Required environment variable on Render: DATABASE_URL (the Neon pooled connection string).


Safety / persistence:
- Neon PostgreSQL is the durable source of truth when DATABASE_URL is configured.
- If Neon cannot be read or a required save fails, the paper agent fails closed instead of continuing on local-only state.
- Positions that have not improved their best profit by at least 0.05 percentage points for 60 minutes and have not activated trailing are closed with reason STALE_NO_PROGRESS_60M.
