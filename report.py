
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
state_file = ROOT / "paper_state.json"
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

if not state_file.exists():
    print("No paper_state.json yet. Run agent.py first.")
    raise SystemExit(0)

state = json.loads(state_file.read_text(encoding="utf-8"))
trades = state["wins"] + state["losses"]
win_rate = state["wins"] / trades * 100 if trades else 0.0
pf = state["gross_profit"] / state["gross_loss"] if state["gross_loss"] else None

print("RWAPerp Agent FINAL — PAPER REPORT")
print(f"Equity:            ${state['equity']:.2f}")
print(f"Realized P/L:      ${state['realized_pnl']:+.2f}")
print(f"Unrealized P/L:    ${state['unrealized_pnl']:+.2f}")
print(f"Closed trades:     {trades}")
print(f"Wins / losses:     {state['wins']} / {state['losses']}")
print(f"Win rate:          {win_rate:.1f}%")
print(f"Profit factor:     {pf if pf is not None else 'n/a'}")
print(f"Peak equity:       ${state['peak_equity']:.2f}")
print(f"Open positions:    {len(state['positions'])}")
print(f"Position cap:      {cfg['risk']['max_position_pct']}%")
print(f"Total exposure:    {cfg['risk']['max_total_position_pct']}%")
print(f"Leverage range:    {cfg['risk']['min_leverage']}x–{cfg['risk']['max_leverage']}x")
print(f"Compounding:       {cfg['capital']['compound_profits']}")
