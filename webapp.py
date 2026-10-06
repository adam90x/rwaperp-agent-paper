import json
import threading
import runpy
from pathlib import Path
from flask import Flask, jsonify, Response

BASE = Path(__file__).resolve().parent
app = Flask(__name__)
_started = False
_lock = threading.Lock()


def read_json(name, default):
    p = BASE / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def read_events(limit=100):
    p = BASE / "paper_events.jsonl"
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines()[-2000:]:
            try:
                e = json.loads(line)
                if e.get("event") in {"PAPER_ENTRY", "PAPER_EXIT"}:
                    out.append(e)
            except Exception:
                pass
    except Exception:
        return []
    return list(reversed(out[-limit:]))


def status_data():
    state = read_json("paper_state.json", {
        "equity": 500.0, "cash": 500.0, "realized_pnl": 0.0,
        "unrealized_pnl": 0.0, "wins": 0, "losses": 0,
        "positions": {}, "halt_reason": None
    })
    report = read_json("paper_live_report.json", {})
    closed = int(state.get("wins", 0)) + int(state.get("losses", 0))
    winrate = (state.get("wins", 0) / closed * 100) if closed else 0.0
    return {
        "equity": state.get("equity", 500.0),
        "cash": state.get("cash", 500.0),
        "realized_pnl": state.get("realized_pnl", 0.0),
        "unrealized_pnl": state.get("unrealized_pnl", 0.0),
        "closed_trades": closed,
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "win_rate_pct": winrate,
        "positions": state.get("positions", {}),
        "open_positions": len(state.get("positions", {})),
        "halt_reason": state.get("halt_reason"),
        "markets_count": report.get("scan_count", 0) and report.get("markets_count", 0),
        "scan_count": report.get("scan_count", 0),
        "live_order_execution": False,
        "agent": "RUNNING"
    }


def run_agent():
    try:
        runpy.run_path(str(BASE / "agent.py"), run_name="__main__")
    except Exception as exc:
        print(f"[AGENT THREAD ERROR] {type(exc).__name__}: {exc}", flush=True)


def start_agent_once():
    global _started
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(target=run_agent, name="paper-agent", daemon=True).start()
        print("[WEBAPP] PAPER agent thread started", flush=True)


HTML = r'''<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dziobak & Mops • RWAPerp PAPER</title>
<style>
:root{--bg:#05080d;--panel:rgba(9,16,26,.91);--line:#26384b;--text:#f4f7fb;--muted:#8fa0b3;--gold:#f5c451;--green:#35e58a;--red:#ff5e6c;--purple:#8b5cf6}
*{box-sizing:border-box}body{margin:0;color:var(--text);font:14px Segoe UI,Arial,sans-serif;background:radial-gradient(circle at 10% 10%,#171225,transparent 30%),radial-gradient(circle at 90% 90%,#122019,transparent 30%),var(--bg)}
body:before{content:"🐶   🦆     🐶   🦆     🐶   🦆     🐶   🦆";position:fixed;inset:0;z-index:-1;opacity:.035;font-size:64px;line-height:2.5;transform:rotate(-8deg) scale(1.25);pointer-events:none}
.wrap{max-width:1450px;margin:auto;padding:22px}header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}h1{margin:0;font-size:28px}h1 span{color:var(--gold)}.sub{color:var(--muted);margin-top:5px}.badge{border:1px solid #27533c;border-radius:999px;padding:9px 14px;color:var(--green);background:#0c1c14;font-weight:800}.nav,.card{background:var(--panel);border:1px solid var(--line);border-radius:15px;box-shadow:0 12px 40px #0007;backdrop-filter:blur(8px)}.nav{padding:12px 16px;margin-bottom:13px;color:#d8e0ea}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:13px}.card{padding:16px}.span2{grid-column:span 2}.span4{grid-column:span 4}.k{font-size:10px;letter-spacing:1px;color:var(--muted);text-transform:uppercase}.v{font-size:27px;font-weight:900;margin-top:6px}.green{color:var(--green)}.red{color:var(--red)}.gold{color:var(--gold)}.section{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.section h2{font-size:16px;margin:0}.tiny{font-size:11px;color:var(--muted)}.statusrow{display:grid;grid-template-columns:22px 1fr auto;gap:9px;align-items:center;padding:7px 0}.check{color:var(--green);background:#103a27;border-radius:50%;width:18px;height:18px;text-align:center;line-height:18px}.chart{height:220px;display:flex;align-items:center;justify-content:center;color:var(--muted)}svg{width:100%;height:210px}table{width:100%;border-collapse:collapse}th{font-size:10px;color:var(--muted);text-transform:uppercase;text-align:left;padding:9px 7px;border-bottom:1px solid var(--line)}td{padding:10px 7px;border-bottom:1px solid #1b2836}.arrow{color:var(--gold);font-weight:900}.empty{color:var(--muted);padding:20px}.pill{padding:4px 8px;border-radius:999px;font-size:11px;font-weight:800}.long{color:var(--green);background:#103b28}.short{color:var(--red);background:#3a1820}.footer{text-align:center;color:var(--muted);font-size:11px;padding:15px}.scan{font-size:42px;font-weight:900}.notice{margin-top:10px;color:var(--muted)}@media(max-width:900px){.grid{grid-template-columns:1fr 1fr}.span4{grid-column:span 2}.span2{grid-column:span 2}}@media(max-width:600px){.grid{grid-template-columns:1fr}.span2,.span4{grid-column:span 1}}
</style></head><body><div class="wrap">
<header><div><h1>🤖 RWAPerp <span>PAPER Trading Lab</span></h1><div class="sub">Paper trading • real orders wyłączone • Dziobaki & Mopsy</div></div><div id="badge" class="badge">● BOT RUNNING</div></header>
<div class="nav">▣ Dashboard &nbsp;&nbsp;→ Live Trades &nbsp;&nbsp;☷ Historia &nbsp;&nbsp;♘ Scanner &nbsp;&nbsp;▦ Markets &nbsp;&nbsp;⌁ Status</div>
<div class="grid">
<div class="card"><div class="k">Total balance</div><div id="equity" class="v">$500.00</div><div id="real" class="tiny">+$0.00</div></div>
<div class="card"><div class="k">Open P/L</div><div id="unreal" class="v green">+$0.00</div><div id="open" class="tiny">0 positions</div></div>
<div class="card"><div class="k">Today's / total P/L</div><div id="pnl" class="v green">+$0.00</div><div id="closed" class="tiny">0 trades</div></div>
<div class="card"><div class="k">Win rate</div><div id="win" class="v">0.0%</div><div id="wl" class="tiny">0W / 0L</div></div>
<div class="card span2"><div class="section"><h2>📈 Portfolio Value</h2><span class="tiny">live • 2s dashboard refresh</span></div><div id="chart" class="chart">Agent is collecting data…</div></div>
<div class="card span2"><div class="section"><h2>🤖 Agent Status</h2><span class="tiny">PAPER ONLY</span></div><div class="statusrow"><b class="check">✓</b><span>Agent process</span><span id="agentstate">RUNNING</span></div><div class="statusrow"><b class="check">✓</b><span>Market cycle</span><span>≤ 5s</span></div><div class="statusrow"><b class="check">✓</b><span>Markets cached</span><span id="markets">0</span></div><div class="statusrow"><b class="check">✓</b><span>Paper trading</span><span>ACTIVE</span></div><div class="statusrow"><b class="check">✓</b><span>Real orders</span><span>DISABLED</span></div></div>
<div class="card span4"><div class="section"><h2>🟢 Open Positions</h2><span class="tiny">maks. 4 • max 7% notional / pozycję</span></div><div style="overflow:auto"><table><thead><tr><th>→</th><th>Market</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Lev.</th><th>Notional</th><th>Score</th></tr></thead><tbody id="positions"></tbody></table></div></div>
<div class="card span2"><div class="section"><h2>🏆 Recent Trades</h2><span class="tiny">golden arrows</span></div><div style="overflow:auto"><table><thead><tr><th>Time</th><th></th><th>Market</th><th>Side</th><th>P/L</th><th>Reason</th></tr></thead><tbody id="history"></tbody></table></div></div>
<div class="card span2"><div class="section"><h2>📡 Scanner</h2><span class="tiny">rotacja rynku</span></div><div id="scanbig" class="scan">0</div><div class="sub">cached markets available to the decision engine</div><div class="notice">15 świeżych rynków na cykl, otwarte pozycje zawsze odświeżane.</div></div>
</div><div id="footer" class="footer">🟢 Live • connecting… • PAPER ONLY</div></div>
<script>
const $=id=>document.getElementById(id), money=x=>'$'+Number(x||0).toFixed(2);let histEq=[];
function chart(vals){if(!vals.length)return 'Agent is collecting data…';const w=820,h=210,p=25,min=Math.min(...vals),max=Math.max(...vals),r=max-min||1;const pts=vals.map((v,i)=>[p+i*(w-2*p)/Math.max(1,vals.length-1),h-p-(v-min)*(h-2*p)/r]);const d=pts.map((q,i)=>(i?'L':'M')+q[0].toFixed(1)+' '+q[1].toFixed(1)).join(' ');return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="#8b5cf6" stroke-width="3"/><circle cx="${pts.at(-1)[0]}" cy="${pts.at(-1)[1]}" r="4" fill="#f5c451"/></svg>`}
async function refresh(){try{const [s,h]=await Promise.all([fetch('/status?_='+Date.now()).then(r=>r.json()),fetch('/history?_='+Date.now()).then(r=>r.json())]);$('equity').textContent=money(s.equity);$('real').textContent=(s.realized_pnl>=0?'+':'')+money(s.realized_pnl);$('unreal').textContent=(s.unrealized_pnl>=0?'+':'')+money(s.unrealized_pnl);$('pnl').textContent=(s.realized_pnl>=0?'+':'')+money(s.realized_pnl);$('open').textContent=(s.open_positions||0)+' positions';$('closed').textContent=(s.closed_trades||0)+' trades';$('win').textContent=Number(s.win_rate_pct||0).toFixed(1)+'%';$('wl').textContent=(s.wins||0)+'W / '+(s.losses||0)+'L';$('markets').textContent=s.markets_count||0;$('scanbig').textContent=s.markets_count||0;histEq.push(Number(s.equity||500));if(histEq.length>100)histEq.shift();$('chart').innerHTML=chart(histEq);const ps=Object.values(s.positions||{});$('positions').innerHTML=ps.length?ps.map(p=>`<tr><td class="arrow">➜</td><td><b>${p.symbol||'—'}</b></td><td><span class="pill ${(p.direction||'').toLowerCase()}">${p.direction||'—'}</span></td><td>${Number(p.entry||0).toFixed(6)}</td><td>${Number(p.stop||0).toFixed(6)}</td><td>${Number(p.target||0).toFixed(6)}</td><td>${Number(p.leverage||0).toFixed(1)}x</td><td>${money(p.remaining_notional||p.initial_notional)}</td><td>${Number(p.entry_score||0).toFixed(0)}</td></tr>`).join(''):'<tr><td colspan="9" class="empty">Brak otwartych pozycji 🐶🦆</td></tr>';$('history').innerHTML=h.length?h.slice(0,20).map(e=>{const n=Number(e.net_pnl||0),win=n>=0;return `<tr><td>${new Date(e.timestamp).toLocaleTimeString('pl-PL')}</td><td class="arrow">${win?'▲':'▼'}</td><td><b>${e.symbol||'—'}</b></td><td>${e.direction||'—'}</td><td class="${win?'green':'red'}">${win?'+':''}${money(n)}</td><td>${e.reason||'—'}</td></tr>`}).join(''):'<tr><td colspan="6" class="empty">Brak zamkniętych transakcji — polowanie dopiero się zaczyna.</td></tr>';$('footer').textContent='🟢 Live • '+new Date().toLocaleTimeString('pl-PL')+' • PAPER ONLY • real orders disabled'}catch(e){$('badge').textContent='● DASHBOARD ERROR';$('badge').style.color='var(--red)';$('footer').textContent='🔴 '+e.message}}
refresh();setInterval(refresh,2000);
</script></body></html>'''


@app.get("/")
def index():
    start_agent_once()
    return Response(HTML, mimetype="text/html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "paper_only": True})


@app.get("/status")
def status():
    start_agent_once()
    return jsonify(status_data())


@app.get("/history")
def history():
    start_agent_once()
    return jsonify(read_events())


start_agent_once()
