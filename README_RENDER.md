
# RWAPerp Agent FINAL — Render Free

## Architecture

GitHub repository → Render Free Web Service → Telegram.
UptimeRobot can be used to hit `/health` every 5 minutes so the Render service receives inbound traffic.

## Render settings

Build Command:
`pip install -r requirements.txt`

Start Command:
`gunicorn -w 1 -b 0.0.0.0:$PORT webapp:app`

Plan:
`Free`

Health Check Path:
`/health`

Environment Variables:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Set Telegram enabled in `config.json`.

## Free-tier limitation

Render Free web services spin down after 15 minutes without inbound traffic and have 750 free instance hours/month. Local filesystem state is ephemeral, so this is intended for PAPER testing, not live funds.

## Keepalive

Recommended: UptimeRobot Free monitor:

`https://YOUR-RENDER-URL/health`

UptimeRobot Free checks every 5 minutes and does not require a credit card.

Optional fallback: the included GitHub Actions workflow checks every 10 minutes. For public repositories, GitHub-hosted standard runners are free.

## Safety

This version has no live order-placement code and no wallet/private-key handling.
