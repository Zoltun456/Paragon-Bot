from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import GUILD_DB_DIR

_LOCK = threading.Lock()


def _db_path() -> Path:
    return Path(GUILD_DB_DIR) / "_user_settings.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _default_user_settings() -> Dict[str, Any]:
    return {"tts": {}}


def _normalize_user_settings(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return _default_user_settings()
    if not isinstance(payload.get("tts"), dict):
        payload["tts"] = {}
    return payload


def _persist_user_settings_locked(conn: sqlite3.Connection, user_id: int, settings: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = json.dumps(settings, separators=(",", ":"), ensure_ascii=True)
    conn.execute(
        """
        INSERT INTO user_settings (user_id, payload, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            payload=excluded.payload,
            updated_at=excluded.updated_at
        """,
        (int(user_id), payload, now),
    )
    conn.commit()


def get_user_settings(user_id: int) -> Dict[str, Any]:
    path = _db_path()
    with _LOCK:
        with _connect(path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT payload FROM user_settings WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
            if row is None:
                default_payload = _default_user_settings()
                _persist_user_settings_locked(conn, user_id, default_payload)
                return default_payload
            try:
                loaded = json.loads(str(row["payload"]))
            except Exception:
                loaded = _default_user_settings()
                _persist_user_settings_locked(conn, user_id, loaded)
                return loaded
            return _normalize_user_settings(loaded)


def set_user_settings(user_id: int, settings: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _normalize_user_settings(dict(settings or {}))
    path = _db_path()
    with _LOCK:
        with _connect(path) as conn:
            _ensure_schema(conn)
            _persist_user_settings_locked(conn, user_id, normalized)
    return normalized


def get_user_tts_profile(user_id: int) -> Dict[str, Any]:
    settings = get_user_settings(user_id)
    tts = settings.get("tts")
    if not isinstance(tts, dict):
        return {}
    profile = tts.get("voice_profile")
    if isinstance(profile, dict):
        return dict(profile)
    return {}


def set_user_tts_profile(user_id: int, profile: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_user_settings(user_id)
    tts = settings.setdefault("tts", {})
    if not isinstance(tts, dict):
        tts = {}
        settings["tts"] = tts
    tts["voice_profile"] = dict(profile or {})
    set_user_settings(user_id, settings)
    return dict(tts["voice_profile"])
