import json
import threading
from pathlib import Path
from flask import Flask, jsonify, Response
from agent import main

app = Flask(__name__)
_started = False
_lock = threading.Lock()
BASE = Path(__file__).resolve().parent

DASHBOARD_HTML = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RWAPerp • PAPER Trading Lab</title>
<style>
:root{--bg:#070b11;--card:#101722e8;--line:#263140;--text:#edf2f7;--muted:#8d99a8;--gold:#f6c453;--green:#57e389;--red:#ff6975;--cyan:#55d9ff}
*{box-sizing:border-box}body{margin:0;color:var(--text);font:14px Inter,Arial,sans-serif;background:
radial-gradient(circle at 10% 20%,#18212e 0,transparent 25%),
radial-gradient(circle at 90% 80%,#191423 0,transparent 25%),var(--bg);overflow-x:hidden}
body:before{content:"🐶🦆  🐶🦆  🐶🦆  🐶🦆  🐶🦆";position:fixed;inset:0;z-index:-1;opacity:.035;font-size:58px;line-height:2.1;word-spacing:32px;transform:rotate(-8deg) scale(1.2);pointer-events:none}
.wrap{max-width:1450px;margin:auto;padding:22px}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:18px}
h1{margin:0;font-size:28px;letter-spacing:-.5px}.sub{color:var(--muted);margin-top:6px}
.badge{padding:9px 14px;border:1px solid #2d4435;border-radius:999px;background:#102118;color:var(--green);font-weight:800;box-shadow:0 0 25px #57e38914}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:16px;box-shadow:0 10px 35px #0005;backdrop-filter:blur(8px)}
.k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px}.v{font-size:25px;font-weight:850;margin-top:7px}.gold{color:var(--gold)}.green{color:var(--green)}.red{color:var(--red)}
.span2{grid-column:span 2}.span4{grid-column:1/-1}
.section{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.section h2{font-size:15px;margin:0}.tiny{font-size:11px;color:var(--muted)}
.chart{height:260px;position:relative}.chart svg{width:100%;height:100%;display:block}.chartline{fill:none;stroke:var(--gold);stroke-width:3}.gridline{stroke:#25303d;stroke-width:1}.axis{fill:#738093;font-size:10px}
table{width:100%;border-collapse:collapse}th,td{padding:10px 8px;border-bottom:1px solid #202a36;text-align:left;white-space:nowrap}th{font-size:10px;color:var(--muted);letter-spacing:.7px}td{font-size:12px}
.arrow{color:var(--gold);font-size:18px;font-weight:900}.pill{display:inline-block;padding:4px 7px;border-radius:8px;font-size:10px;font-weight:800}.long{background:#123622;color:var(--green)}.short{background:#3a1820;color:#ff8b95}
.scroll{overflow:auto}.empty{color:var(--muted);padding:20px 0}.footer{color:#687586;font-size:11px;margin:16px 2px 4px;text-align:center}
@media(max-width:1000px){.grid{grid-template-columns:repeat(2,1fr)}.span4{grid-column:1/-1}}
@media(max-width:650px){.grid{grid-template-columns:1fr}.span2,.span4{grid-column:span 1}.wrap{padding:12px}header{align-items:flex-start;flex-direction:column}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div><h1>🤖 RWAPerp <span class="gold">PAPER Trading Lab</span></h1><div class="sub">Live paper-trading dashboard • 🐶🦆 edition • real orders are disabled</div></div>
  <div id="status" class="badge">CONNECTING…</div>
</header>

<div class="grid">
  <div class="card"><div class="k">Equity</div><div id="equity" class="v">$—</div></div>
  <div class="card"><div class="k">Total P/L</div><div id="pnl" class="v">—</div></div>
  <div class="card"><div class="k">Open positions</div><div id="open" class="v">—</div></div>
  <div class="card"><div class="k">Win rate</div><div id="winrate" class="v">—</div></div>
  <div class="card"><div class="k">Closed trades</div><div id="closed" class="v">—</div></div>
  <div class="card"><div class="k">Profit factor</div><div id="pf" class="v">—</div></div>
  <div class="card"><div class="k">Peak equity</div><div id="peak" class="v">$—</div></div>
  <div class="card"><div class="k">Markets scanned</div><div id="scans" class="v">—</div></div>

  <div class="card span2"><div class="section"><h2>📈 Equity curve</h2><span class="tiny">last dashboard observations</span></div><div id="chart" class="chart"></div></div>
  <div class="card span2"><div class="section"><h2>🎯 Performance</h2><span class="tiny">wins vs losses</span></div><div id="perfchart" class="chart"></div></div>

  <div class="card span4"><div class="section"><h2>🟢 Open positions</h2><span class="tiny">live • refresh 2s</span></div>
    <div class="scroll"><table><thead><tr><th></th><th>Market</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Lev.</th><th>Notional</th><th>Score</th></tr></thead><tbody id="positions"></tbody></table></div>
  </div>

  <div class="card span4"><div class="section"><h2>🏆 Trade history</h2><span class="tiny">golden arrows = completed paper trades</span></div>
    <div class="scroll"><table><thead><tr><th>Time</th><th></th><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L</th><th>Reason</th><th>Lev.</th></tr></thead><tbody id="history"></tbody></table></div>
  </div>
</div>
<div id="updated" class="footer">Waiting for agent…</div>
</div>

<script>
const $=x=>document.getElementById(x);
const money=x=>'$'+Number(x||0).toFixed(2);
let equityHistory=[], lastHistory=[];

function lineChart(values){
 if(!values.length)return '<div class="empty">Brak danych do wykresu — agent dopiero zaczyna.</div>';
 const w=800,h=240,p=28,min=Math.min(...values),max=Math.max(...values),range=max-min||1;
 const pts=values.map((v,i)=>[p+i*(w-2*p)/Math.max(1,values.length-1),h-p-(v-min)*(h-2*p)/range]);
 const d=pts.map((q,i)=>(i?'L':'M')+q[0].toFixed(1)+' '+q[1].toFixed(1)).join(' ');
 const guides=[0,1,2,3,4].map(i=>{let y=p+i*(h-2*p)/4;return `<line class="gridline" x1="${p}" y1="${y}" x2="${w-p}" y2="${y}"/>`}).join('');
 return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${guides}<path class="chartline" d="${d}"/><circle cx="${pts.at(-1)[0]}" cy="${pts.at(-1)[1]}" r="5" fill="#f6c453"/><text class="axis" x="${p}" y="18">${money(max)}</text><text class="axis" x="${p}" y="${h-5}">${money(min)}</text></svg>`;
}
function perfChart(wins,losses){
 const total=wins+losses||1, bw=150, h=210, base=185, wh=130*wins/total, lh=130*losses/total;
 return `<svg viewBox="0 0 420 210"><line class="gridline" x1="40" y1="${base}" x2="390" y2="${base}"/><rect x="105" y="${base-wh}" width="${bw}" height="${wh}" rx="8" fill="#57e389"/><rect x="260" y="${base-lh}" width="${bw}" height="${lh}" rx="8" fill="#ff6975"/><text class="axis" x="150" y="${base+18}">WINS ${wins}</text><text class="axis" x="305" y="${base+18}">LOSSES ${losses}</text></svg>`;
}
function renderPositions(ps){
 const rows=Object.values(ps||{}).map(p=>`<tr><td class="arrow">➜</td><td><b>${p.symbol||'—'}</b></td><td><span class="pill ${(p.direction||'').toLowerCase()}">${p.direction||'—'}</span></td><td>${Number(p.entry||0).toFixed(6)}</td><td>${Number(p.stop||0).toFixed(6)}</td><td>${Number(p.target||0).toFixed(6)}</td><td>${Number(p.leverage||0).toFixed(1)}x</td><td>${money(p.remaining_notional||p.initial_notional)}</td><td>${Number(p.entry_score||0).toFixed(0)}</td></tr>`).join('');
 $('positions').innerHTML=rows||'<tr><td colspan="9" class="empty">Brak otwartych pozycji 🐶🦆</td></tr>';
}
function renderHistory(items){
 $('history').innerHTML=items.length?items.map(e=>{
  const win=Number(e.net_pnl||0)>=0;
  return `<tr><td>${new Date(e.timestamp).toLocaleTimeString('pl-PL')}</td><td class="arrow">${win?'▲':'▼'}</td><td><b>${e.symbol||'—'}</b></td><td><span class="pill ${(e.direction||'').toLowerCase()}">${e.direction||'—'}</span></td><td>${Number(e.entry||0).toFixed(6)}</td><td>${Number(e.exit||0).toFixed(6)}</td><td class="${win?'green':'red'}">${win?'+':''}${money(e.net_pnl||0)}</td><td>${e.reason||'—'}</td><td>${Number(e.leverage||0).toFixed(1)}x</td></tr>`
 }).join(''):'<tr><td colspan="9" class="empty">Brak zamkniętych transakcji — polowanie dopiero się zaczyna.</td></tr>';
}
async function refresh(){
 try{
  const [s,h]=await Promise.all([fetch('/status?_='+Date.now(),{cache:'no-store'}),fetch('/history?_='+Date.now(),{cache:'no-store'})]);
  const d=await s.json(), hist=await h.json();
  $('status').textContent=d.halt_reason?'HALTED':'RUNNING';
  $('status').style.color=d.halt_reason?'#ff6975':'#57e389';
  $('equity').textContent=money(d.equity);
  const pnl=Number(d.equity||0)-Number(d.starting_equity||500);
  $('pnl').textContent=(pnl>=0?'+':'')+money(pnl); $('pnl').className='v '+(pnl>=0?'green':'red');
  $('open').textContent=(d.open_positions||0)+' / 4'; $('winrate').textContent=Number(d.win_rate_pct||0).toFixed(1)+'%';
  $('closed').textContent=d.closed_trades??0; $('pf').textContent=d.profit_factor==null?'—':Number(d.profit_factor).toFixed(2);
  $('peak').textContent=money(d.peak_equity); $('scans').textContent=d.scan_count??0;
  const eq=Number(d.equity||0); equityHistory.push(eq); if(equityHistory.length>80)equityHistory.shift();
  $('chart').innerHTML=lineChart(equityHistory);
  $('perfchart').innerHTML=perfChart(Number(d.wins||0),Number(d.losses||0));
  renderPositions(d.positions); renderHistory(hist);
  $('updated').textContent='🟢 Live • aktualizacja '+new Date().toLocaleTimeString('pl-PL')+' • PAPER ONLY';
 }catch(e){$('status').textContent='OFFLINE';$('status').style.color='#ff6975';$('updated').textContent='🔴 Dashboard error: '+e.message}
}
refresh();setInterval(refresh,2000);
</script>
</body>
</html>"""

def start_agent_once():
    global _started
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(target=main, name="paper-agent", daemon=True).start()

@app.get("/")
def root():
    start_agent_once()
    return Response(DASHBOARD_HTML, mimetype="text/html")

@app.get("/health")
def health():
    start_agent_once()
    return jsonify({"ok": True, "service": "rwaperp-agent-final-paper", "live_order_execution": False}), 200

@app.get("/status")
def status():
    start_agent_once()
    p = BASE / "paper_live_report.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            state = BASE / "paper_state.json"
            if state.exists():
                try:
                    st = json.loads(state.read_text(encoding="utf-8"))
                    data["positions"] = st.get("positions", {})
                    data["halt_reason"] = st.get("halt_reason")
                    data["starting_equity"] = st.get("starting_equity", 500.0)
                    data["wins"] = st.get("wins", 0)
                    data["losses"] = st.get("losses", 0)
                except Exception:
                    pass
            return jsonify(data), 200
        except Exception:
            pass
    return jsonify({"ok": True, "status": "starting", "equity": 500, "starting_equity": 500, "positions": {}, "live_order_execution": False}), 200

@app.get("/history")
def history():
    start_agent_once()
    p = BASE / "paper_events.jsonl"
    out = []
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines()[-300:]:
                try:
                    e = json.loads(line)
                    if e.get("event") in ("PAPER_EXIT", "PAPER_ENTRY", "PAPER_PARTIAL"):
                        out.append(e)
                except Exception:
                    continue
        except Exception:
            pass
    out = out[-100:][::-1]
    return jsonify(out), 200

start_agent_once()
