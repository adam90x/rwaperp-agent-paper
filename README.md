# RWAPerp Agent — FINAL PAPER

This is the final research build before any live execution work.

## PAPER ONLY

The project contains **no live order-placement implementation** and does not handle wallet/private keys/API secrets. It only reads public RWAPerp data and simulates entries, exits, funding, leverage and P/L.

RWAPerp's public API provides market data and the platform documents REST/WebSocket API access; its Quick Start also warns that order examples can submit real orders. We intentionally do not include that capability here.

## Trading model

The agent scans available `PERP_*` markets and ranks them using:

- market regime (TREND/MIXED/RANGE)
- multi-window momentum
- EMA alignment
- VWAP
- volume acceleration
- recent trade imbalance
- orderbook imbalance/depth
- mark/index deviation
- volatility and ATR
- shock-candle filter
- relative strength
- funding rate
- conservative fee/slippage buffer
- directional confirmation

A market in a range or with poor liquidity, excessive spread, weak edge, adverse funding, a price/oracle deviation, or a shock move is rejected.

## Portfolio rules

- **1 to 4 simultaneous positions**
- first slot score >= 78
- second >= 82
- third >= 85
- fourth >= 88
- **7% maximum nominal position size** based on current marked equity
- **28% maximum total nominal exposure**
- maximum 3 positions in one direction
- highly correlated positions are rejected

The bot does not open a fourth position just because a slot is available.

## Leverage

The final model can select **1x–5x**.

Important: leverage is used as a **margin-efficiency setting**, not as permission to increase the 7% nominal exposure cap.

Example with $500 equity:
- max position notional = $35
- at 1x, model margin = about $35
- at 2x, model margin = about $17.50
- at 5x, model margin = about $7

P/L is calculated from the nominal position, so leverage does not secretly amplify the account's risk in PAPER mode.

The model also checks a conservative, approximate liquidation-distance buffer. This is **not the exchange's exact liquidation formula**.

## Exits

- dynamic SL: 0.75%–1.50% based on ATR
- initial target: **2.5R**
- partial take: **30% at 1R**
- trailing activates at **1.5R**
- at least 0.5R is locked when trailing
- thesis invalidation can close early
- time stop after 60 minutes if the setup has not proven itself
- funding is included in paper P/L

## Capital

Profits compound automatically.

The 7% cap is recalculated from current marked equity.

Examples:
- $500 → $35 maximum position
- $550 → $38.50
- $450 → $31.50

No paper profit reserve is used right now.

## Risk protection

- daily loss halt: 2.5%
- max peak-to-trough equity drawdown: 6%
- cooldown after losses
- max 3 consecutive losses before protection
- no trade if data quality is insufficient
- no trade if liquidation-distance buffer is inadequate

## Phone monitoring

Optional Telegram notifications can send:
- agent online
- paper entries
- paper exits
- risk halts
- errors
- 15-minute heartbeat with equity, P/L and number of open positions

### Setup

1. Create a Telegram bot with BotFather.
2. Put the token in the VPS environment:
   `TELEGRAM_BOT_TOKEN`
3. Put your chat ID in:
   `TELEGRAM_CHAT_ID`
4. Set `"enabled": true` in `config.json`.

The token is never stored in this repository.

## 24/7 VPS

Use the included systemd service:

```bash
sudo mkdir -p /opt/rwaperp-agent
sudo cp -r . /opt/rwaperp-agent/
cd /opt/rwaperp-agent

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Test
python agent.py

# Then configure systemd:
sudo cp deploy/rwaperp-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rwaperp-agent
sudo systemctl status rwaperp-agent
```

The computer can be completely turned off once the VPS is running.

## Report

```bash
python report.py
```

The agent also writes `paper_live_report.json`.

## Reality check

There is no "unsinkable" trading strategy. The purpose of this version is to maximize selectivity and keep losses bounded while testing whether the system has real positive expectancy after conservative costs.

Do not add a wallet seed phrase or private key to this project.
