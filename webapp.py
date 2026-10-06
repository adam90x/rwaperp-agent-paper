import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, Response

from agent import main

app = Flask(__name__)
_started = False
_lock = threading.Lock()
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "paper_state.json"
REPORT_FILE = ROOT / "paper_live_report.json"
EVENT_FILE = ROOT / "paper_events.jsonl"


def start_agent_once():
    global _started
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(target=main, name="paper-agent", daemon=True).start()


def read_json(path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def dashboard_data():
    report = read_json(REPORT_FILE) or {}
    state = read_json(STATE_FILE) or {}
    positions = []
    for sym, pos in (state.get("positions") or {}).items():
        positions.append({
            "symbol": sym,
            "direction": pos.get("direction"),
            "entry": pos.get("entry"),
            "stop": pos.get("stop"),
            "target": pos.get("target"),
            "leverage": pos.get("leverage"),
            "notional": pos.get("remaining_notional", pos.get("initial_notional", 0)),
            "score": pos.get("entry_score"),
            "opened_at": pos.get("opened_at"),
            "partial_taken": pos.get("partial_taken", False),
        })

    events = []
    if EVENT_FILE.exists():
        try:
            lines = EVENT_FILE.read_text(encoding="utf-8").splitlines()[-30:]
            for line in reversed(lines):
                try:
                    e = json.loads(line)
                    if e.get("event") in {"PAPER_ENTRY", "PAPER_EXIT", "SELECT", "LOOP_ERROR", "SCAN_ERROR"}:
                        events.append({
                            "timestamp": e.get("timestamp"),
                            "event": e.get("event"),
                            "symbol": e.get("symbol"),
                            "direction": e.get("direction"),
                            "score": e.get("score"),
                            "reason": e.get("reason"),
                            "pnl": e.get("pnl"),
                            "error": e.get("error"),
                        })
                except Exception:
                    continue
        except Exception:
            pass

    data = dict(report)
    data.update({
        "server_time": datetime.now(timezone.utc).isoformat(),
        "positions": positions,
        "events": events[:15],
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "trades": state.get("trades", 0),
        "consecutive_losses": state.get("consecutive_losses", 0),
        "halt_reason": state.get("halt_reason"),
        "agent_started": _started,
        "live_order_execution": False,
    })
    return data


@app.get("/")
def root():
    start_agent_once()
    return Response(DASHBOARD_HTML, mimetype="text/html")


@app.get("/health")
def health():
    start_agent_once()
    return jsonify({
        "ok": True,
        "service": "rwaperp-agent-final-paper",
        "live_order_execution": False,
        "agent_started": _started,
    }), 200


@app.get("/status")
def status():
    start_agent_once()
    return jsonify(dashboard_data()), 200


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RWAPerp Paper Trading — Live Dashboard</title>
<style>
:root{color-scheme:dark;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
*{box-sizing:border-box}body{margin:0;background:#090b10;color:#e8ecf1}main{max-width:1250px;margin:auto;padding:24px}
header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:20px}h1{margin:0;font-size:28px}h2{font-size:17px;margin:0 0 14px}.sub{color:#8f98a8;margin-top:5px}.badge{border:1px solid #26303d;border-radius:999px;padding:7px 12px;font-size:12px;white-space:nowrap}.live{color:#7ee787;border-color:#285b39}.paper{color:#ffcf66;border-color:#66521d}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:#10141b;border:1px solid #202733;border-radius:14px;padding:16px}.label{font-size:12px;color:#8f98a8}.value{font-size:25px;font-weight:700;margin-top:7px}.positive{color:#7ee787}.negative{color:#ff7b72}
.section{margin-top:18px}.tablewrap{overflow:auto}.table{width:100%;border-collapse:collapse;min-width:760px}.table th,.table td{padding:10px 8px;border-bottom:1px solid #202733;text-align:left;font-size:13px}.table th{color:#8f98a8;font-weight:500}.long{color:#7ee787}.short{color:#ff7b72}.muted{color:#8f98a8}.empty{text-align:center;color:#737d8c;padding:28px}
.footer{margin-top:18px;color:#737d8c;font-size:12px;display:flex;justify-content:space-between}.error{color:#ff7b72}.pill{padding:3px 7px;border-radius:6px;background:#1a2029;font-size:11px}
@media(max-width:800px){main{padding:14px}.grid{grid-template-columns:repeat(2,1fr)}header{flex-direction:column}.value{font-size:21px}}
</style>
</head>
<body>
<main>
<header><div><h1>RWAPerp Paper Trading</h1><div class="sub">Live public dashboard • simulated trading only</div></div><div><span class="badge live" id="statusBadge">● CONNECTING</span> <span class="badge paper">PAPER ONLY</span></div></header>
<div class="grid">
<div class="card"><div class="label">Equity</div><div class="value" id="equity">$—</div></div>
<div class="card"><div class="label">Total P/L</div><div class="value" id="pnl">$—</div></div>
<div class="card"><div class="label">Open positions</div><div class="value" id="open">—</div></div>
<div class="card"><div class="label">Win rate</div><div class="value" id="winrate">—</div></div>
</div>
<div class="grid section">
<div class="card"><div class="label">Closed trades</div><div class="value" id="closed">—</div></div>
<div class="card"><div class="label">Peak equity</div><div class="value" id="peak">$—</div></div>
<div class="card"><div class="label">Markets scanned</div><div class="value" id="markets">—</div></div>
<div class="card"><div class="label">Scan count</div><div class="value" id="scans">—</div></div>
</div>
<section class="card section"><h2>Open positions</h2><div class="tablewrap"><table class="table"><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th><th>Notional</th><th>Lev.</th><th>Score</th><th>Opened</th></tr></thead><tbody id="positions"><tr><td colspan="9" class="empty">Loading…</td></tr></tbody></table></div></section>
<section class="card section"><h2>Recent agent events</h2><div class="tablewrap"><table class="table"><thead><tr><th>Time</th><th>Event</th><th>Market</th><th>Side</th><th>Score</th><th>Result / reason</th></tr></thead><tbody id="events"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody></table></div></section>
<div class="footer"><span>Auto-refresh: 2 seconds</span><span id="updated">Last update: —</span></div>
</main>
<script>
const money=v=>v==null?'$—':'$'+Number(v).toFixed(2);
const num=v=>v==null?'—':Number(v).toFixed(4);
const time=v=>v?v.replace('T',' ').replace('Z','').slice(0,19):'—';
function cls(v){return Number(v)>=0?'positive':'negative'}
async function refresh(){
 try{
  const r=await fetch('/status?ts='+Date.now(),{cache:'no-store'}); const d=await r.json();
  const total=(Number(d.equity||0)-500);
  document.getElementById('equity').textContent=money(d.equity);
  const p=document.getElementById('pnl');p.textContent=money(total);p.className='value '+cls(total);
  document.getElementById('open').textContent=(d.open_positions??0)+'/4';
  document.getElementById('winrate').textContent=(Number(d.win_rate_pct||0)).toFixed(1)+'%';
  document.getElementById('closed').textContent=d.closed_trades??0;
  document.getElementById('peak').textContent=money(d.peak_equity);
  document.getElementById('markets').textContent=d.markets_scanned??'—';
  document.getElementById('scans').textContent=d.scan_count??0;
  const badge=document.getElementById('statusBadge'); badge.textContent=d.halt_reason?'● HALTED':'● RUNNING'; badge.className='badge '+(d.halt_reason?'':'live');
  document.getElementById('positions').innerHTML=(d.positions||[]).length?d.positions.map(x=>`<tr><td><b>${x.symbol||'—'}</b></td><td class="${x.direction==='LONG'?'long':'short'}">${x.direction||'—'}</td><td>${num(x.entry)}</td><td>${num(x.stop)}</td><td>${num(x.target)}</td><td>${money(x.notional)}</td><td>${x.leverage?Number(x.leverage).toFixed(1)+'x':'—'}</td><td>${x.score?Number(x.score).toFixed(0):'—'}</td><td>${time(x.opened_at)}</td></tr>`).join(''):'<tr><td colspan="9" class="empty">No open positions</td></tr>';
  document.getElementById('events').innerHTML=(d.events||[]).length?d.events.map(x=>`<tr><td class="muted">${time(x.timestamp)}</td><td><span class="pill">${x.event||'—'}</span></td><td>${x.symbol||'—'}</td><td>${x.direction||'—'}</td><td>${x.score!=null?Number(x.score).toFixed(0):'—'}</td><td class="${x.error?'error':''}">${x.pnl!=null?money(x.pnl):(x.reason||x.error||'—')}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">No events yet</td></tr>';
  document.getElementById('updated').textContent='Last update: '+new Date().toLocaleTimeString();
 }catch(e){const b=document.getElementById('statusBadge');b.textContent='● OFFLINE';b.className='badge';}
}
refresh();setInterval(refresh,2000);
</script>
</body></html>'''

start_agent_once()
