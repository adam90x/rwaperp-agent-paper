import json
import threading
import runpy
import time
from pathlib import Path
from flask import Flask, jsonify, Response, request
import db_persistence

BASE = Path(__file__).resolve().parent
app = Flask(__name__)
_started = False
_agent_thread = None
_agent_error = None
_lock = threading.Lock()
_primary_state_cache = None
_primary_state_cache_ts = 0.0
_PRIMARY_STATE_TTL = 10.0


def read_json(name, default):
    p = BASE / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def _default_state():
    return {
        "equity": 500.0, "cash": 500.0, "realized_pnl": 0.0,
        "unrealized_pnl": 0.0, "wins": 0, "losses": 0,
        "positions": {}, "halt_reason": None, "equity_history": [],
        "daily_history": {}, "trade_history": [], "scanner_stats": {},
        "strategy_stats": {}, "peak_equity": 500.0
    }


def read_primary_state():
    """Read durable state from Neon when configured. Local JSON is only a fallback
    for Neon-disabled development runs. The dashboard caches the external read for
    a few seconds so a mobile dashboard does not hammer the database.
    """
    global _primary_state_cache, _primary_state_cache_ts
    now = time.time()
    if _primary_state_cache is not None and now - _primary_state_cache_ts < _PRIMARY_STATE_TTL:
        return _primary_state_cache

    if db_persistence.enabled():
        try:
            state = db_persistence.load_state()
            if isinstance(state, dict):
                _primary_state_cache = state
                _primary_state_cache_ts = now
                return state
        except Exception:
            pass
        return _primary_state_cache if isinstance(_primary_state_cache, dict) else _default_state()

    local_path = BASE / "paper_state.json"
    if local_path.exists():
        state = read_json("paper_state.json", None)
        if isinstance(state, dict):
            _primary_state_cache = state
            _primary_state_cache_ts = now
            return state
    return _default_state()

def read_all_exits(limit=5000):
    state = read_primary_state()
    merged = {}
    for e in state.get("trade_history", []) or []:
        try:
            net = round(float(e.get("net_pnl", 0.0) or 0.0), 8)
        except (TypeError, ValueError):
            net = 0.0
        key = (e.get("timestamp"), e.get("symbol"), e.get("reason"), net)
        merged[key] = e
    p = BASE / "paper_events.jsonl"
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                    if e.get("event") == "PAPER_EXIT":
                        key = (e.get("timestamp"), e.get("symbol"), e.get("reason"), round(float(e.get("net_pnl", 0.0) or 0.0), 8))
                        merged.setdefault(key, e)
                except Exception:
                    continue
        except Exception:
            pass
    events = sorted(merged.values(), key=lambda e: e.get("timestamp", ""), reverse=True)
    return events[:limit]


def status_data():
    state = read_primary_state()
    report = read_json("paper_live_report.json", {})
    closed = int(state.get("wins", 0)) + int(state.get("losses", 0))
    winrate = (state.get("wins", 0) / closed * 100) if closed else 0.0
    cooldown_remaining = float(report.get("cooldown_remaining_sec", 0.0) or 0.0)
    last_loop_ts = state.get("last_loop_ts") or report.get("last_loop_ts")
    heartbeat_age = None
    if last_loop_ts:
        try:
            from datetime import datetime, timezone
            heartbeat_age = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(str(last_loop_ts).replace("Z", "+00:00"))).total_seconds())
        except Exception:
            heartbeat_age = None
    # Liveness must be based on the external heartbeat, not this Gunicorn
    # worker's local thread object. Render can serve a request from another
    # instance during/after deploys.
    if heartbeat_age is not None and heartbeat_age <= 45.0:
        agent_status = "RUNNING"
    elif _agent_thread is not None and _agent_thread.is_alive() and not last_loop_ts:
        agent_status = "STARTING"
    else:
        agent_status = "STOPPED"
    persist_status = db_persistence.status()
    persisted_save_ts = state.get("last_db_save_ts")
    if persisted_save_ts:
        persist_status["last_save_ts"] = persisted_save_ts
    return {
        "equity": state.get("equity", 500.0),
        "cash": state.get("cash", 500.0),
        "realized_pnl": state.get("realized_pnl", 0.0),
        "unrealized_pnl": state.get("unrealized_pnl", 0.0),
        "closed_trades": closed,
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "win_rate_pct": report.get("win_rate_pct", winrate),
        "profit_factor": report.get("profit_factor"),
        "avg_winner": report.get("avg_winner", 0.0),
        "avg_loser": report.get("avg_loser", 0.0),
        "current_drawdown_pct": report.get("current_drawdown_pct", 0.0),
        "max_drawdown_pct": report.get("max_drawdown_pct", 0.0),
        "daily_pnl": report.get("daily_pnl", 0.0),
        "pnl_24h": report.get("pnl_24h", 0.0),
        "trades_24h": report.get("trades_24h", 0),
        "win_rate_24h_pct": report.get("win_rate_24h_pct", 0.0),
        "peak_equity": report.get("peak_equity", state.get("peak_equity", 500.0)),
        "positions": state.get("positions", {}),
        "open_positions": len(state.get("positions", {})),
        "halt_reason": state.get("halt_reason"),
        "cooldown_remaining_sec": cooldown_remaining,
        "markets_count": report.get("markets_count", 0),
        "scan_count": report.get("scan_count", 0),
        "live_order_execution": False,
        "agent": agent_status,
        "agent_error": _agent_error,
        "btc_symbol": report.get("btc_symbol", state.get("btc_symbol")),
        "btc_regime": report.get("btc_regime", state.get("btc_regime", "UNKNOWN")),
        "consecutive_losses": report.get("consecutive_losses", state.get("consecutive_losses", 0)),
        "max_consecutive_losses": report.get("max_consecutive_losses", 3),
        "last_loop_ts": last_loop_ts,
        "last_scan_ts": state.get("last_scan_ts") or report.get("last_scan_ts"),
        "last_error": state.get("last_error") or report.get("last_error"),
        "api_errors": state.get("api_errors", 0),
        "persistence": persist_status,
        "equity_history": report.get("equity_history", state.get("equity_history", [])),
        "daily_history": report.get("daily_history", state.get("daily_history", {})),
        "scanner": report.get("scanner", state.get("scanner_stats", {})),
        "strategy": report.get("strategy", state.get("strategy_stats", {})),
        "stop_loss_pct": report.get("stop_loss_pct", 15.0),
        "trail_activation_pct": report.get("trail_activation_pct", 10.0),
        "max_open_positions": report.get("max_open_positions", 5),
        "max_position_pct": report.get("max_position_pct", 7.0),
        "exceptional_position_pct": report.get("exceptional_position_pct", 12.0),
        "max_total_exposure_pct": report.get("max_total_exposure_pct", 40.0),
        "exceptional_risk_per_trade_pct": report.get("exceptional_risk_per_trade_pct", 1.8),
        "max_exceptional_positions": report.get("max_exceptional_positions", 1),
        "slippage_pct_per_side": report.get("slippage_pct_per_side", 0.03),
    }


def run_agent():
    global _agent_error
    try:
        runpy.run_path(str(BASE / "agent.py"), run_name="__main__")
    except Exception as exc:
        _agent_error = f"{type(exc).__name__}: {exc}"
        print(f"[AGENT THREAD ERROR] {_agent_error}", flush=True)


def start_agent_once():
    global _started, _agent_thread
    with _lock:
        if _started:
            return
        _started = True
        _agent_thread = threading.Thread(target=run_agent, name="paper-agent", daemon=True)
        _agent_thread.start()
        print("[WEBAPP] PAPER agent thread started", flush=True)


HTML = r'''<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RWAPerp Trading Lab V2</title>
<style>
:root{--bg:#05080d;--panel:rgba(9,16,26,.96);--panel2:#0b121b;--line:#26384b;--text:#f4f7fb;--muted:#8fa0b3;--gold:#f5c451;--green:#35e58a;--red:#ff5e6c;--purple:#8b5cf6;--shadow:0 12px 40px #0007}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;color:var(--text);font:14px Segoe UI,Arial,sans-serif;background:radial-gradient(circle at 10% 10%,#171225,transparent 30%),radial-gradient(circle at 90% 90%,#122019,transparent 30%),var(--bg);min-width:320px}
button{font:inherit}.wrap{width:min(1500px,100%);margin:auto;padding:22px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:16px}
h1{margin:0;font-size:30px;line-height:1.1}h1 span{color:var(--gold)}
.sub{color:var(--muted);margin-top:7px;line-height:1.45}.badge{border:1px solid #27533c;border-radius:999px;padding:9px 14px;color:var(--green);background:#0c1c14;font-weight:800;white-space:nowrap}
.nav,.card{background:var(--panel);border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow);backdrop-filter:blur(8px)}
.nav{display:flex;gap:14px;align-items:center;padding:12px 16px;margin-bottom:13px;color:#d8e0ea;overflow:auto;white-space:nowrap;scrollbar-width:none}.nav::-webkit-scrollbar{display:none}
.nav a{color:inherit;text-decoration:none}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px}.card{padding:16px;min-width:0}.span2{grid-column:span 2}.span4{grid-column:span 4}
.k{font-size:10px;letter-spacing:1px;color:var(--muted);text-transform:uppercase}.v{font-size:28px;font-weight:900;margin-top:6px;line-height:1.05}.green{color:var(--green)}.red{color:var(--red)}.gold{color:var(--gold)}
.section{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}.section h2{font-size:16px;margin:0}.tiny{font-size:11px;color:var(--muted)}
.statusrow{display:grid;grid-template-columns:22px minmax(0,1fr) auto;gap:9px;align-items:center;padding:6px 0}.statusrow span:last-child{overflow-wrap:anywhere;text-align:right}.check{color:var(--green);background:#103a27;border-radius:50%;width:18px;height:18px;text-align:center;line-height:18px}
.chart{height:250px;overflow:hidden}.chart svg{width:100%;height:240px;display:block}
.tablescroll{overflow:auto;-webkit-overflow-scrolling:touch;border-radius:10px}.tablescroll table{min-width:760px}
table{width:100%;border-collapse:collapse}th{font-size:10px;color:var(--muted);text-transform:uppercase;text-align:left;padding:9px 7px;border-bottom:1px solid var(--line);white-space:nowrap}td{padding:8px 7px;border-bottom:1px solid #1b2836;vertical-align:middle;white-space:nowrap}
.arrow{color:var(--gold);font-weight:900}.empty{color:var(--muted);padding:20px}.pill{padding:4px 8px;border-radius:999px;font-size:11px;font-weight:800}.long{color:var(--green);background:#103b28}.short{color:var(--red);background:#3a1820}
.footer{text-align:center;color:var(--muted);font-size:11px;padding:15px}.tabs{display:flex;gap:6px;flex-wrap:wrap}.tab{border:1px solid var(--line);background:var(--panel2);color:var(--muted);border-radius:8px;padding:6px 9px;cursor:pointer}.tab.active{color:var(--text);border-color:var(--purple)}
.metricgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.metric{background:var(--panel2);border:1px solid #1c2a39;border-radius:10px;padding:10px}.metric b{display:block;font-size:18px;margin-top:4px}.mini{font-size:12px;color:var(--muted);line-height:1.7}.spark{width:100px;height:38px}.sectiongrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.span2mini{grid-column:span 2}.danger{color:var(--red)}
.mobile-position-list,.mobile-history-list{display:none}
.position-card,.trade-card{background:var(--panel2);border:1px solid #1c2a39;border-radius:12px;padding:12px}.position-card+.position-card,.trade-card+.trade-card{margin-top:9px}
.position-top,.trade-top{display:flex;justify-content:space-between;align-items:center;gap:10px}.position-name{font-weight:800;overflow-wrap:anywhere}.position-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}.position-metric{border:1px solid #1c2a39;border-radius:8px;padding:7px;background:#08101a}.position-metric span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px}.position-metric b{display:block;margin-top:2px;overflow-wrap:anywhere}.mobile-line{display:flex;justify-content:space-between;gap:12px;color:var(--muted);font-size:12px;margin-top:8px}.mobile-line strong{color:var(--text)}
@media(max-width:1100px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.span4{grid-column:span 2}.metricgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:720px){
 body{font-size:13px}.wrap{padding:10px}.grid{grid-template-columns:1fr;gap:10px}.span2,.span4{grid-column:span 1}.card{padding:12px;border-radius:13px}
 header{flex-direction:column;align-items:stretch;gap:10px;margin-bottom:10px}h1{font-size:22px}.sub{font-size:11px;margin-top:5px}.badge{align-self:flex-start;padding:7px 10px;font-size:11px}
 .nav{gap:7px;padding:9px 10px;margin-bottom:10px;font-size:11px}.section{align-items:flex-start;margin-bottom:9px}.section h2{font-size:14px}.section .tiny{max-width:48%;text-align:right}
 .v{font-size:24px}.chart{height:220px}.chart svg{height:215px}.tabs{gap:5px}.tab{padding:7px 9px;font-size:11px}
 .metricgrid{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.metric{padding:9px}.metric b{font-size:16px}
 .desktop-table{display:none}.mobile-position-list,.mobile-history-list{display:block}
 .tablescroll{overflow:visible}.tablescroll table{min-width:0}
 .position-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.spark{width:110px;height:42px}\n.position-charts{display:flex;gap:8px;align-items:flex-start;min-width:300px}.position-charts>div{min-width:0}.chart-label{font-size:9px;color:var(--muted);font-weight:800;margin-bottom:2px;letter-spacing:.06em}.candle-chart{display:block;width:220px;height:92px;background:#07101a;border:1px solid #1f3043;border-radius:6px}.chart-empty{color:var(--muted)}.mobile-chart-line{align-items:flex-start}.mobile-chart-line .position-charts{flex-direction:column;width:100%}.mobile-chart-line .position-charts .candle-chart{width:min(100%,360px);height:auto;aspect-ratio:220/92}\n.footer{padding:12px 5px}
}
@media(min-width:721px){.desktop-table{display:block}.mobile-position-list,.mobile-history-list{display:none}}
@media(max-width:380px){.wrap{padding:8px}.position-grid{grid-template-columns:1fr}.section{flex-direction:column}.section .tiny{max-width:none;text-align:left}.tabs{width:100%}.tab{flex:1}.metricgrid{grid-template-columns:1fr 1fr}.position-top,.trade-top{align-items:flex-start}}
</style></head><body><div class="wrap">
<header><div><h1>🤖 RWAPerp <span>Trading Lab V2</span></h1><div class="sub">PAPER ONLY • SL -15% • trailing +10% • max 5 pozycji • standard 7% / exceptional 12%</div></div><div id="badge" class="badge">● BOT RUNNING</div></header>
<div class="nav">▣ Dashboard &nbsp;&nbsp;→ Live Trades &nbsp;&nbsp;☷ ALL-TIME Historia &nbsp;&nbsp;♘ Scanner &nbsp;&nbsp;⌁ Bot Health</div>
<div class="grid">
<div class="card"><div class="k">Equity</div><div id="equity" class="v">$500.00</div><div id="real" class="tiny">+$0.00 realized</div></div>
<div class="card"><div class="k">Open P/L</div><div id="unreal" class="v green">+$0.00</div><div id="open" class="tiny">0 positions</div></div>
<div class="card"><div class="k">Today's P/L</div><div id="daily" class="v green">+$0.00</div><div id="p24" class="tiny">24H: +$0.00 • 0 trades</div></div>
<div class="card"><div class="k">Win rate / PF</div><div id="win" class="v">0.0%</div><div id="pf" class="tiny">PF —</div></div>
<div class="card span2"><div class="section"><h2>📈 Equity</h2><div class="tabs"><button class="tab active" data-range="24h">24H</button><button class="tab" data-range="7d">7D</button><button class="tab" data-range="all">ALL-TIME</button></div></div><div id="chart" class="chart">Agent is collecting data…</div></div>
<div class="card span2"><div class="section"><h2>🤖 Bot Health</h2><span class="tiny">PAPER ONLY</span></div><div class="statusrow"><b class="check">✓</b><span>Agent</span><span id="agentstate">RUNNING</span></div><div class="statusrow"><b class="check">✓</b><span>Market cycle</span><span>5s target</span></div><div class="statusrow"><b class="check">✓</b><span>Markets refreshed</span><span id="markets">0</span></div><div class="statusrow"><b class="check">✓</b><span>BTC regime</span><span id="btc">UNKNOWN</span></div><div class="statusrow"><b class="check">✓</b><span>Loss streak</span><span id="lossstreak">0/3</span></div><div class="statusrow"><b class="check">✓</b><span>Risk / cooldown</span><span id="halt">RUNNING</span></div><div class="statusrow"><b class="check">✓</b><span>Last loop</span><span id="lastloop">—</span></div><div class="statusrow"><b class="check">✓</b><span>Errors</span><span id="errors">0</span></div><div class="statusrow"><b class="check">✓</b><span>Persistence</span><span id="persistence">NEON —</span></div><div class="statusrow"><b class="check">✓</b><span>Last DB save</span><span id="dbsave">—</span></div></div>
<div class="card span4"><div class="section"><h2>🟢 Open Positions</h2><span class="tiny">SL -15% • trailing from +10%</span></div><div class="desktop-table tablescroll"><table><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Current</th><th>P/L</th><th>SL / Trail</th><th>Peak</th><th>Chart</th><th>Lev.</th><th>Notional</th><th>Score</th></tr></thead><tbody id="positions"></tbody></table></div><div id="positionsMobile" class="mobile-position-list"></div></div>
<div class="card span4"><div class="section"><h2>📊 Performance</h2><span class="tiny">ALL-TIME</span></div><div class="metricgrid"><div class="metric"><span class="tiny">Avg winner</span><b id="avgw">$0.00</b></div><div class="metric"><span class="tiny">Avg loser</span><b id="avgl">$0.00</b></div><div class="metric"><span class="tiny">Current DD</span><b id="dd">0.0%</b></div><div class="metric"><span class="tiny">Max DD</span><b id="mdd">0.0%</b></div></div></div>
<div class="card span2"><div class="section"><h2>🏆 ALL-TIME Trades</h2><span class="tiny">cała historia zapisanej sesji</span></div><div class="desktop-table tablescroll" style="max-height:550px"><table><thead><tr><th>Time</th><th></th><th>Market</th><th>Side</th><th>P/L</th><th>Reason</th><th>Peak</th><th>Hold</th></tr></thead><tbody id="history"></tbody></table></div><div id="historyMobile" class="mobile-history-list"></div></div>
<div class="card span2"><div class="section"><h2>📡 Scanner Funnel</h2><span class="tiny">cały okres</span></div><div id="funnel" class="mini"></div><div id="rejects" class="mini"></div></div>
<div class="card span2"><div class="section"><h2>🧠 Strategy Stats</h2><span class="tiny">market / entry / exit / score</span></div><div class="sectiongrid"><div id="marketsStats" class="mini"></div><div id="reasonStats" class="mini"></div><div id="scoreStats" class="mini span2mini"></div></div></div>
<div class="card span2"><div class="section"><h2>📅 Daily P/L</h2><span class="tiny">ostatnie 30 dni</span></div><div id="dailytable"></div></div>
</div><div id="footer" class="footer">🟢 Live • connecting… • PAPER ONLY</div></div>
<script>
const $=id=>document.getElementById(id), money=x=>'$'+Number(x||0).toFixed(2);let selectedRange='24h',latestStatus=null;
function chart(vals){
  if(!vals.length)return 'Agent is collecting data…';
  const clean=vals.filter(x=>x&&Number.isFinite(Number(x.equity))&&x.ts);
  if(!clean.length)return 'Agent is collecting data…';
  const w=900,h=260,left=72,right=24,top=18,bottom=42;
  const min0=Math.min(...clean.map(x=>Number(x.equity)));
  const max0=Math.max(...clean.map(x=>Number(x.equity)));
  const span=max0-min0;
  const pad=span===0?0.5:Math.max(span*0.12,0.05);
  const min=Math.floor((min0-pad)*100)/100;
  const max=Math.ceil((max0+pad)*100)/100;
  const r=max-min||1;
  const plotW=w-left-right,plotH=h-top-bottom;
  const x=i=>left+i*plotW/Math.max(1,clean.length-1);
  const y=v=>top+(max-Number(v))*plotH/r;
  const pts=clean.map((v,i)=>[x(i),y(v.equity)]);
  const path=pts.map((q,i)=>(i?'L':'M')+q[0].toFixed(1)+' '+q[1].toFixed(1)).join(' ');
  const ticks=4;
  let grid='',labelsY='';
  for(let i=0;i<=ticks;i++){
    const val=min+(max-min)*(1-i/ticks);
    const yy=top+i*plotH/ticks;
    grid+=`<line x1="${left}" x2="${w-right}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" stroke="#26384b" stroke-width="1" opacity="0.55"/>`;
    labelsY+=`<text x="${left-10}" y="${(yy+4).toFixed(1)}" text-anchor="end" fill="#8fa0b3" font-size="11">${money(val)}</text>`;
  }
  const xTickCount=Math.min(4,clean.length-1);
  let labelsX='';
  for(let i=0;i<=xTickCount;i++){
    const idx=Math.round(i*(clean.length-1)/Math.max(1,xTickCount));
    const xx=x(idx);
    const dt=new Date(clean[idx].ts);
    const label=selectedRange==='all'?dt.toLocaleDateString('pl-PL',{day:'2-digit',month:'2-digit',year:'2-digit'}):selectedRange==='7d'?dt.toLocaleDateString('pl-PL',{day:'2-digit',month:'2-digit'}):dt.toLocaleTimeString('pl-PL',{hour:'2-digit',minute:'2-digit'});
    labelsX+=`<line x1="${xx.toFixed(1)}" x2="${xx.toFixed(1)}" y1="${h-bottom}" y2="${h-bottom+4}" stroke="#26384b"/><text x="${xx.toFixed(1)}" y="${h-14}" text-anchor="middle" fill="#8fa0b3" font-size="11">${label}</text>`;
  }
  const last=clean.at(-1);
  const lastPt=pts.at(-1);
  const lastLabel=money(last.equity);
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Equity chart"><line x1="${left}" x2="${left}" y1="${top}" y2="${h-bottom}" stroke="#26384b"/><line x1="${left}" x2="${w-right}" y1="${h-bottom}" y2="${h-bottom}" stroke="#26384b"/>${grid}${labelsY}${labelsX}<text x="8" y="${top+8}" fill="#8fa0b3" font-size="10">EQUITY ($)</text><text x="${w-right}" y="${h-2}" text-anchor="end" fill="#8fa0b3" font-size="10">TIME</text><path d="${path}" fill="none" stroke="#8b5cf6" stroke-width="3"/><circle cx="${lastPt[0].toFixed(1)}" cy="${lastPt[1].toFixed(1)}" r="4" fill="#f5c451"/><text x="${Math.min(w-right-4,lastPt[0]+8).toFixed(1)}" y="${Math.max(top+12,lastPt[1]-8).toFixed(1)}" fill="#f5c451" font-size="12" font-weight="700">${lastLabel}</text></svg>`;
}
function sample(vals,maxn=1000){if(vals.length<=maxn)return vals;const out=[];const step=(vals.length-1)/(maxn-1);for(let i=0;i<maxn;i++)out.push(vals[Math.round(i*step)]);return out}
function rangeData(history){const now=Date.now(),cut=selectedRange==='24h'?now-86400000:selectedRange==='7d'?now-7*86400000:0;const h=(history||[]).filter(x=>x&&x.ts&&Number.isFinite(Number(x.equity)));const filtered=h.filter(x=>!cut||new Date(x.ts).getTime()>=cut);if(filtered.length)return sample(filtered);if(latestStatus&&Number.isFinite(Number(latestStatus.equity))){return [{ts:new Date(now).toISOString(),equity:Number(latestStatus.equity)}];}return [];}
function candleTs(c){const t=c?.timestamp; if(typeof t==='number') return t<1e12?t*1000:t; const n=Number(t); if(Number.isFinite(n)) return n<1e12?n*1000:n; const d=Date.parse(t||''); return Number.isFinite(d)?d:0;}
function sortCandles(candles){return (candles||[]).filter(c=>Number(c.open)>0&&Number(c.high)>0&&Number(c.low)>0&&Number(c.close)>0).slice().sort((a,b)=>candleTs(a)-candleTs(b));}
function aggregateCandles(candles,minutes){const src=sortCandles(candles),bucketMs=minutes*60000,map=new Map();src.forEach(c=>{const ts=candleTs(c);if(!ts)return;const key=Math.floor(ts/bucketMs)*bucketMs;let a=map.get(key);if(!a){a={timestamp:new Date(key).toISOString(),open:Number(c.open),high:Number(c.high),low:Number(c.low),close:Number(c.close),volume:Number(c.volume||0)};map.set(key,a)}else{a.high=Math.max(a.high,Number(c.high));a.low=Math.min(a.low,Number(c.low));a.close=Number(c.close);a.volume+=Number(c.volume||0)}});return [...map.values()].sort((a,b)=>candleTs(a)-candleTs(b));}
function favorableCandleColor(c,prev,direction){const delta=prev==null?Number(c.close)-Number(c.open):Number(c.close)-Number(prev);const favorable=direction==='SHORT'?delta<=0:delta>=0;return favorable?'#35e58a':'#ff5e6c';}
function candleSvg(candles,direction,minutes,bars){let cs=minutes===30?aggregateCandles(candles,30):sortCandles(candles);cs=cs.slice(-(bars||24));if(!cs.length)return '<span class="chart-empty">—</span>';const w=220,h=92,left=8,right=8,top=6,bottom=14;let min=Math.min(...cs.map(c=>Number(c.low))),max=Math.max(...cs.map(c=>Number(c.high)));if(max<=min){max=min+1}const r=max-min;const y=v=>top+(max-v)*(h-top-bottom)/r;const body=Math.max(2,(h-top-bottom)/70);let svg=`<svg class="candle-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${minutes} minute candlestick chart">`;svg+=`<line x1="${left}" x2="${w-right}" y1="${h-bottom}" y2="${h-bottom}" stroke="#26384b"/>`;cs.forEach((c,i)=>{const x=left+(i+0.5)*(w-left-right)/cs.length;const yo=y(c.open),yc=y(c.close),yh=y(c.high),yl=y(c.low);const prev=i>0?Number(cs[i-1].close):null;const sw=favorableCandleColor(c,prev,direction);const topBody=Math.min(yo,yc),bh=Math.max(body,Math.abs(yc-yo));svg+=`<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${yh.toFixed(1)}" y2="${yl.toFixed(1)}" stroke="${sw}" stroke-width="1.4"/><rect x="${(x-2.5).toFixed(1)}" y="${topBody.toFixed(1)}" width="5" height="${bh.toFixed(1)}" rx="1" fill="${sw}"/>`;});return svg+'</svg>';}
function positionCharts(p){const base=24,dir=p.direction||'LONG';return `<div class="position-charts"><div><div class="chart-label">5 MIN</div>${candleSvg(p.candles,dir,5,base)}</div><div><div class="chart-label">30 MIN</div>${candleSvg(p.candles,dir,30,12)}</div></div>`;}
function renderDaily(d){const rows=Object.entries(d||{}).sort((a,b)=>b[0].localeCompare(a[0])).slice(0,30);$('dailytable').innerHTML=rows.length?'<table><thead><tr><th>Date</th><th>Start</th><th>End</th><th>P/L</th><th>Trades</th></tr></thead><tbody>'+rows.map(([day,x])=>`<tr><td>${day}</td><td>${money(x.start_equity)}</td><td>${money(x.equity)}</td><td class="${x.pnl>=0?'green':'red'}">${x.pnl>=0?'+':''}${money(x.pnl)}</td><td>${x.trades||0}</td></tr>`).join('')+'</tbody></table>':'<div class="empty">Brak danych.</div>'}
function renderScanner(s){const seen=Number(s.markets_seen||0),cand=Number(s.candidate_markets||0),sel=Number(s.selected||0),rej=Number(s.portfolio_rejected||0);$('funnel').innerHTML=`<div>Markets refreshed: <b>${seen}</b></div><div>Candidate signals: <b>${cand}</b></div><div>Selected: <b>${sel}</b></div><div>Portfolio rejected: <b>${rej}</b></div>`;const rr=s.top_rejections||[];$('rejects').innerHTML=rr.length?'<br><b>Top rejection reasons</b><br>'+rr.map(x=>`${x[0]}: ${x[1]}`).join('<br>'):'<br>No rejection data yet.'}
function renderStrategy(st){const mk=(st.top_markets||[]).slice(0,8),er=(st.top_entry_reasons||[]).slice(0,8),xr=(st.exit_reasons||[]).slice(0,8),sb=(st.score_buckets||[]);$('marketsStats').innerHTML='<b>Best markets</b><br>'+ (mk.length?mk.map(([k,v])=>`${k}: ${money(v.pnl)} (${v.trades}t)`).join('<br>'):'brak');$('reasonStats').innerHTML='<b>Exit reasons</b><br>'+ (xr.length?xr.map(([k,v])=>`${k}: ${v.trades}t / ${money(v.pnl)}`).join('<br>'):'brak') + '<br><br><b>Entry signals</b><br>'+(er.length?er.map(([k,v])=>`${k}: ${v.trades}t / ${money(v.pnl)}`).join('<br>'):'brak');$('scoreStats').innerHTML='<b>Score buckets</b><br>'+ (sb.length?sb.map(([k,v])=>`${k}: ${v.trades}t • ${Number(v.win_rate_pct||0).toFixed(1)}% WR • ${money(v.pnl)}`).join('<br>'):'brak')}
function renderPositionMobile(ps){const el=$('positionsMobile');if(!el)return;if(!ps.length){el.innerHTML='<div class="empty">Brak otwartych pozycji 🐶🦆</div>';return}el.innerHTML=ps.map(p=>{const cur=p.current_price||0,pl=p.current_pnl||0;const trail=p.trailing_active?`Trail -${Number(p.trail_retrace_pct||0).toFixed(1)}%`:'Waiting +10%';const extra=p.exceptional_setup?' ⭐ exceptional 12%':'';const corr=Number(p.correlated_open_count||0);return `<div class="position-card"><div class="position-top"><div class="position-name">${p.symbol||'—'}${extra}</div><span class="pill ${(p.direction||'').toLowerCase()}">${p.direction||'—'}</span></div><div class="position-grid"><div class="position-metric"><span>Entry</span><b>${Number(p.entry||0).toFixed(6)}</b></div><div class="position-metric"><span>Current</span><b>${Number(cur).toFixed(6)}</b></div><div class="position-metric"><span>P/L</span><b class="${pl>=0?'green':'red'}">${pl>=0?'+':''}${money(pl)} (${Number(p.current_profit_pct||0).toFixed(1)}%)</b></div><div class="position-metric"><span>SL / Trail</span><b>${Number(p.stop||0).toFixed(6)}<br><span class="tiny">${trail}</span></b></div><div class="position-metric"><span>Peak</span><b>+${Number(p.peak_profit_pct||0).toFixed(1)}%</b></div><div class="position-metric"><span>Leverage</span><b>${Number(p.leverage||0).toFixed(1)}x</b></div><div class="position-metric"><span>Notional</span><b>${money(p.remaining_notional||p.initial_notional)}</b></div><div class="position-metric"><span>Score / Corr.</span><b>${Number(p.entry_score||0).toFixed(0)} • ${corr}/2</b></div></div><div class="mobile-line"><span>Chart</span><strong>${positionCharts(p)}</strong></div></div>`}).join('')}
function renderHistoryMobile(h){const el=$('historyMobile');if(!el)return;if(!h.length){el.innerHTML='<div class="empty">Brak zamkniętych transakcji.</div>';return}el.innerHTML=h.map(e=>{const n=Number(e.net_pnl||0),win=n>=0;return `<div class="trade-card"><div class="trade-top"><strong>${e.symbol||'—'}</strong><span class="pill ${(e.direction||'').toLowerCase()}">${e.direction||'—'}</span></div><div class="mobile-line"><span>${new Date(e.timestamp).toLocaleString('pl-PL')}</span><strong class="${win?'green':'red'}">${win?'+':''}${money(n)}</strong></div><div class="mobile-line"><span>Reason</span><strong>${e.reason||'—'}</strong></div><div class="mobile-line"><span>Peak / Hold</span><strong>+${Number(e.peak_profit_pct||0).toFixed(1)}% • ${Number(e.hold_minutes||0).toFixed(1)}m</strong></div></div>`}).join('')}
async function refresh(){try{const [s,h]=await Promise.all([fetch('/status?_='+Date.now()).then(r=>r.json()),fetch('/history?limit=20000&_='+Date.now()).then(r=>r.json())]);latestStatus=s;$('equity').textContent=money(s.equity);$('real').textContent=(s.realized_pnl>=0?'+':'')+money(s.realized_pnl)+' realized';$('unreal').textContent=(s.unrealized_pnl>=0?'+':'')+money(s.unrealized_pnl);$('unreal').className='v '+(s.unrealized_pnl>=0?'green':'red');$('daily').textContent=(s.daily_pnl>=0?'+':'')+money(s.daily_pnl);$('daily').className='v '+(s.daily_pnl>=0?'green':'red');$('p24').textContent=`24H: ${s.pnl_24h>=0?'+':''}${money(s.pnl_24h)} • ${s.trades_24h||0} trades • ${Number(s.win_rate_24h_pct||0).toFixed(1)}% WR`;$('open').textContent=(s.open_positions||0)+' positions';$('win').textContent=Number(s.win_rate_pct||0).toFixed(1)+'%';$('pf').textContent='PF '+(s.profit_factor==null?'—':Number(s.profit_factor).toFixed(2));$('avgw').textContent=money(s.avg_winner);$('avgl').textContent=money(s.avg_loser);$('dd').textContent=Number(s.current_drawdown_pct||0).toFixed(2)+'%';$('mdd').textContent=Number(s.max_drawdown_pct||0).toFixed(2)+'%';$('markets').textContent=Number((s.scanner||{}).markets_seen||0).toLocaleString('pl-PL');$('agentstate').textContent=s.agent||'UNKNOWN';$('btc').textContent=(s.btc_regime||'UNKNOWN')+((s.btc_symbol)?' • '+s.btc_symbol:'');$('lossstreak').textContent=(s.consecutive_losses||0)+'/'+(s.max_consecutive_losses||3);$('halt').textContent=s.halt_reason||(s.cooldown_remaining_sec>0?'COOLDOWN':'RUNNING');$('lastloop').textContent=s.last_loop_ts?new Date(s.last_loop_ts).toLocaleTimeString('pl-PL'):'—';$('errors').textContent=s.api_errors||0;const persistStatus=s.persistence||{};$('persistence').textContent=persistStatus.ready?'NEON ✓':(persistStatus.enabled?(persistStatus.last_error?'NEON ERROR':'NEON CONNECTING'):'LOCAL ONLY');$('persistence').title=(persistStatus.last_error||'Neon PostgreSQL') + (persistStatus.state_exists===true?' • state present':persistStatus.state_exists===false?' • state empty':'');$('dbsave').textContent=persistStatus.last_save_ts?new Date(Number(persistStatus.last_save_ts)*1000).toLocaleTimeString('pl-PL'):'—';if(s.last_error||s.agent_error){$('badge').textContent='● ERROR';$('badge').style.color='var(--red)'}else if(s.agent==='STOPPED'){$('badge').textContent='● BOT STOPPED';$('badge').style.color='var(--red)'}else{$('badge').textContent='● BOT RUNNING';$('badge').style.color='var(--green)'}$('chart').innerHTML=chart(rangeData(s.equity_history));renderScanner(s.scanner||{});renderStrategy(s.strategy||{});renderDaily(s.daily_history||{});const positionsList=Object.values(s.positions||{});$('positions').innerHTML=positionsList.length?positionsList.map(p=>{const cur=p.current_price||0,pl=p.current_pnl||0;const trail=p.trailing_active?`Trail -${Number(p.trail_retrace_pct||0).toFixed(1)}%`:'Waiting +10%';const extra=p.exceptional_setup?' ⭐12%':'';const corr=Number(p.correlated_open_count||0);return `<tr><td><b>${p.symbol||'—'}${extra}</b></td><td><span class="pill ${(p.direction||'').toLowerCase()}">${p.direction||'—'}</span></td><td>${Number(p.entry||0).toFixed(6)}</td><td>${Number(cur).toFixed(6)}</td><td class="${pl>=0?'green':'red'}">${pl>=0?'+':''}${money(pl)}<br><span class="tiny">${Number(p.current_profit_pct||0).toFixed(1)}%</span></td><td>${Number(p.stop||0).toFixed(6)}<br><span class="tiny">${trail}</span></td><td>${Number(p.peak_profit_pct||0).toFixed(1)}%</td><td>${positionCharts(p)}</td><td>${Number(p.leverage||0).toFixed(1)}x</td><td>${money(p.remaining_notional||p.initial_notional)}</td><td>${Number(p.entry_score||0).toFixed(0)}<br><span class="tiny">corr ${corr}/2</span></td></tr>`}).join(''):'<tr><td colspan="11" class="empty">Brak otwartych pozycji 🐶🦆</td></tr>';renderPositionMobile(positionsList);$('history').innerHTML=h.length?h.map(e=>{const n=Number(e.net_pnl||0),win=n>=0;return `<tr><td>${new Date(e.timestamp).toLocaleString('pl-PL')}</td><td class="arrow">${win?'▲':'▼'}</td><td><b>${e.symbol||'—'}</b></td><td>${e.direction||'—'}</td><td class="${win?'green':'red'}">${win?'+':''}${money(n)}</td><td>${e.reason||'—'}</td><td>+${Number(e.peak_profit_pct||0).toFixed(1)}%</td><td>${Number(e.hold_minutes||0).toFixed(1)}m</td></tr>`}).join(''):'<tr><td colspan="8" class="empty">Brak zamkniętych transakcji.</td></tr>';renderHistoryMobile(h);$('footer').textContent='🟢 Live • '+new Date().toLocaleTimeString('pl-PL')+' • PAPER ONLY • SL -15% • trailing +10%'}catch(e){$('badge').textContent='● DASHBOARD ERROR';$('badge').style.color='var(--red)';$('footer').textContent='🔴 '+e.message}}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));b.classList.add('active');selectedRange=b.dataset.range;if(latestStatus)$('chart').innerHTML=chart(rangeData(latestStatus.equity_history))});refresh();setInterval(refresh,2000);
</script></body></html>'''

@app.get("/")
def index():
    start_agent_once()
    return Response(HTML, mimetype="text/html")

@app.get("/health")
def health():
    s = status_data()
    return jsonify({"ok": True, "paper_only": True, "agent": s.get("agent"), "last_error": s.get("last_error")})

@app.get("/status")
def status():
    start_agent_once()
    return jsonify(status_data())

@app.get("/history")
def history():
    start_agent_once()
    try:
        limit = max(1, min(20000, int(request.args.get("limit", "20000"))))
    except ValueError:
        limit = 20000
    return jsonify(read_all_exits(limit))

start_agent_once()
