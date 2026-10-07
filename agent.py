
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
MARKET_CACHE = {}
MARKET_CACHE_TS = {}
SYMBOL_CURSOR = 0
LAST_SYMBOL_REFRESH = 0.0


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


def load_legacy_trade_history():
    """Recover prior PAPER_EXIT events so an upgrade does not hide old trades."""
    if not EVENT_FILE.exists():
        return []
    out = []
    try:
        for line in EVENT_FILE.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                if e.get("event") == "PAPER_EXIT":
                    out.append(e)
            except Exception:
                continue
    except Exception:
        return []
    return out


def load_state():
    def migrate_position(pos):
        entry = float(pos.get("entry", 0.0) or 0.0)
        direction = pos.get("direction", "LONG")
        stop_pct = float(CFG["risk"].get("stop_loss_pct", 15.0))
        if entry > 0:
            pos["stop_pct"] = stop_pct
            pos["target_pct"] = None
            pos["target"] = None
            pos["r_distance"] = entry * stop_pct / 100.0
            # Existing positions must use the new hard-stop regime after deploy.
            pos["stop"] = entry * (1 - stop_pct / 100.0) if direction == "LONG" else entry * (1 + stop_pct / 100.0)
            pos.setdefault("peak_price", entry)
            pos.setdefault("peak_profit_pct", 0.0)
            pos.setdefault("trailing_active", False)
            pos.setdefault("trail_retrace_pct", None)
            pos.setdefault("current_price", entry)
            pos.setdefault("current_pnl", 0.0)
            pos.setdefault("current_profit_pct", 0.0)
            pos.setdefault("candles", [])
        return pos

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
        state.setdefault("equity_history", [])
        state.setdefault("daily_history", {})
        state.setdefault("trade_history", [])
        if not state["trade_history"]:
            state["trade_history"] = load_legacy_trade_history()
        state.setdefault("scanner_stats", {"markets_seen": 0, "candidate_markets": 0, "selected": 0, "portfolio_rejected": 0, "rejections": {}})
        state.setdefault("strategy_stats", {"by_market": {}, "by_exit_reason": {}, "by_entry_reason": {}, "by_score_bucket": {}})
        state.setdefault("max_drawdown_pct", 0.0)
        state.setdefault("btc_symbol", None)
        state.setdefault("btc_regime", "UNKNOWN")
        state.setdefault("last_equity_sample_ts", 0.0)
        state.setdefault("last_loop_ts", None)
        state.setdefault("last_scan_ts", None)
        state.setdefault("last_error", None)
        state.setdefault("api_errors", 0)
        state["positions"] = {sym: migrate_position(pos) for sym, pos in state.get("positions", {}).items()}
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
        "max_drawdown_pct": 0.0,
        "btc_symbol": None,
        "btc_regime": "UNKNOWN",
        "equity_history": [],
        "daily_history": {},
        "trade_history": [],
        "strategy_stats": {"by_market": {}, "by_exit_reason": {}, "by_entry_reason": {}, "by_score_bucket": {}},
        "scanner_stats": {"markets_seen": 0, "candidate_markets": 0, "selected": 0, "portfolio_rejected": 0, "rejections": {}},
        "last_equity_sample_ts": 0.0,
        "last_loop_ts": None,
        "last_scan_ts": None,
        "last_error": None,
        "api_errors": 0,
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


def refresh_market_batch(symbols, funding):
    """Refresh a small batch only. Three workers keeps API pressure bounded."""
    results = {}

    def one(symbol):
        try:
            raw = market_detail(symbol)
            m = build_features(symbol, raw, funding.get(symbol, {}))
            if min(m["mark"], m["bid"], m["ask"]) <= 0:
                return symbol, None, "invalid_price"
            return symbol, m, None
        except Exception as exc:
            return symbol, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(one, symbol) for symbol in symbols]
        for fut in as_completed(futures):
            symbol, m, err = fut.result()
            if m is not None:
                results[symbol] = m
                MARKET_CACHE[symbol] = m
                MARKET_CACHE_TS[symbol] = time.time()
                log_event({"timestamp": iso(utcnow()), "event": "SCAN", "market": m})
            else:
                log_event({"timestamp": iso(utcnow()), "event": "SCAN_ERROR", "symbol": symbol, "error": err})
    return results


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
        "candles": candles[-30:],
    }


def score_threshold(slot):
    f = CFG["filters"]
    return {
        0: f["min_score"],
        1: f["min_score_slot_2"],
        2: f["min_score_slot_3"],
        3: f["min_score_slot_4"]
    }.get(slot, 999.0)



def find_btc_symbol(symbols):
    """Find a BTC reference market without assuming one exact symbol format."""
    preferred = (
        "PERP_BTCUSDT", "PERP_BTC_USDT", "PERP_BTC-USD", "PERP_BTC_USD",
        "PERP_BTCUSD", "PERP_BTC"
    )
    upper = {str(x).upper(): x for x in symbols}
    for p in preferred:
        if p in upper:
            return upper[p]
    for sym in symbols:
        u = str(sym).upper()
        if u.startswith("PERP_") and "BTC" in u and ("USD" in u or "USDT" in u):
            return sym
    return None


def btc_regime_from_market(btc):
    if not btc:
        return "UNKNOWN"
    min_mom = float(CFG["filters"].get("btc_min_momentum_pct", 0.10))
    if (
        btc.get("regime") == "TREND"
        and btc.get("momentum5", 0.0) >= min_mom
        and btc.get("momentum15", 0.0) > 0
        and btc.get("ema_gap_pct", 0.0) > 0
    ):
        return "BULLISH"
    if (
        btc.get("regime") == "TREND"
        and btc.get("momentum5", 0.0) <= -min_mom
        and btc.get("momentum15", 0.0) < 0
        and btc.get("ema_gap_pct", 0.0) < 0
    ):
        return "BEARISH"
    return "NEUTRAL"


def apply_btc_context(market_map, btc_symbol):
    """Annotate all markets with the latest BTC regime used for entry filtering."""
    btc = market_map.get(btc_symbol) if btc_symbol else None
    regime = btc_regime_from_market(btc)
    btc_m5 = num(btc.get("momentum5")) if btc else 0.0
    btc_m15 = num(btc.get("momentum15")) if btc else 0.0
    btc_ema = num(btc.get("ema_gap_pct")) if btc else 0.0
    for market in market_map.values():
        market["btc_symbol"] = btc_symbol
        market["btc_regime"] = regime
        market["btc_momentum5"] = btc_m5
        market["btc_momentum15"] = btc_m15
        market["btc_ema_gap_pct"] = btc_ema
    return regime


def overextension_metrics(m):
    """Measure whether price is already too far through a recent breakout."""
    candles = m.get("candles", []) or []
    lookback = int(CFG["filters"].get("overextension_lookback", 20))
    if len(candles) < max(lookback, 5):
        return {"long_extension_pct": 0.0, "short_extension_pct": 0.0, "momentum_atr_multiple": 0.0}
    prior = candles[-lookback:-1]
    prior_high = max(num(c.get("high")) for c in prior)
    prior_low = min(num(c.get("low")) for c in prior)
    mark = num(m.get("mark"))
    long_ext = max(0.0, (mark / prior_high - 1.0) * 100.0) if prior_high > 0 else 0.0
    short_ext = max(0.0, (prior_low / mark - 1.0) * 100.0) if mark > 0 and prior_low > 0 else 0.0
    atr = num(m.get("atr_pct"))
    mom_atr = abs(num(m.get("momentum5"))) / atr if atr > 0 else 0.0
    return {
        "long_extension_pct": long_ext,
        "short_extension_pct": short_ext,
        "momentum_atr_multiple": mom_atr,
    }


def add_entry_context(m):
    ext = overextension_metrics(m)
    m.update({f"overext_{k}": v for k, v in ext.items()})
    return m

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

    btc_regime = str(m.get("btc_regime", "UNKNOWN"))
    if direction == "LONG" and btc_regime == "BEARISH":
        rejects.append("btc_regime_against_long")
    elif direction == "SHORT" and btc_regime == "BULLISH":
        rejects.append("btc_regime_against_short")

    max_ext = float(f.get("max_breakout_extension_pct", 0.75))
    max_mom_atr = float(f.get("max_momentum_atr_multiple", 2.5))
    long_ext = num(m.get("overext_long_extension_pct"))
    short_ext = num(m.get("overext_short_extension_pct"))
    mom_atr = num(m.get("overext_momentum_atr_multiple"))
    direction_ext = long_ext if direction == "LONG" else short_ext
    if direction_ext > max_ext:
        rejects.append("overextended_breakout")
    if mom_atr > max_mom_atr and sign * m["momentum5"] > 0:
        rejects.append("momentum_overextension")

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

    # BTC alignment is worth 5 points and also makes the 0-100 score genuinely reachable.
    if (direction == "LONG" and btc_regime == "BULLISH") or (direction == "SHORT" and btc_regime == "BEARISH"):
        score += 5
        reasons.append("btc_alignment")
    elif btc_regime == "NEUTRAL":
        reasons.append("btc_neutral")

    reasons.append("overextension_ok")

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
        - (2.0 * float(f.get("slippage_pct_per_side", 0.0)))
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
    exceptional = (
        candidate.get("score", 0.0) >= float(r.get("exceptional_setup_min_score", 999.0))
        and candidate["market"].get("regime") == "TREND"
    )
    if exceptional:
        exceptional_open = sum(1 for p in positions if p.get("exceptional_setup"))
        if exceptional_open >= int(r.get("max_exceptional_positions", 1)):
            return False, "exceptional_position_cap"
    position_pct = float(r.get("exceptional_position_pct", r["max_position_pct"])) if exceptional else float(r["max_position_pct"])
    new_notional = state["equity"] * position_pct / 100.0
    max_gross = state["equity"] * r["max_total_position_pct"] / 100.0
    if gross + new_notional > max_gross + 1e-9:
        return False, "gross_exposure_cap"

    same_dir = sum(1 for p in positions if p.get("direction") == candidate["direction"])
    if same_dir >= r["max_same_direction_positions"]:
        return False, "same_direction_cap"

    if candidate["market"]["symbol"] in state["positions"]:
        return False, "already_held"

    correlated_count = 0
    correlated = []
    for p in positions:
        other = market_map.get(p["symbol"])
        if not other:
            continue
        corr = pearson_corr(
            candidate["market"].get("returns_series", []),
            other.get("returns_series", [])
        )
        if corr >= r["max_correlated_pair"]:
            correlated_count += 1
            correlated.append((p.get("symbol"), corr))

    max_corr_positions = int(r.get("max_correlated_positions", 2))
    if correlated_count >= max_corr_positions:
        return False, f"correlated_cluster_{correlated_count + 1}"

    candidate["correlated_open_count"] = correlated_count
    candidate["correlated_open"] = correlated
    return True, ""

def choose_direction(m, median_momentum):
    add_entry_context(m)
    candidates = []
    reject_reasons = []
    for direction in ("LONG", "SHORT"):
        s, reasons, rejects = direction_score(m, direction, median_momentum)
        if rejects:
            reject_reasons.extend(rejects)
            continue
        sign = 1 if direction == "LONG" else -1
        evidence = [
            sign * m["momentum5"] > 0,
            sign * m["momentum15"] > 0,
            sign * m["ema_gap_pct"] > 0,
            sign * m["vwap_distance_pct"] > 0,
            sign * m["trade_imbalance"] >= CFG["filters"]["min_trade_imbalance"],
            sign * m["book_imbalance"] >= CFG["filters"]["min_book_imbalance"],
        ]
        confirmation_count = sum(evidence)
        if confirmation_count >= CFG["filters"]["min_confirmations"]:
            reasons = list(reasons) + [f"confirmations_{confirmation_count}"]
            candidates.append((s, direction, reasons))
        else:
            reject_reasons.append("insufficient_confirmations")
    m["_reject_reasons"] = reject_reasons
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
    """Fixed paper stop: 15% adverse move on the position price.
    There is deliberately no fixed take-profit; winners are managed by trailing.
    """
    stop_pct = float(CFG["risk"].get("stop_loss_pct", 15.0))
    return stop_pct, None


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

    raw_entry = m["ask"] if direction == "LONG" else m["bid"]
    if raw_entry <= 0:
        return

    slippage = float(CFG["filters"].get("slippage_pct_per_side", 0.0))
    entry = raw_entry * (1.0 + slippage / 100.0) if direction == "LONG" else raw_entry * (1.0 - slippage / 100.0)
    equity = state["equity"]
    exceptional = (
        candidate.get("score", 0.0) >= float(CFG["risk"].get("exceptional_setup_min_score", 999.0))
        and m.get("regime") == "TREND"
    )
    position_pct = float(CFG["risk"].get("exceptional_position_pct", CFG["risk"]["max_position_pct"])) if exceptional else float(CFG["risk"]["max_position_pct"])
    max_notional = equity * position_pct / 100.0
    stop_pct, _ = dynamic_stop(m)
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

    risk_pct = (
        float(CFG["risk"].get("exceptional_risk_per_trade_pct", CFG["risk"]["risk_per_trade_pct"]))
        if exceptional else float(CFG["risk"]["risk_per_trade_pct"])
    )
    risk_cash_target = equity * risk_pct / 100.0
    risk_based_notional = risk_cash_target / (stop_pct / 100.0)
    notional = min(max_notional, risk_based_notional)

    qty = notional / entry
    margin = notional / leverage
    risk_cash = notional * stop_pct / 100.0

    pos = {
        "symbol": m["symbol"],
        "direction": direction,
        "entry": entry,
        "raw_entry": raw_entry,
        "initial_qty": qty,
        "remaining_qty": qty,
        "initial_notional": notional,
        "remaining_notional": notional,
        "margin_used": margin,
        "leverage": leverage,
        "stop_pct": stop_pct,
        "target_pct": None,
        "peak_price": entry,
        "peak_profit_pct": 0.0,
        "trailing_active": False,
        "trail_retrace_pct": None,
        "stop": entry * (1 - stop_pct / 100) if direction == "LONG" else entry * (1 + stop_pct / 100),
        "target": None,
        "r_distance": entry * stop_pct / 100,
        "partial_taken": False,
        "opened_at": iso(utcnow()),
        "entry_score": candidate["score"],
        "entry_reasons": candidate["reasons"],
        "funding_paid": 0.0,
        "risk_cash_at_entry": risk_cash,
        "last_funding_ts": 0,
        "current_price": entry,
        "current_pnl": 0.0,
        "current_profit_pct": 0.0,
        "candles": m.get("candles", [])[-30:],
        "exceptional_setup": exceptional,
        "position_pct": position_pct,
        "btc_symbol": m.get("btc_symbol"),
        "btc_regime": m.get("btc_regime", "UNKNOWN"),
        "btc_momentum5": m.get("btc_momentum5", 0.0),
        "btc_momentum15": m.get("btc_momentum15", 0.0),
        "overext_long_extension_pct": m.get("overext_long_extension_pct", 0.0),
        "overext_short_extension_pct": m.get("overext_short_extension_pct", 0.0),
        "overext_momentum_atr_multiple": m.get("overext_momentum_atr_multiple", 0.0),
        "correlated_open_count": candidate.get("correlated_open_count", 0),
        "entry_snapshot": {
            "mark": m.get("mark"),
            "volume24": m.get("volume24"),
            "oi": m.get("oi"),
            "spread_bps": m.get("spread_bps"),
            "depth_notional": m.get("depth_notional"),
            "basis_pct": m.get("basis_pct"),
            "momentum5": m.get("momentum5"),
            "momentum15": m.get("momentum15"),
            "ema_gap_pct": m.get("ema_gap_pct"),
            "atr_pct": m.get("atr_pct"),
            "volatility_pct": m.get("volatility_pct"),
            "volume_accel": m.get("volume_accel"),
            "trade_imbalance": m.get("trade_imbalance"),
            "book_imbalance": m.get("book_imbalance"),
            "vwap_distance_pct": m.get("vwap_distance_pct"),
            "shock_multiple": m.get("shock_multiple"),
            "trend_strength": m.get("trend_strength"),
            "regime": m.get("regime"),
            "funding_est": m.get("funding_est"),
            "btc_regime": m.get("btc_regime", "UNKNOWN"),
            "btc_momentum5": m.get("btc_momentum5", 0.0),
            "btc_momentum15": m.get("btc_momentum15", 0.0),
            "overext_long_extension_pct": m.get("overext_long_extension_pct", 0.0),
            "overext_short_extension_pct": m.get("overext_short_extension_pct", 0.0),
            "overext_momentum_atr_multiple": m.get("overext_momentum_atr_multiple", 0.0),
            "correlated_open_count": candidate.get("correlated_open_count", 0),
        },
    }

    state["positions"][m["symbol"]] = pos
    state["cash"] -= margin
    state["trades"] += 1

    msg = (
        f"📈 PAPER ENTRY\n{m['symbol']} {direction}\n"
        f"Score: {candidate['score']:.0f}\n"
        f"Entry: {entry:.6f}\n"
        f"Notional: ${notional:.2f} ({position_pct:.1f}%)\n"
        f"Margin: ${margin:.2f}\n"
        f"Leverage: {leverage:.1f}x\n"
        f"SL: -{stop_pct:.1f}% | Trailing: starts +{CFG['risk'].get('trail_activation_pct',10.0):.1f}%"
        + ("\n⭐ EXCEPTIONAL SETUP" if exceptional else "")
    )
    log_event({"timestamp": iso(utcnow()), "event": "PAPER_ENTRY", **pos})
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
    # Fixed full-position management: no partial TP. The whole position is
    # protected by the -15% hard stop and profit trailing.
    return



def score_bucket(score):
    s = float(score or 0.0)
    if s < 75:
        return "<75"
    if s < 80:
        return "75-79"
    if s < 85:
        return "80-84"
    if s < 90:
        return "85-89"
    if s < 94:
        return "90-93"
    return "94-100"

def close_position(state, m, reason):
    pos = state["positions"].get(m["symbol"])
    if not pos:
        return

    direction = pos["direction"]
    raw_px = m["bid"] if direction == "LONG" else m["ask"]
    if raw_px <= 0:
        return
    slippage = float(CFG["filters"].get("slippage_pct_per_side", 0.0))
    px = raw_px * (1.0 - slippage / 100.0) if direction == "LONG" else raw_px * (1.0 + slippage / 100.0)

    if direction == "LONG":
        gross = (px - pos["entry"]) * pos["remaining_qty"]
    else:
        gross = (pos["entry"] - px) * pos["remaining_qty"]

    cost = pos["remaining_notional"] * (CFG["filters"]["fee_buffer_pct_round_trip"] / 100.0)
    net_close = gross - cost
    total_net = net_close + pos.get("funding_paid", 0.0)

    opened = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
    hold_minutes = max(0.0, (utcnow() - opened).total_seconds() / 60.0)
    peak_profit = float(pos.get("peak_profit_pct", 0.0))

    state["cash"] += pos["margin_used"] + net_close
    state["realized_pnl"] += total_net

    cooldown_triggered = False
    if total_net >= 0:
        state["wins"] += 1
        state["gross_profit"] += total_net
        state["consecutive_losses"] = 0
    else:
        state["losses"] += 1
        state["gross_loss"] += abs(total_net)
        state["consecutive_losses"] += 1
        max_losses = int(CFG["risk"].get("max_consecutive_losses", 3))
        if state["consecutive_losses"] >= max_losses:
            cooldown = utcnow() + timedelta(minutes=CFG["risk"]["loss_cooldown_min"])
            state["cooldown_until"] = iso(cooldown)
            cooldown_triggered = True

    entry_score = float(pos.get("entry_score", 0.0))
    bucket = score_bucket(entry_score)
    event = {
        "timestamp": iso(utcnow()),
        "event": "PAPER_EXIT",
        "symbol": m["symbol"],
        "direction": direction,
        "reason": reason,
        "entry": pos["entry"],
        "exit": px,
        "leverage": pos["leverage"],
        "net_pnl": total_net,
        "funding_paid": pos.get("funding_paid", 0.0),
        "entry_score": entry_score,
        "score_bucket": bucket,
        "entry_reasons": pos.get("entry_reasons", []),
        "peak_profit_pct": peak_profit,
        "hold_minutes": hold_minutes,
        "exceptional_setup": bool(pos.get("exceptional_setup", False)),
        "position_pct": pos.get("position_pct", CFG["risk"]["max_position_pct"]),
        "btc_symbol": pos.get("btc_symbol"),
        "btc_regime": pos.get("btc_regime", "UNKNOWN"),
        "btc_momentum5": pos.get("btc_momentum5", 0.0),
        "btc_momentum15": pos.get("btc_momentum15", 0.0),
        "overext_long_extension_pct": pos.get("overext_long_extension_pct", 0.0),
        "overext_short_extension_pct": pos.get("overext_short_extension_pct", 0.0),
        "overext_momentum_atr_multiple": pos.get("overext_momentum_atr_multiple", 0.0),
        "correlated_open_count": pos.get("correlated_open_count", 0),
        "consecutive_losses_after_close": state.get("consecutive_losses", 0),
        "cooldown_triggered": cooldown_triggered,
        "entry_snapshot": pos.get("entry_snapshot", {}),
    }
    log_event(event)
    state.setdefault("trade_history", []).append(event)

    stats = state.setdefault("strategy_stats", {"by_market": {}, "by_exit_reason": {}, "by_entry_reason": {}, "by_score_bucket": {}})
    ms = stats.setdefault("by_market", {}).setdefault(m["symbol"], {"trades": 0, "wins": 0, "pnl": 0.0})
    ms["trades"] += 1
    ms["wins"] += int(total_net >= 0)
    ms["pnl"] += total_net
    rs = stats.setdefault("by_exit_reason", {}).setdefault(reason, {"trades": 0, "pnl": 0.0})
    rs["trades"] += 1
    rs["pnl"] += total_net
    ss = stats.setdefault("by_score_bucket", {}).setdefault(bucket, {"trades": 0, "wins": 0, "pnl": 0.0})
    ss["trades"] += 1
    ss["wins"] += int(total_net >= 0)
    ss["pnl"] += total_net
    for er in pos.get("entry_reasons", []):
        es = stats.setdefault("by_entry_reason", {}).setdefault(er, {"trades": 0, "pnl": 0.0})
        es["trades"] += 1
        es["pnl"] += total_net

    telegram_send(
        f"📉 PAPER EXIT\n{m['symbol']} {direction}\n"
        f"Reason: {reason}\nEntry: {pos['entry']:.6f}\nExit: {px:.6f}\n"
        f"Net P/L: ${total_net:+.2f}\nPeak: +{peak_profit:.1f}%\n"
        f"Hold: {hold_minutes:.1f}m\nScore: {entry_score:.0f}\n"
        f"BTC: {pos.get('btc_regime','UNKNOWN')}\nLeverage: {pos['leverage']:.1f}x"
        + ("\n🛑 LOSS COOLDOWN" if cooldown_triggered else "")
    )
    del state["positions"][m["symbol"]]

def trailing_retrace_pct(peak_profit_pct):
    r = CFG["risk"]
    if peak_profit_pct >= 100.0:
        return float(r.get("trail_retrace_100_plus_pct", 15.0))
    if peak_profit_pct >= 50.0:
        return float(r.get("trail_retrace_50_100_pct", 10.0))
    if peak_profit_pct >= 20.0:
        return float(r.get("trail_retrace_20_50_pct", 7.0))
    return float(r.get("trail_retrace_10_20_pct", 5.0))


def position_profit_pct(pos, px):
    if pos["entry"] <= 0 or px <= 0:
        return 0.0
    return ((px / pos["entry"] - 1.0) * 100.0
            if pos["direction"] == "LONG"
            else (pos["entry"] / px - 1.0) * 100.0)


def manage_position(state, m):
    pos = state["positions"].get(m["symbol"])
    if not pos:
        return

    direction = pos["direction"]
    px = m["bid"] if direction == "LONG" else m["ask"]
    if px <= 0:
        return

    profit_pct = position_profit_pct(pos, px)
    pos["current_price"] = px
    pos["current_pnl"] = current_pnl(pos, m)
    pos["current_profit_pct"] = profit_pct
    pos["candles"] = m.get("candles", [])[-30:]
    peak_profit = float(pos.get("peak_profit_pct", 0.0))

    # Track the best price/profit reached while the position is open.
    if profit_pct > peak_profit:
        pos["peak_profit_pct"] = profit_pct
        pos["peak_price"] = px
        peak_profit = profit_pct

    activation = float(CFG["risk"].get("trail_activation_pct", 10.0))
    if peak_profit >= activation:
        pos["trailing_active"] = True
        retrace = trailing_retrace_pct(peak_profit)
        pos["trail_retrace_pct"] = retrace
        peak_price = float(pos.get("peak_price", px))
        if direction == "LONG":
            trail_stop = peak_price * (1.0 - retrace / 100.0)
            pos["stop"] = max(pos.get("stop", 0.0), trail_stop)
        else:
            trail_stop = peak_price * (1.0 + retrace / 100.0)
            current_stop = pos.get("stop", float("inf"))
            pos["stop"] = min(current_stop, trail_stop)

    # Only the hard -15% stop may close a losing position.
    # The previous thesis-invalidation exit was intentionally removed so that
    # temporary momentum reversals get room to recover.
    if direction == "LONG":
        if px <= pos["stop"]:
            reason = "HARD_SL_-15%" if profit_pct < 0 else "TRAILING_PEAK_RETRACE"
            close_position(state, m, reason)
            return
    else:
        if px >= pos["stop"]:
            reason = "HARD_SL_-15%" if profit_pct < 0 else "TRAILING_PEAK_RETRACE"
            close_position(state, m, reason)
            return

    # No fixed TP and no time stop. A winner is allowed to run until trailing
    # gives back the configured portion of the move.


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

    max_losses = int(CFG["risk"].get("max_consecutive_losses", 3))
    if state.get("cooldown_until"):
        try:
            cooldown_dt = datetime.fromisoformat(state["cooldown_until"])
            if now < cooldown_dt:
                return True, "LOSS_COOLDOWN"
            state["cooldown_until"] = None
            state["consecutive_losses"] = 0
        except ValueError:
            state["cooldown_until"] = None

    if state.get("consecutive_losses", 0) >= max_losses:
        return True, "MAX_CONSECUTIVE_LOSSES"

    return False, ""

def update_dashboard_history(state):
    """Persist equity and daily history for 24H/7D/ALL-TIME dashboard ranges."""
    now = utcnow()
    ts = now.timestamp()
    sample_every = float(CFG.get("dashboard", {}).get("equity_sample_seconds", 60))
    if ts - float(state.get("last_equity_sample_ts", 0.0)) >= sample_every:
        state.setdefault("equity_history", []).append({"ts": iso(now), "equity": state["equity"]})
        state["last_equity_sample_ts"] = ts
    day = now.date().isoformat()
    day_start = state.get("day_start_equity", state["equity"])
    state.setdefault("daily_history", {})[day] = {
        "start_equity": day_start,
        "equity": state["equity"],
        "pnl": state["equity"] - day_start,
        "trades": sum(1 for e in state.get("trade_history", []) if str(e.get("timestamp", "")).startswith(day)),
    }


def scanner_rejection(state, reason):
    stats = state.setdefault("scanner_stats", {"markets_seen": 0, "candidate_markets": 0, "selected": 0, "portfolio_rejected": 0, "rejections": {}})
    rej = stats.setdefault("rejections", {})
    rej[reason] = int(rej.get(reason, 0)) + 1


def write_report(state, scan_count, markets_count=0):
    closed = state["wins"] + state["losses"]
    win_rate = state["wins"] / closed * 100 if closed else 0.0
    pf = state["gross_profit"] / state["gross_loss"] if state["gross_loss"] > 0 else None
    daily_pnl = state["equity"] - state.get("day_start_equity", state["equity"])
    dd = ((state["equity"] / state["peak_equity"] - 1.0) * 100.0) if state.get("peak_equity") else 0.0
    avg_winner = state["gross_profit"] / state["wins"] if state["wins"] else 0.0
    avg_loser = -(state["gross_loss"] / state["losses"]) if state["losses"] else 0.0

    now = utcnow()
    cutoff_24 = now.timestamp() - 86400
    recent = []
    for e in state.get("trade_history", []):
        try:
            ts = datetime.fromisoformat(str(e.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
            if ts >= cutoff_24:
                recent.append(e)
        except (TypeError, ValueError):
            continue
    wins24 = sum(1 for e in recent if num(e.get("net_pnl")) >= 0)
    pnl24 = sum(num(e.get("net_pnl")) for e in recent)

    stats = state.get("scanner_stats", {})
    rejections = stats.get("rejections", {})
    top_rejections = sorted(rejections.items(), key=lambda x: x[1], reverse=True)[:CFG.get("dashboard", {}).get("rejection_top_n", 12)]
    strat = state.get("strategy_stats", {})
    top_markets = sorted([(k, v) for k, v in strat.get("by_market", {}).items()], key=lambda kv: kv[1].get("pnl", 0.0), reverse=True)[:10]
    top_entry = sorted([(k, v) for k, v in strat.get("by_entry_reason", {}).items()], key=lambda kv: kv[1].get("pnl", 0.0), reverse=True)[:10]
    exit_stats = sorted(strat.get("by_exit_reason", {}).items(), key=lambda kv: kv[1].get("trades", 0), reverse=True)
    score_order = ["<75", "75-79", "80-84", "85-89", "90-93", "94-100"]
    score_buckets = []
    for b in score_order:
        v = strat.get("by_score_bucket", {}).get(b, {"trades": 0, "wins": 0, "pnl": 0.0})
        score_buckets.append([b, {**v, "win_rate_pct": (v.get("wins",0)/v.get("trades",1)*100.0) if v.get("trades",0) else 0.0}])

    cooldown_remaining = 0.0
    if state.get("cooldown_until"):
        try:
            cooldown_remaining = max(0.0, (datetime.fromisoformat(state["cooldown_until"]) - now).total_seconds())
        except ValueError:
            cooldown_remaining = 0.0

    report = {
        "generated_at": iso(now),
        "equity": state["equity"],
        "cash": state["cash"],
        "realized_pnl": state["realized_pnl"],
        "unrealized_pnl": state["unrealized_pnl"],
        "closed_trades": closed,
        "win_rate_pct": win_rate,
        "profit_factor": pf,
        "avg_winner": avg_winner,
        "avg_loser": avg_loser,
        "peak_equity": state["peak_equity"],
        "current_drawdown_pct": dd,
        "max_drawdown_pct": min(state.get("max_drawdown_pct", dd), 0.0),
        "daily_pnl": daily_pnl,
        "pnl_24h": pnl24,
        "trades_24h": len(recent),
        "win_rate_24h_pct": (wins24 / len(recent) * 100.0) if recent else 0.0,
        "open_positions": len(state["positions"]),
        "max_open_positions": CFG["risk"]["max_open_positions"],
        "max_position_pct": CFG["risk"]["max_position_pct"],
        "exceptional_position_pct": CFG["risk"].get("exceptional_position_pct", CFG["risk"]["max_position_pct"]),
        "exceptional_risk_per_trade_pct": CFG["risk"].get("exceptional_risk_per_trade_pct"),
        "max_exceptional_positions": CFG["risk"].get("max_exceptional_positions", 1),
        "max_total_exposure_pct": CFG["risk"]["max_total_position_pct"],
        "max_correlated_positions": CFG["risk"].get("max_correlated_positions", 2),
        "stop_loss_pct": CFG["risk"].get("stop_loss_pct", 15.0),
        "trail_activation_pct": CFG["risk"].get("trail_activation_pct", 10.0),
        "trail_retrace_rules": {
            "10_20": CFG["risk"].get("trail_retrace_10_20_pct", 5.0),
            "20_50": CFG["risk"].get("trail_retrace_20_50_pct", 7.0),
            "50_100": CFG["risk"].get("trail_retrace_50_100_pct", 10.0),
            "100_plus": CFG["risk"].get("trail_retrace_100_plus_pct", 15.0),
        },
        "btc_symbol": state.get("btc_symbol"),
        "btc_regime": state.get("btc_regime", "UNKNOWN"),
        "slippage_pct_per_side": CFG["filters"].get("slippage_pct_per_side", 0.0),
        "live_order_execution": False,
        "scan_count": scan_count,
        "markets_count": markets_count,
        "last_loop_ts": state.get("last_loop_ts"),
        "last_scan_ts": state.get("last_scan_ts"),
        "last_error": state.get("last_error"),
        "api_errors": state.get("api_errors", 0),
        "cooldown_remaining_sec": cooldown_remaining,
        "consecutive_losses": state.get("consecutive_losses", 0),
        "max_consecutive_losses": CFG["risk"].get("max_consecutive_losses", 3),
        "scanner": {
            "markets_seen": stats.get("markets_seen", 0),
            "candidate_markets": stats.get("candidate_markets", 0),
            "selected": stats.get("selected", 0),
            "portfolio_rejected": stats.get("portfolio_rejected", 0),
            "top_rejections": top_rejections,
        },
        "strategy": {
            "top_markets": top_markets,
            "top_entry_reasons": top_entry,
            "exit_reasons": exit_stats,
            "score_buckets": score_buckets,
        },
        "equity_history": state.get("equity_history", []),
        "daily_history": state.get("daily_history", {}),
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

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
    global SYMBOL_CURSOR, LAST_SYMBOL_REFRESH

    state = load_state()
    scan_count = 0
    last_heartbeat = 0.0
    last_halt_reason = None
    symbols = []
    btc_symbol = None
    last_symbol_discovery = 0.0
    cycle_seconds = float(CFG.get("poll_seconds", 5))
    batch_size = int(CFG.get("batch_size", 15))
    symbols_refresh_seconds = float(CFG.get("symbols_refresh_seconds", 60))

    print("RWAPerp Agent FINAL — PAPER ONLY", flush=True)
    print("LIVE ORDER EXECUTION: DISABLED", flush=True)
    print(f"Max {CFG['risk']['max_open_positions']} positions | {CFG['risk']['max_position_pct']:.0f}% standard / {CFG['risk'].get('exceptional_position_pct', 12):.0f}% exceptional | gross {CFG['risk']['max_total_position_pct']:.0f}% | hard SL -15% | trailing from +10% | max 2 correlated", flush=True)
    print(f"Market cycle target: {cycle_seconds:.1f}s | batch: {batch_size} | workers: 3", flush=True)
    print(f"Starting/current equity: ${state['equity']:.2f}", flush=True)

    telegram_send(
        f"🤖 RWAPerp Agent ONLINE\nPAPER ONLY\n"
        f"Equity: ${state['equity']:.2f}\n"
        f"Cycle: {cycle_seconds:.0f}s | batch: {batch_size} | SL -15% | trailing +10% | BTC regime filter"
    )

    while True:
        cycle_started = time.time()
        try:
            halted, reason = risk_halt(state)
            state["halt_reason"] = reason or None
            if reason and reason != last_halt_reason:
                telegram_send(f"🛑 RISK HALT\nReason: {reason}")
                last_halt_reason = reason
            elif not reason:
                last_halt_reason = None

            now = time.time()
            if not symbols or now - last_symbol_discovery >= symbols_refresh_seconds:
                discovered = discover_symbols()
                if discovered:
                    symbols = discovered
                    SYMBOL_CURSOR %= len(symbols)
                    btc_symbol = find_btc_symbol(symbols)
                    state["btc_symbol"] = btc_symbol
                    last_symbol_discovery = now
                    print(f"[UNIVERSE] {len(symbols)} markets discovered | BTC reference: {btc_symbol or 'NOT FOUND'}", flush=True)

            if not symbols:
                raise RuntimeError("No PERP markets discovered")

            funding = get_funding_rates()

            # Open positions are always refreshed first. Remaining markets rotate
            # through the universe so the full set is refreshed progressively.
            open_symbols = [s for s in state["positions"] if s in symbols]
            remaining = [s for s in symbols if s not in open_symbols]
            if remaining:
                n = len(remaining)
                batch = [remaining[(SYMBOL_CURSOR + i) % n] for i in range(min(batch_size, n))]
                SYMBOL_CURSOR = (SYMBOL_CURSOR + len(batch)) % n
            else:
                batch = []
            refresh_symbols = list(dict.fromkeys(open_symbols + batch + ([btc_symbol] if btc_symbol else [])))

            t0 = time.time()
            refreshed = refresh_market_batch(refresh_symbols, funding)
            elapsed = time.time() - t0

            # Build the decision universe from fresh cache plus still-valid cache.
            markets = []
            market_map = {}
            for symbol in symbols:
                m = MARKET_CACHE.get(symbol)
                if m and time.time() - MARKET_CACHE_TS.get(symbol, 0) <= max(90.0, symbols_refresh_seconds * 2.0):
                    markets.append(m)
                    market_map[symbol] = m

            scan_count += 1
            state["btc_regime"] = apply_btc_context(market_map, btc_symbol)
            state["last_loop_ts"] = iso(utcnow())
            state["last_scan_ts"] = iso(utcnow())
            state["last_error"] = None
            state.setdefault("scanner_stats", {}).setdefault("markets_seen", 0)
            state["scanner_stats"]["markets_seen"] += len(refreshed)

            # Existing positions are managed every cycle when their market was refreshed.
            for symbol in open_symbols:
                m = market_map.get(symbol)
                if m:
                    apply_funding(state, m)
                    manage_position(state, m)

            mark_equity(state, market_map)
            apply_btc_context(market_map, btc_symbol)

            if not halted and markets:
                median_momentum = median([m["momentum5"] for m in markets]) if markets else 0.0

                for _ in range(max(0, CFG["risk"]["max_open_positions"] - len(state["positions"]))):
                    candidates = []
                    for m in markets:
                        if m["symbol"] in state["positions"]:
                            continue
                        result = choose_direction(m, median_momentum)
                        if result:
                            score_val, direction, reasons = result
                            candidates.append({"score": score_val, "direction": direction, "market": m, "reasons": reasons})
                        else:
                            for reject_reason in m.get("_reject_reasons", []):
                                scanner_rejection(state, reject_reason)

                    candidates.sort(key=lambda x: x["score"], reverse=True)
                    state["scanner_stats"]["candidate_markets"] += len(candidates)
                    if not candidates:
                        break

                    slot = len(state["positions"])
                    threshold = score_threshold(slot)
                    chosen = None
                    for candidate in candidates:
                        if candidate["score"] < threshold:
                            break
                        ok, reject_reason = portfolio_allows(state, candidate, market_map)
                        if ok:
                            chosen = candidate
                            break
                        state["scanner_stats"]["portfolio_rejected"] += 1
                        scanner_rejection(state, reject_reason)
                        log_event({"timestamp": iso(utcnow()), "event": "CANDIDATE_REJECTED", "symbol": candidate["market"]["symbol"], "direction": candidate["direction"], "score": candidate["score"], "reason": reject_reason})

                    if not chosen:
                        break

                    log_event({"timestamp": iso(utcnow()), "event": "SELECT", "symbol": chosen["market"]["symbol"], "direction": chosen["direction"], "score": chosen["score"], "slot": slot + 1, "reasons": chosen["reasons"]})
                    state["scanner_stats"]["selected"] += 1
                    open_position(state, chosen)

            mark_equity(state, market_map)
            update_dashboard_history(state)
            current_dd = ((state["equity"] / state["peak_equity"] - 1.0) * 100.0) if state.get("peak_equity") else 0.0
            state["max_drawdown_pct"] = min(float(state.get("max_drawdown_pct", 0.0)), current_dd)
            write_report(state, scan_count, len(markets))
            save_state(state)

            last_heartbeat = maybe_heartbeat(state, len(markets), last_heartbeat)
            print(
                f"[HEARTBEAT] equity=${state['equity']:.2f} "
                f"realized={state['realized_pnl']:+.2f} "
                f"unrealized={state['unrealized_pnl']:+.2f} "
                f"positions={len(state['positions'])} "
                f"wins={state['wins']} losses={state['losses']} "
                f"markets={len(markets)}/{len(symbols)} refreshed={len(refreshed)} "
                f"batch={len(refresh_symbols)} api={elapsed:.2f}s "
                f"halt={state['halt_reason'] or '-'}",
                flush=True
            )

            sleep_for = max(0.2, cycle_seconds - (time.time() - cycle_started))
            time.sleep(sleep_for)

        except KeyboardInterrupt:
            save_state(state)
            print("Stopped.", flush=True)
            break
        except Exception as exc:
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            state["api_errors"] = int(state.get("api_errors", 0)) + 1
            log_event({"timestamp": iso(utcnow()), "event": "LOOP_ERROR", "error": str(exc)})
            save_state(state)
            print(f"[LOOP_ERROR] {type(exc).__name__}: {exc}", flush=True)
            telegram_send(f"⚠️ AGENT ERROR\n{type(exc).__name__}: {exc}")
            time.sleep(cycle_seconds)


if __name__ == "__main__":
    main()
