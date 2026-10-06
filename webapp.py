
import json
import threading
from pathlib import Path
from flask import Flask, jsonify

from agent import main

app = Flask(__name__)
_started = False
_lock = threading.Lock()

def start_agent_once():
    global _started
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(
            target=main,
            name="paper-agent",
            daemon=True,
        ).start()

@app.get("/")
def root():
    start_agent_once()
    return "RWAPerp Agent FINAL — PAPER ONLY\n", 200

@app.get("/health")
def health():
    start_agent_once()
    return jsonify({
        "ok": True,
        "service": "rwaperp-agent-final-paper",
        "live_order_execution": False
    }), 200

@app.get("/status")
def status():
    start_agent_once()
    p = Path(__file__).resolve().parent / "paper_live_report.json"
    if p.exists():
        try:
            return jsonify(json.loads(p.read_text(encoding="utf-8"))), 200
        except Exception:
            pass
    return jsonify({"ok": True, "status": "starting", "live_order_execution": False}), 200

start_agent_once()
