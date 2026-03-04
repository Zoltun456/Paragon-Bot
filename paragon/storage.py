from __future__ import annotations

import asyncio
import pickle
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import GUILD_DB_DIR

SAVE_LOCK = asyncio.Lock()
data: Dict[str, Any] = {"guilds": {}}


def _db_path_for_guild(guild_id: int) -> Path:
    return Path(GUILD_DB_DIR) / f"{int(guild_id)}.db"


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
        CREATE TABLE IF NOT EXISTS app_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            payload BLOB NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _default_state() -> Dict[str, Any]:
    return {"guilds": {}}


def _default_guild_state() -> Dict[str, Any]:
    return {
        "users": {},
        "settings": {"inactive_loss_enabled": True},
        "channels": {},
    }


def _normalize_state(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return _default_state()
    guilds = payload.get("guilds")
    if not isinstance(guilds, dict):
        payload["guilds"] = {}
    return payload


def _normalize_guild_state(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return _default_guild_state()
    if not isinstance(payload.get("users"), dict):
        payload["users"] = {}
    if not isinstance(payload.get("channels"), dict):
        payload["channels"] = {}
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        settings = {}
        payload["settings"] = settings
    if "inactive_loss_enabled" not in settings:
        settings["inactive_loss_enabled"] = True
    return payload


def load_data() -> None:
    global data
    data = _default_state()


def _persist_snapshot_locked(conn: sqlite3.Connection, snapshot: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = pickle.dumps(snapshot, protocol=pickle.HIGHEST_PROTOCOL)
    conn.execute(
        """
        INSERT INTO app_state (id, payload, updated_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            payload=excluded.payload,
            updated_at=excluded.updated_at
        """,
        (payload, now),
    )
    conn.commit()


def _persist_snapshot(snapshot: Dict[str, Any]) -> None:
    path = _db_path_for_guild(int(snapshot["guild_id"]))
    payload = snapshot["payload"]
    with _connect(path) as conn:
        _ensure_schema(conn)
        _persist_snapshot_locked(conn, payload)


async def save_data() -> None:
    guilds_snapshot = deepcopy(data.get("guilds", {}))
    async with SAVE_LOCK:
        await asyncio.to_thread(_persist_all_guilds_snapshot, guilds_snapshot)


def _gdict(gid: int) -> dict:
    gs = data.setdefault("guilds", {})
    key = str(gid)
    g = gs.get(key)
    if g is not None:
        return g
    g = _load_guild_state(gid)
    gs[key] = g
    return g


def _udict(gid: int, uid: int) -> dict:
    g = _gdict(gid)
    users = g.setdefault("users", {})
    u = users.get(str(uid))
    if u is None:
        u = {
            "xp": 0,
            "xp_f": 0.0,
            "level": 1,
            "xp_boosts": [],
            "total_active_minutes": 0,
            "total_inactive_minutes": 0,
            "bucket_active_remainder": 0,
            "bucket_inactive_remainder": 0,
            "prestige": 0,
            "stats": {},
        }
        users[str(uid)] = u
    if "xp_f" not in u:
        u["xp_f"] = float(u.get("xp", 0))
    if "level" not in u:
        u["level"] = 1
    if "xp_boosts" not in u or not isinstance(u.get("xp_boosts"), list):
        u["xp_boosts"] = []
    return u


def _load_guild_state(guild_id: int) -> Dict[str, Any]:
    path = _db_path_for_guild(guild_id)
    with _connect(path) as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT payload FROM app_state WHERE id = 1").fetchone()
        if row is None:
            default_payload = _default_guild_state()
            _persist_snapshot_locked(conn, default_payload)
            return default_payload
        try:
            loaded = pickle.loads(row["payload"])
        except Exception:
            loaded = _default_guild_state()
            _persist_snapshot_locked(conn, loaded)
        return _normalize_guild_state(loaded)


def _persist_all_guilds_snapshot(guilds_snapshot: Dict[str, Dict[str, Any]]) -> None:
    for gid_str, payload in guilds_snapshot.items():
        try:
            gid = int(gid_str)
        except (TypeError, ValueError):
            continue
        _persist_snapshot({"guild_id": gid, "payload": _normalize_guild_state(payload)})
