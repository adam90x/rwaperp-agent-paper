"""
RWAPerp Trading Lab V2.2 - durable Neon PostgreSQL persistence.

The Render filesystem remains only a local cache. Neon is the durable source
for paper state. The implementation intentionally writes on important state
changes (entry/exit) and at a low-frequency checkpoint so the Free plan can
still scale to zero when idle.
"""
import json
import os
import time
import threading
from typing import Optional, Dict, Any

_DB_READY = False
_LAST_DB_SAVE = 0.0
_LAST_DB_ERROR = None
_LAST_LOAD_FOUND = None
_CHECKPOINT_SECONDS = 30.0  # 30 seconds; entries/exits force an immediate save.
_DB_LOCK = threading.RLock()
_SAVE_RETRIES = 3
_SAVE_RETRY_DELAY = 1.5


def _dsn() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def enabled() -> bool:
    return bool(_dsn())


def status() -> Dict[str, Any]:
    with _DB_LOCK:
        return {
            "enabled": enabled(),
            "ready": _DB_READY,
            "last_error": _LAST_DB_ERROR,
            "last_save_ts": _LAST_DB_SAVE,
            "state_exists": _LAST_LOAD_FOUND,
            "checkpoint_seconds": _CHECKPOINT_SECONDS,
            "save_retries": _SAVE_RETRIES,
        }

def _error_text(exc: Exception) -> str:
    # Never expose DATABASE_URL / password in the dashboard or logs.
    msg = str(exc).replace(_dsn(), "[DATABASE_URL]") if _dsn() else str(exc)
    return f"{type(exc).__name__}: {msg}"[:500]


def _connect():
    import psycopg2
    return psycopg2.connect(_dsn(), connect_timeout=10, sslmode="require")


def init_db() -> bool:
    global _DB_READY, _LAST_DB_ERROR, _LAST_LOAD_FOUND, _LAST_DB_SAVE
    with _DB_LOCK:
        if not enabled():
            _LAST_DB_ERROR = "DATABASE_URL not set"
            print("[PERSISTENCE] DISABLED: DATABASE_URL not set", flush=True)
            return False
        try:
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS rwaperp_state (
                            state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
                            state JSONB NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
                conn.commit()
            _DB_READY = True
            _LAST_DB_ERROR = None
            print("[PERSISTENCE] Neon PostgreSQL READY", flush=True)
            return True
        except Exception as exc:
            _DB_READY = False
            _LAST_DB_ERROR = _error_text(exc)
            print(f"[PERSISTENCE] Neon PostgreSQL ERROR: {_LAST_DB_ERROR}", flush=True)
            return False

def load_state() -> Optional[Dict[str, Any]]:
    global _DB_READY, _LAST_DB_ERROR, _LAST_LOAD_FOUND, _LAST_DB_SAVE
    with _DB_LOCK:
        if not enabled():
            _LAST_LOAD_FOUND = None
            return None
        try:
            if not _DB_READY and not init_db():
                return None
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT state FROM rwaperp_state WHERE state_id = 1")
                    row = cur.fetchone()
            _DB_READY = True
            _LAST_DB_ERROR = None
            if not row:
                _LAST_LOAD_FOUND = False
                return None
            _LAST_LOAD_FOUND = True
            state = row[0]
            state = state if isinstance(state, dict) else json.loads(state)
            try:
                _LAST_DB_SAVE = float(state.get("last_db_save_ts", 0.0) or 0.0)
            except (TypeError, ValueError):
                _LAST_DB_SAVE = 0.0
            return state
        except Exception as exc:
            _DB_READY = False
            _LAST_LOAD_FOUND = None
            _LAST_DB_ERROR = _error_text(exc)
            print(f"[PERSISTENCE] Neon load ERROR: {_LAST_DB_ERROR}", flush=True)
            return None

def save_state(state: Dict[str, Any], force: bool = False, reason: str = "checkpoint") -> bool:
    global _DB_READY, _LAST_DB_ERROR, _LAST_DB_SAVE
    with _DB_LOCK:
        if not enabled():
            _LAST_DB_ERROR = "DATABASE_URL not set"
            return False

        # Re-establish DB health before using the checkpoint timer. A recent
        # save timestamp must never be treated as proof that the current
        # connection is still healthy.
        if not _DB_READY and not init_db():
            return False

        now = time.time()
        if not force and (now - _LAST_DB_SAVE) < _CHECKPOINT_SECONDS:
            return True

        save_ts = now
        try:
            state["last_db_save_ts"] = save_ts
            payload = json.dumps(state, default=str, separators=(",", ":"))
        except Exception as exc:
            _LAST_DB_ERROR = _error_text(exc)
            print(f"[PERSISTENCE] Neon payload ERROR: {_LAST_DB_ERROR}", flush=True)
            return False

        last_error = None
        for attempt in range(1, _SAVE_RETRIES + 1):
            try:
                with _connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO rwaperp_state (state_id, state, updated_at)
                            VALUES (1, %s::jsonb, NOW())
                            ON CONFLICT (state_id) DO UPDATE
                            SET state = EXCLUDED.state,
                                updated_at = NOW()
                        """, (payload,))
                    conn.commit()
                _DB_READY = True
                _LAST_DB_ERROR = None
                _LAST_DB_SAVE = save_ts
                print(f"[PERSISTENCE] SAVE OK | reason={reason} | ts={save_ts:.3f}", flush=True)
                return True
            except Exception as exc:
                _DB_READY = False
                last_error = _error_text(exc)
                _LAST_DB_ERROR = last_error
                print(f"[PERSISTENCE] Neon save attempt {attempt}/{_SAVE_RETRIES} ERROR: {last_error}", flush=True)
                if attempt < _SAVE_RETRIES:
                    time.sleep(_SAVE_RETRY_DELAY * attempt)

        _LAST_DB_ERROR = last_error or "unknown Neon save failure"
        return False
