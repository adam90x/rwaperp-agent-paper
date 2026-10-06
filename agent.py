
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import median
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE_URL = "https://api-trade.rwaperp.xyz"
PUBLIC_QUERY = f"{BASE_URL}/v1/public/query"
PUBLIC_FUNDING = f"{BASE_URL}/v1/public/funding_rates"

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "paper_state.json"
EVENT_FILE = ROOT / "paper_events.jsonl"
REPORT_FILE = ROOT / "paper_live_report.json"

HTTP = requests.Session()


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pct(new, old):
    if not old:
        return 0.0
    return (new / old - 1.0) * 100.0


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def log_event(event):
    with EVENT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def telegram_send(message):
    tg = CFG.get("telegram", {})
    if not tg.get("enabled"):
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        HTTP.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10
        )
    except Exception as exc:
        log_event({
            "timestamp": iso(utcnow()),
            "event": "TELEGRAM_ERROR",
            "error": str(exc)
        })


def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("peak_equity", state.get("equity", CFG["starting_equity"]))
        state.setdefault("day", utcnow().date().isoformat())
        state.setdefault("day_start_equity", state.get("equity", CFG["starting_equity"]))
        state.setdefault("realized_pnl", 0.0)
        state.setdefault("unrealized_pnl", 0.0)
        state.setdefault("gross_profit", 0.0)
        state.setdefault("gross_loss", 0.0)
        state.setdefault("wins", 0)
        state.setdefault("losses", 0)
        state.setdefault("trades", 0)
        state.setdefault("consecutive_losses", 0)
        state.setdefault("positions", {})
        state.setdefault("cooldown_until", None)
        state.setdefault("halt_reason", None)
        return state

    eq = float(CFG["starting_equity"])
    return {
        "cash": eq,
        "equity": eq,
        "peak_equity": eq,
        "day": utcnow().date().isoformat(),
        "day_start_equity": eq,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "wins": 0,
        "losses": 0,
        "trades": 0,
        "consecutive_losses": 0,
        "positions": {},
        "cooldown_until": None,
        "halt_reason": None,
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def post_query(payload):
    r = HTTP.post(
        PUBLIC_QUERY,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10
    )
