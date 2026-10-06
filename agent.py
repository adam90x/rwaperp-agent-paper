
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import median

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
    r.raise_for_status()
    body = r.json()
    if body.get("success") is False:
        raise RuntimeError(body)
    return body.get("data", body)


def get_funding_rates():
    r = HTTP.get(PUBLIC_FUNDING, timeout=10)
    r.raise_for_status()
    body = r.json()
    if body.get("success") is False:
        raise RuntimeError(body)
    data = body.get("data", body)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    out = {}
    for row in rows:
        sym = row.get("symbol")
        if sym:
            out[sym] = {
                "est": num(row.get("est_funding_rate")),
                "last": num(row.get("last_funding_rate")),
                "est_ts": row.get("est_funding_rate_timestamp"),
                "last_ts": row.get("last_funding_rate_timestamp"),
                "next_ts": row.get("next_funding_time"),
            }
    return out


def parse_level(x):
    if isinstance(x, dict):
        return num(x.get("price")), num(
            x.get("quantity", x.get("size", x.get("executed_quantity", 0)))
        )
    if isinstance(x, (list, tuple)) and len(x) >= 2:
        return num(x[0]), num(x[1])
    return 0.0, 0.0


def parse_candles(candles):
    result = []
    for c in candles or []:
        if isinstance(c, dict):
            row = {
                "open": num(c.get("open")),
                "high": num(c.get("high")),
                "low": num(c.get("low")),
                "close": num(c.get("close")),
                "volume": num(c.get("volume")),
                "timestamp": c.get("timestamp")
            }
        elif isinstance(c, (list, tuple)) and len(c) >= 6:
            row = {
                "open": num(c[1]),
                "high": num(c[2]),
                "low": num(c[3]),
                "close": num(c[4]),
                "volume": num(c[5]),
                "timestamp": c[0]
            }
        else:
            continue
        if row["close"] > 0:
            result.append(row)
    return result


def discover_symbols():
    data = post_query({"type": "marketSummary"})
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            symbol = obj.get("symbol")
            if isinstance(symbol, str) and symbol.startswith("PERP_"):
                found.append(symbol)
            for key in ("rows", "data", "markets", "items", "symbols"):
                if key in obj:
                    walk(obj[key])
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return sorted(set(found)) or CFG.get("symbols", [])


def market_detail(symbol):
    return post_query({"type": "marketDetail", "symbol": symbol})


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = alpha * x + (1 - alpha) * value
    return value


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def build_features(symbol, data, funding):
    mi = data.get("market_info", {}) if isinstance(data, dict) else {}
    ob = data.get("orderbook", {}) if isinstance(data, dict) else {}

    candles = parse_candles(data.get("candles", []) or [])
    trades = data.get("recent_trades", []) or []

    bids = [parse_level(x) for x in (ob.get("bids", []) or [])]
    asks = [parse_level(x) for x in (ob.get("asks", []) or [])]
    bids = [(p, q) for p, q in bids if p > 0 and q > 0]
    asks = [(p, q) for p, q in asks if p > 0 and q > 0]

    mark = num(mi.get("mark_price"))
    index = num(mi.get("index_price"))
    volume24 = num(mi.get("24h_volume"))
    oi = num(mi.get("open_interest"))

    bid = bids[0][0] if bids else 0.0
    ask = asks[0][0] if asks else 0.0
    spread_bps = ((ask - bid) / mark * 10000) if mark and bid and ask else math.inf
    basis_pct = pct(mark, index) if index else 0.0

    bid_depth = sum(p*q for p, q in bids[:5])
    ask_depth = sum(p*q for p, q in asks[:5])
    depth = min(bid_depth, ask_depth)
    book_imb = (
        (bid_depth - ask_depth) / (bid_depth + ask_depth)
        if bid_depth + ask_depth else 0.0
    )

    buy_qty = sell_qty = 0.0
    for t in trades:
        if isinstance(t, dict):
            side = str(t.get("side", "")).lower()
            qty = num(t.get("executed_quantity", t.get("quantity")))
        elif isinstance(t, (list, tuple)) and len(t) >= 3:
            side = str(t[0]).lower()
            qty = num(t[2])
        else:
            continue
        if side in ("buy", "bid", "long"):
            buy_qty += qty
        elif side in ("sell", "ask", "short"):
            sell_qty += qty

    total = buy_qty + sell_qty
    trade_imb = (buy_qty - sell_qty) / total if total else 0.0

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    vols = [c["volume"] for c in candles]

    momentum5 = pct(closes[-1], closes[-6]) if len(closes) >= 6 else 0.0
    momentum15 = pct(closes[-1], closes[-16]) if len(closes) >= 16 else 0.0

    ema8 = ema(closes, 8)
    ema21 = ema(closes, 21)
    ema_gap = pct(ema8, ema21) if ema8 and ema21 else 0.0

    returns = []
    true_ranges = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        returns.append(pct(closes[i], prev))
        true_range = max(
            highs[i] - lows[i],
            abs(highs[i] - prev),
            abs(lows[i] - prev)
        )
        true_ranges.append(true_range / prev * 100)

    atr_pct = mean(true_ranges[-14:]) if true_ranges else 0.0
    returns_recent = returns[-20:]
    if len(returns_recent) >= 10:
        mu = mean(returns_recent)
        volatility_pct = math.sqrt(
            mean([(x - mu) ** 2 for x in returns_recent])
        )
    else:
        volatility_pct = 0.0

    recent_volume = mean(vols[-3:]) if len(vols) >= 3 else 0.0
    base_volume = mean(vols[-12:-3]) if len(vols) >= 12 else 0.0
    volume_accel = recent_volume / base_volume if base_volume > 0 else 1.0

    pv = sum(
        ((c["high"] + c["low"] + c["close"]) / 3) * c["volume"]
        for c in candles
    )
    vv = sum(c["volume"] for c in candles)
    vwap = pv / vv if vv > 0 else mark
    vwap_distance = pct(mark, vwap) if vwap else 0.0

    last_range_pct = (
        (highs[-1] - lows[-1]) / closes[-2] * 100
        if len(closes) >= 2 and closes[-2] > 0 else 0.0
    )
    shock_multiple = last_range_pct / atr_pct if atr_pct > 0 else 0.0
    trend_strength = abs(ema_gap) / max(atr_pct, 0.01)

    aligned = (
        (ema8 > ema21 and momentum5 > 0 and momentum15 > 0) or
        (ema8 < ema21 and momentum5 < 0 and momentum15 < 0)
    )
    if trend_strength >= 0.60 and aligned:
        regime = "TREND"
    elif trend_strength <= 0.25:
        regime = "RANGE"
    else:
        regime = "MIXED"

    return {
        "symbol": symbol,
        "mark": mark,
        "index": index,
        "volume24": volume24,
        "oi": oi,
        "bid": bid,
        "ask": ask,
        "spread_bps": spread_bps,
        "basis_pct": basis_pct,
        "depth_notional": depth,
        "book_imbalance": book_imb,
        "trade_imbalance": trade_imb,
        "momentum5": momentum5,
        "momentum15": momentum15,
        "ema_gap_pct": ema_gap,
        "atr_pct": atr_pct,
        "volatility_pct": volatility_pct,
        "volume_accel": volume_accel,
        "vwap": vwap,
        "vwap_distance_pct": vwap_distance,
        "shock_multiple": shock_multiple,
        "trend_strength": trend_strength,
        "regime": regime,
        "funding_est": funding.get("est", 0.0),
        "funding_last": funding.get("last", 0.0),
        "funding_last_ts": funding.get("last_ts"),
        "funding_next_ts": funding.get("next_ts"),
        "returns_series": returns[-24:],
        "candles_count": len(candles),
    }


def score_threshold(slot):
    f = CFG["filters"]
    return {
        0: f["min_score"],
        1: f["min_score_slot_2"],
        2: f["min_score_slot_3"],
        3: f["min_score_slot_4"]
    }.get(slot, 999.0)


def direction_score(m, direction, median_momentum):
    f = CFG["filters"]
    sign = 1 if direction == "LONG" else -1
    score = 0.0
    reasons = []
    rejects = []

    if m["volume24"] < f["min_24h_volume"]:
        rejects.append("low_volume")
    if not math.isfinite(m["spread_bps"]) or m["spread_bps"] > f["max_spread_bps"]:
        rejects.append("wide_spread")
    if m["depth_notional"] < f["min_depth_notional"]:
        rejects.append("thin_depth")
    if abs(m["basis_pct"]) > f["max_mark_index_deviation_pct"]:
        rejects.append("bad_basis")
    if m["atr_pct"] < f["min_atr_pct"]:
        rejects.append("too_quiet")
    if m["volatility_pct"] > f["max_volatility_pct"]:
        rejects.append("too_volatile")
    if m["shock_multiple"] > f["max_shock_atr_multiple"]:
        rejects.append("shock_candle")
    if m["regime"] == "RANGE":
        rejects.append("range_regime")

    if rejects:
        return 0.0, reasons, rejects

    momentum5 = sign * m["momentum5"]
    momentum15 = sign * m["momentum15"]
    ema_gap = sign * m["ema_gap_pct"]
    vwap_dist = sign * m["vwap_distance_pct"]
    trade_flow = sign * m["trade_imbalance"]
    book_flow = sign * m["book_imbalance"]
    relative = sign * (m["momentum5"] - median_momentum)

    if m["regime"] == "TREND":
        score += 15
        reasons.append("trend_regime")
    else:
        score += 5
        reasons.append("mixed_regime")

    if momentum5 >= f["min_momentum_5_pct"]:
        score += 12
        reasons.append("momentum5")
    if momentum15 > 0:
        score += 8
        reasons.append("momentum15")
    if ema_gap > 0:
        score += 8
        reasons.append("ema_alignment")
    if vwap_dist > 0:
        score += 8
        reasons.append("vwap")
    if m["volume_accel"] >= f["min_volume_acceleration"]:
        score += 10
        reasons.append("volume_acceleration")
    if trade_flow >= f["min_trade_imbalance"]:
        score += 10
        reasons.append("trade_flow")
    if book_flow >= f["min_book_imbalance"]:
        score += 5
        reasons.append("book_flow")
    if relative >= f["min_relative_strength_pct"]:
        score += 5
        reasons.append("relative_strength")

    funding_pct = m["funding_est"] * 100.0
    adverse_funding_pct = funding_pct if direction == "LONG" else -funding_pct
    if adverse_funding_pct > f["max_adverse_funding_rate_pct"]:
        rejects.append("adverse_funding")
    elif adverse_funding_pct > f["funding_penalty_start_pct"]:
        score -= 5
        reasons.append("funding_penalty")
    else:
        score += 4
        reasons.append("funding_ok")

    movement = max(abs(m["momentum5"]), m["atr_pct"])
    expected_edge = (
        movement
        - f["fee_buffer_pct_round_trip"]
        - max(0.0, adverse_funding_pct)
    )
    if expected_edge < f["min_expected_edge_pct"]:
        rejects.append("insufficient_edge")
    else:
        score += 10
        reasons.append("edge_ok")

    return clamp(score, 0.0, 100.0), reasons, rejects


def pearson_corr(a, b):
    n = min(len(a), len(b))
    if n < 8:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = mean(a), mean(b)
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return 0.0
    return sum((x-ma)*(y-mb) for x, y in zip(a, b)) / (da * db)


def portfolio_allows(state, candidate, market_map):
    r = CFG["risk"]
    positions = list(state["positions"].values())
    if len(positions) >= r["max_open_positions"]:
        return False, "max_positions"

    gross = sum(p.get("remaining_notional", 0.0) for p in positions)
    new_notional = state["equity"] * r["max_position_pct"] / 100.0
    max_gross = state["equity"] * r["max_total_position_pct"] / 100.0
    if gross + new_notional > max_gross + 1e-9:
        return False, "gross_exposure_cap"

    same_dir = sum(
        1 for p in positions if p.get("direction") == candidate["direction"]
    )
    if same_dir >= r["max_same_direction_positions"]:
        return False, "same_direction_cap"

    if candidate["market"]["symbol"] in state["positions"]:
        return False, "already_held"

    for p in positions:
        other = market_map.get(p["symbol"])
        if not other:
            continue
        corr = pearson_corr(
            candidate["market"].get("returns_series", []),
            other.get("returns_series", [])
        )
        if corr >= r["max_correlated_pair"]:
            return False, f"correlated_{corr:.2f}"

    return True, ""


def choose_direction(m, median_momentum):
    candidates = []
    for direction in ("LONG", "SHORT"):
        s, reasons, rejects = direction_score(m, direction, median_momentum)
        if rejects:
            continue
        sign = 1 if direction == "LONG" else -1
        evidence = [
            sign * m["momentum5"] > 0,
            sign * m["momentum15"] > 0,
            sign * m["ema_gap_pct"] > 0,
            sign * m["vwap_distance_pct"] > 0,
            sign * m["trade_imbalance"] >= CFG["filters"]["min_trade_imbalance"],
            sign * m["book_imbalance"] >= CFG["filters"]["min_book_imbalance"]
        ]
        if sum(evidence) >= CFG["filters"]["min_confirmations"]:
            candidates.append((s, direction, reasons))
    return max(candidates, default=None, key=lambda x: x[0])


def choose_leverage(m, score_val):
    r = CFG["risk"]
    # Leverage is a margin-efficiency setting, not permission to exceed
    # the 7% nominal position cap.
    if score_val >= 92 and m["regime"] == "TREND" and m["volatility_pct"] <= 1.5:
        lev = 5.0
    elif score_val >= 88 and m["regime"] == "TREND" and m["volatility_pct"] <= 2.0:
        lev = 3.0
    elif score_val >= 82 and m["volatility_pct"] <= 2.5:
        lev = 2.0
    else:
        lev = 1.0

    return clamp(lev, r["min_leverage"], min(r["max_leverage"], 5.0))


def dynamic_stop(m):
    r = CFG["risk"]
    stop_pct = clamp(m["atr_pct"] * 1.25, r["min_stop_pct"], r["max_stop_pct"])
    target_pct = stop_pct * r["reward_r_multiple"]
    return stop_pct, target_pct


def model_liquidation_distance(leverage):
    # Conservative paper-only approximation. The real exchange liquidation
    # formula can depend on maintenance margin, cross-margin state, fees, etc.
    # We do not use this as an exact exchange liquidation price.
    return max(0.0, 100.0 / leverage - 0.50)


def open_position(state, candidate):
    m = candidate["market"]
    direction = candidate["direction"]

    if len(state["positions"]) >= CFG["risk"]["max_open_positions"]:
        return

    entry = m["ask"] if direction == "LONG" else m["bid"]
    if entry <= 0:
        return

    equity = state["equity"]
    max_notional = equity * CFG["risk"]["max_position_pct"] / 100.0
    stop_pct, target_pct = dynamic_stop(m)
    leverage = choose_leverage(m, candidate["score"])

    liq_distance = model_liquidation_distance(leverage)
    if liq_distance < stop_pct * CFG["risk"]["min_liquidation_buffer_multiple"]:
        log_event({
            "timestamp": iso(utcnow()),
            "event": "REJECT_LIQUIDATION_BUFFER",
            "symbol": m["symbol"],
            "leverage": leverage,
            "stop_pct": stop_pct,
            "model_liq_distance_pct": liq_distance
        })
        return

    # Risk-based sizing, but never above the 7% nominal cap.
    risk_cash_target = equity * CFG["risk"]["risk_per_trade_pct"] / 100.0
    risk_based_notional = risk_cash_target / (stop_pct / 100.0)
    notional = min(max_notional, risk_based_notional)

    qty = notional / entry
    margin = notional / leverage
    risk_cash = notional * stop_pct / 100.0

    pos = {
        "symbol": m["symbol"],
        "direction": direction,
        "entry": entry,
        "initial_qty": qty,
        "remaining_qty": qty,
        "initial_notional": notional,
        "remaining_notional": notional,
        "margin_used": margin,
        "leverage": leverage,
        "stop_pct": stop_pct,
        "target_pct": target_pct,
        "stop": entry * (1 - stop_pct / 100) if direction == "LONG"
                else entry * (1 + stop_pct / 100),
        "target": entry * (1 + target_pct / 100) if direction == "LONG"
                  else entry * (1 - target_pct / 100),
        "r_distance": entry * stop_pct / 100,
        "partial_taken": False,
        "opened_at": iso(utcnow()),
        "entry_score": candidate["score"],
        "entry_reasons": candidate["reasons"],
        "funding_paid": 0.0,
        "risk_cash_at_entry": risk_cash,
        "last_funding_ts": 0
    }

    state["positions"][m["symbol"]] = pos
    state["cash"] -= margin
    state["trades"] += 1

    msg = (
        f"📈 PAPER ENTRY\n{m['symbol']} {direction}\n"
        f"Score: {candidate['score']:.0f}\n"
        f"Entry: {entry:.6f}\n"
        f"Notional: ${notional:.2f}\n"
        f"Margin: ${margin:.2f}\n"
        f"Leverage: {leverage:.1f}x\n"
        f"SL: {stop_pct:.2f}% | TP: {target_pct:.2f}%"
    )
    log_event({
        "timestamp": iso(utcnow()),
        "event": "PAPER_ENTRY",
        **pos
    })
    telegram_send(msg)


def apply_funding(state, m):
    pos = state["positions"].get(m["symbol"])
    if not pos:
        return

    last_ts = m.get("funding_last_ts")
    next_ts = m.get("funding_next_ts")
    if last_ts is None or next_ts is None:
        return

    try:
        last_ms = int(last_ts)
        opened_ms = int(
            datetime.fromisoformat(
                pos["opened_at"].replace("Z", "+00:00")
            ).timestamp() * 1000
        )
    except (TypeError, ValueError, OverflowError):
        return

    applied = int(pos.get("last_funding_ts", 0) or 0)
    now_ms = int(utcnow().timestamp() * 1000)
    if last_ms <= applied or last_ms < opened_ms or now_ms < last_ms:
        return

    rate = m["funding_last"]
    notional = pos["remaining_notional"]
    funding_pnl = -notional * rate if pos["direction"] == "LONG" else notional * rate

    pos["funding_paid"] += funding_pnl
    state["cash"] += funding_pnl
    pos["last_funding_ts"] = last_ms

    log_event({
        "timestamp": iso(utcnow()),
        "event": "PAPER_FUNDING",
        "symbol": m["symbol"],
        "direction": pos["direction"],
        "rate": rate,
        "pnl": funding_pnl,
        "funding_timestamp": last_ms
    })


def current_pnl(pos, m):
    px = m["bid"] if pos["direction"] == "LONG" else m["ask"]
    if px <= 0:
        return 0.0
    if pos["direction"] == "LONG":
        return (px - pos["entry"]) * pos["remaining_qty"]
    return (pos["entry"] - px) * pos["remaining_qty"]


def partial_take(state, m):
    pos = state["positions"].get(m["symbol"])
    if not pos:
        return

    direction = pos["direction"]
    px = m["bid"] if direction == "LONG" else m["ask"]
    favorable = px - pos["entry"] if direction == "LONG" else pos["entry"] - px
    r = pos["r_distance"]

    if pos["partial_taken"] or favorable < r * CFG["risk"]["partial_take_r"]:
        return

    fraction = CFG["risk"]["partial_take_fraction"]
    qty = pos["remaining_qty"] * fraction
    if direction == "LONG":
        gross = (px - pos["entry"]) * qty
    else:
        gross = (pos["entry"] - px) * qty

    cost = pos["remaining_notional"] * fraction * (
        CFG["filters"]["fee_buffer_pct_round_trip"] / 100.0
    )
    net = gross - cost
    released_margin = pos["margin_used"] * fraction
    state["cash"] += released_margin + net

    pos["remaining_qty"] -= qty
    pos["remaining_notional"] -= pos["initial_notional"] * fraction
    pos["margin_used"] -= released_margin
    pos["partial_taken"] = True

    lock = r * CFG["risk"]["trail_lock_r"]
    pos["stop"] = (
        max(pos["stop"], pos["entry"] + lock)
        if direction == "LONG"
        else min(pos["stop"], pos["entry"] - lock)
    )

    log_event({
        "timestamp": iso(utcnow()),
        "event": "PAPER_PARTIAL",
        "symbol": m["symbol"],
        "direction": direction,
        "exit": px,
        "fraction": fraction,
        "net_pnl": net,
    })


def close_position(state, m, reason):
    pos = state["positions"].get(m["symbol"])
    if not pos:
        return

    direction = pos["direction"]
    px = m["bid"] if direction == "LONG" else m["ask"]
    if px <= 0:
        return

    if direction == "LONG":
        gross = (px - pos["entry"]) * pos["remaining_qty"]
    else:
        gross = (pos["entry"] - px) * pos["remaining_qty"]

    cost = pos["remaining_notional"] * (
        CFG["filters"]["fee_buffer_pct_round_trip"] / 100.0
    )
    net_close = gross - cost
    total_net = net_close + pos.get("funding_paid", 0.0)

    state["cash"] += pos["margin_used"] + net_close
    state["realized_pnl"] += total_net

    if total_net >= 0:
        state["wins"] += 1
        state["gross_profit"] += total_net
        state["consecutive_losses"] = 0
    else:
        state["losses"] += 1
        state["gross_loss"] += abs(total_net)
        state["consecutive_losses"] += 1
        cooldown = utcnow() + timedelta(minutes=CFG["risk"]["loss_cooldown_min"])
        state["cooldown_until"] = iso(cooldown)

    log_event({
        "timestamp": iso(utcnow()),
        "event": "PAPER_EXIT",
        "symbol": m["symbol"],
        "direction": direction,
        "reason": reason,
        "entry": pos["entry"],
        "exit": px,
        "leverage": pos["leverage"],
        "net_pnl": total_net,
        "funding_paid": pos.get("funding_paid", 0.0)
    })

    telegram_send(
        f"📉 PAPER EXIT\n{m['symbol']} {direction}\n"
        f"Reason: {reason}\n"
        f"Entry: {pos['entry']:.6f}\nExit: {px:.6f}\n"
        f"Net P/L: ${total_net:+.2f}\n"
        f"Leverage: {pos['leverage']:.1f}x"
    )
    del state["positions"][m["symbol"]]


def manage_position(state, m):
    pos = state["positions"].get(m["symbol"])
    if not pos:
        return

    direction = pos["direction"]
    px = m["bid"] if direction == "LONG" else m["ask"]
    favorable = px - pos["entry"] if direction == "LONG" else pos["entry"] - px
    r = pos["r_distance"]

    partial_take(state, m)
    pos = state["positions"].get(m["symbol"])
    if not pos:
        return

    if favorable >= CFG["risk"]["trail_start_r"] * r:
        if direction == "LONG":
            trail = px - CFG["risk"]["trail_distance_r"] * r
            lock = pos["entry"] + CFG["risk"]["trail_lock_r"] * r
            pos["stop"] = max(pos["stop"], trail, lock)
        else:
            trail = px + CFG["risk"]["trail_distance_r"] * r
            lock = pos["entry"] - CFG["risk"]["trail_lock_r"] * r
            pos["stop"] = min(pos["stop"], trail, lock)

    # Strong invalidation: the original directional evidence has flipped.
    thesis_invalid = (
        (direction == "LONG"
         and m["momentum5"] < -0.20
         and m["trade_imbalance"] < -0.15)
        or
        (direction == "SHORT"
         and m["momentum5"] > 0.20
         and m["trade_imbalance"] > 0.15)
    )
    if thesis_invalid:
        close_position(state, m, "THESIS_INVALIDATION")
        return

    if direction == "LONG":
        if px <= pos["stop"]:
            close_position(state, m, "STOP/TRAIL")
            return
        if px >= pos["target"]:
            close_position(state, m, "TAKE_PROFIT")
            return
    else:
        if px >= pos["stop"]:
            close_position(state, m, "STOP/TRAIL")
            return
        if px <= pos["target"]:
            close_position(state, m, "TAKE_PROFIT")
            return

    opened = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
    minutes_open = (utcnow() - opened).total_seconds() / 60.0
    if minutes_open >= CFG["risk"]["max_holding_minutes"] and favorable < 0.5 * r:
        close_position(state, m, "TIME_STOP")


def mark_equity(state, market_map):
    unrealized = 0.0
    margin_used = 0.0

    for sym, pos in state["positions"].items():
        m = market_map.get(sym)
        if not m:
            continue
        unrealized += current_pnl(pos, m)
        margin_used += pos.get("margin_used", 0.0)

    state["unrealized_pnl"] = unrealized
    state["equity"] = state["cash"] + margin_used + unrealized
    state["peak_equity"] = max(state["peak_equity"], state["equity"])


def risk_halt(state):
    now = utcnow()
    if state["day"] != now.date().isoformat():
        state["day"] = now.date().isoformat()
        state["day_start_equity"] = state["equity"]
        state["halt_reason"] = None

    day_loss = (
        (state["equity"] / state["day_start_equity"] - 1) * 100
        if state["day_start_equity"] else 0
    )
    drawdown = (
        (state["equity"] / state["peak_equity"] - 1) * 100
        if state["peak_equity"] else 0
    )

    if day_loss <= -CFG["risk"]["max_daily_loss_pct"]:
        return True, "DAILY_LOSS_LIMIT"
    if drawdown <= -CFG["risk"]["max_equity_drawdown_pct"]:
        return True, "MAX_DRAWDOWN"

    if state.get("cooldown_until"):
        try:
            if now < datetime.fromisoformat(state["cooldown_until"]):
                return True, "LOSS_COOLDOWN"
        except ValueError:
            state["cooldown_until"] = None

    return False, ""


def write_report(state, scan_count):
    closed = state["wins"] + state["losses"]
    win_rate = state["wins"] / closed * 100 if closed else 0.0
    pf = (
        state["gross_profit"] / state["gross_loss"]
        if state["gross_loss"] > 0 else None
    )

    report = {
        "generated_at": iso(utcnow()),
        "equity": state["equity"],
        "cash": state["cash"],
        "realized_pnl": state["realized_pnl"],
        "unrealized_pnl": state["unrealized_pnl"],
        "closed_trades": closed,
        "win_rate_pct": win_rate,
        "profit_factor": pf,
        "peak_equity": state["peak_equity"],
        "open_positions": len(state["positions"]),
        "max_position_pct": CFG["risk"]["max_position_pct"],
        "max_total_exposure_pct": CFG["risk"]["max_total_position_pct"],
        "live_order_execution": False,
        "scan_count": scan_count
    }
    REPORT_FILE.write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8"
    )


def maybe_hourly_report(state, markets_count):
    now = utcnow()
    hour_key = now.strftime("%Y-%m-%d %H")

    # Persist the snapshot so a Render restart does not silently reset the
    # hourly accounting. The report is for the completed previous hour.
    snapshot_key = state.get("hour_report_key")
    if snapshot_key is None:
        state["hour_report_key"] = hour_key
        state["hour_start_equity"] = state["equity"]
        state["hour_start_trades"] = state["trades"]
        state["hour_start_wins"] = state["wins"]
        state["hour_start_losses"] = state["losses"]
        state["hour_start_gross_profit"] = state["gross_profit"]
        state["hour_start_gross_loss"] = state["gross_loss"]
        return

    if snapshot_key == hour_key:
        return

    start_equity = num(state.get("hour_start_equity"), state["equity"])
    start_trades = int(state.get("hour_start_trades", state["trades"]))
    start_wins = int(state.get("hour_start_wins", state["wins"]))
    start_losses = int(state.get("hour_start_losses", state["losses"]))
    start_gp = num(state.get("hour_start_gross_profit"))
    start_gl = num(state.get("hour_start_gross_loss"))

    hourly_entries = max(0, state["trades"] - start_trades)
    hourly_wins = max(0, state["wins"] - start_wins)
    hourly_losses = max(0, state["losses"] - start_losses)
    hourly_closed = hourly_wins + hourly_losses
    hourly_profit = state["equity"] - start_equity
    hourly_gross_profit = state["gross_profit"] - start_gp
    hourly_gross_loss = state["gross_loss"] - start_gl

    telegram_send(
        f"📊 PAPER RAPORT — OSTATNIA 1H\n"
        f"💰 Start: ${start_equity:.2f}\n"
        f"💰 Koniec: ${state['equity']:.2f}\n"
        f"📈 Zysk: ${hourly_profit:+.2f}\n\n"
        f"🔄 Transakcje zamknięte: {hourly_closed}\n"
        f"🟢 Wygrane: {hourly_wins}\n"
        f"🔴 Przegrane: {hourly_losses}\n"
        f"📥 Nowe wejścia: {hourly_entries}\n\n"
        f"📈 Zyskowne P/L: ${hourly_gross_profit:+.2f}\n"
        f"📉 Stratne P/L: ${-hourly_gross_loss:+.2f}\n"
        f"📂 Otwarte pozycje: {len(state['positions'])}/{CFG['risk']['max_open_positions']}\n"
        f"🔎 Rynki skanowane: {markets_count}\n"
        f"📊 Łączny P/L: ${state['equity'] - CFG['starting_equity']:+.2f}"
    )

    state["hour_report_key"] = hour_key
    state["hour_start_equity"] = state["equity"]
    state["hour_start_trades"] = state["trades"]
    state["hour_start_wins"] = state["wins"]
    state["hour_start_losses"] = state["losses"]
    state["hour_start_gross_profit"] = state["gross_profit"]
    state["hour_start_gross_loss"] = state["gross_loss"]


def maybe_heartbeat(state, markets_count, last_heartbeat):
    interval = CFG.get("heartbeat_minutes", 15) * 60
    if time.time() - last_heartbeat < interval:
        return last_heartbeat

    if CFG.get("telegram", {}).get("enabled") and CFG.get("telegram", {}).get("send_heartbeat"):
        pnl = state["equity"] - CFG["starting_equity"]
        telegram_send(
            f"🤖 PAPER HEARTBEAT\n"
            f"Equity: ${state['equity']:.2f}\n"
            f"Total P/L: ${pnl:+.2f}\n"
            f"Open: {len(state['positions'])}/{CFG['risk']['max_open_positions']}\n"
            f"Markets scanned: {markets_count}\n"
            f"Status: {state.get('halt_reason') or 'RUNNING'}"
        )
    return time.time()


def main():
    state = load_state()
    scan_count = 0
    last_heartbeat = 0.0
    last_halt_reason = None

    print("RWAPerp Agent FINAL — PAPER ONLY")
    print("LIVE ORDER EXECUTION: DISABLED")
    print("Max 4 positions | 7% nominal position cap | 28% gross exposure cap")
    print("Dynamic leverage model: 1x–5x (does NOT increase nominal exposure)")
    print(f"Starting/current equity: ${state['equity']:.2f}")

    telegram_send(
        f"🤖 RWAPerp Agent FINAL ONLINE\n"
        f"PAPER ONLY\nEquity: ${state['equity']:.2f}\n"
        f"Max position: 7% | Max positions: 4"
    )

    while True:
        try:
            halted, reason = risk_halt(state)
            state["halt_reason"] = reason or None
            if reason and reason != last_halt_reason:
                telegram_send(f"🛑 RISK HALT\nReason: {reason}")
                last_halt_reason = reason
            elif not reason:
                last_halt_reason = None

            symbols = CFG.get("symbols") or discover_symbols()
            funding = get_funding_rates()
            markets = []
            market_map = {}

            for symbol in symbols:
                try:
                    m = build_features(
                        symbol,
                        market_detail(symbol),
                        funding.get(symbol, {})
                    )
                    if min(m["mark"], m["bid"], m["ask"]) <= 0:
                        continue
                    markets.append(m)
                    market_map[symbol] = m
                    log_event({
                        "timestamp": iso(utcnow()),
                        "event": "SCAN",
                        "market": m
                    })
                except Exception as exc:
                    log_event({
                        "timestamp": iso(utcnow()),
                        "event": "SCAN_ERROR",
                        "symbol": symbol,
                        "error": str(exc)
                    })

            scan_count += 1

            # First manage existing positions.
            for m in markets:
                if m["symbol"] in state["positions"]:
                    apply_funding(state, m)
                    manage_position(state, m)

            mark_equity(state, market_map)

            if not halted:
                median_momentum = median(
                    [m["momentum5"] for m in markets]
                ) if markets else 0.0

                # Attempt to fill available slots. Every additional position
                # requires a higher score and portfolio diversification check.
                for _ in range(
                    max(0, CFG["risk"]["max_open_positions"] - len(state["positions"]))
                ):
                    candidates = []

                    for m in markets:
                        if m["symbol"] in state["positions"]:
                            continue
                        result = choose_direction(m, median_momentum)
                        if result:
                            score_val, direction, reasons = result
                            candidates.append({
                                "score": score_val,
                                "direction": direction,
                                "market": m,
                                "reasons": reasons
                            })

                    candidates.sort(key=lambda x: x["score"], reverse=True)
                    if not candidates:
                        break

                    slot = len(state["positions"])
                    threshold = score_threshold(slot)
                    chosen = None

                    for candidate in candidates:
                        if candidate["score"] < threshold:
                            break
                        ok, reject_reason = portfolio_allows(
                            state, candidate, market_map
                        )
                        if ok:
                            chosen = candidate
                            break
                        log_event({
                            "timestamp": iso(utcnow()),
                            "event": "CANDIDATE_REJECTED",
                            "symbol": candidate["market"]["symbol"],
                            "direction": candidate["direction"],
                            "score": candidate["score"],
                            "reason": reject_reason
                        })

                    if not chosen:
                        break

                    log_event({
                        "timestamp": iso(utcnow()),
                        "event": "SELECT",
                        "symbol": chosen["market"]["symbol"],
                        "direction": chosen["direction"],
                        "score": chosen["score"],
                        "slot": slot + 1,
                        "reasons": chosen["reasons"]
                    })
                    open_position(state, chosen)

            mark_equity(state, market_map)
            write_report(state, scan_count)
            save_state(state)

            maybe_hourly_report(state, len(markets))
            save_state(state)

            last_heartbeat = maybe_heartbeat(
                state, len(markets), last_heartbeat
            )

            print(
                f"[HEARTBEAT] equity=${state['equity']:.2f} "
                f"realized={state['realized_pnl']:+.2f} "
                f"unrealized={state['unrealized_pnl']:+.2f} "
                f"positions={len(state['positions'])} "
                f"wins={state['wins']} losses={state['losses']} "
                f"halt={state['halt_reason'] or '-'}"
            )

          time.sleep(2)

        except KeyboardInterrupt:
            save_state(state)
            print("Stopped.")
            break
        except Exception as exc:
            log_event({
                "timestamp": iso(utcnow()),
                "event": "LOOP_ERROR",
                "error": str(exc)
            })
            save_state(state)
            telegram_send(f"⚠️ AGENT ERROR\n{type(exc).__name__}: {exc}")
            time.sleep(CFG["poll_seconds"])


if __name__ == "__main__":
    main()
