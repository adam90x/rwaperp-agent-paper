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
from typing import Optional, Dict, Any

_DB_READY = False
_LAST_DB_SAVE = 0.0
_LAST_DB_ERROR = None
_CHECKPOINT_SECONDS = 300.0  # 5 minutes; entries/exits force an immediate save.


def _dsn() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def enabled() -> bool:
    return bool(_dsn())


def status() -> Dict[str, Any]:
    return {
        "enabled": enabled(),
        "ready": _DB_READY,
        "last_error": _LAST_DB_ERROR,
        "last_save_ts": _LAST_DB_SAVE,
    }


def _connect():
    import psycopg2
    return psycopg2.connect(_dsn(), connect_timeout=10, sslmode="require")


def init_db() -> bool:
    global _DB_READY, _LAST_DB_ERROR
    if not enabled():
        _LAST_DB_ERROR = "DATABASE_URL not set"
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
        return True
    except Exception as exc:
        _DB_READY = False
        _LAST_DB_ERROR = f"{type(exc).__name__}: {exc}"
        return False


def load_state() -> Optional[Dict[str, Any]]:
    global _DB_READY, _LAST_DB_ERROR
    if not enabled():
        return None
    try:
        if not _DB_READY and not init_db():
            return None
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM rwaperp_state WHERE state_id = 1")
                row = cur.fetchone()
        if not row:
            return None
        _DB_READY = True
        _LAST_DB_ERROR = None
        state = row[0]
        return state if isinstance(state, dict) else json.loads(state)
    except Exception as exc:
        _DB_READY = False
        _LAST_DB_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def save_state(state: Dict[str, Any], force: bool = False) -> bool:
    global _DB_READY, _LAST_DB_ERROR, _LAST_DB_SAVE
    if not enabled():
        return False
    now = time.time()
    if not force and (now - _LAST_DB_SAVE) < _CHECKPOINT_SECONDS:
        return False
    try:
        if not _DB_READY and not init_db():
            return False
        payload = json.dumps(state, default=str, separators=(",", ":"))
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
        _LAST_DB_SAVE = now
        return True
    except Exception as exc:
        _DB_READY = False
        _LAST_DB_ERROR = f"{type(exc).__name__}: {exc}"
        return False
