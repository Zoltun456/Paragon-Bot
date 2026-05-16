from __future__ import annotations

import asyncio
import pickle
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import GUILD_DB_DIR
from .source_keys import migrate_user_boost_sources

SAVE_LOCK = asyncio.Lock()
data: Dict[str, Any] = {"guilds": {}}

DATABASE_VERSION_KEY = "database_version"

_DATABASE_SCHEMA_VERSION = 2
_DATABASE_SCHEMA_VERSION_KEY = "database_schema_version"
_DATABASE_SHARED_KEY = "database_shared"
_DATABASE_STATE_KEY = "database_state"
_DATABASE_VERSIONS_KEY = "versions"
_DATABASE_NEXT_ID_KEY = "next_id"
_DATABASE_RUNTIME_KEY = "__database_runtime"
_GUILD_OWNER_KEY = "guild_owner_user_id"

_SHARED_TOP_LEVEL_KEYS = {
    "settings",
    "channels",
    _GUILD_OWNER_KEY,
}
_SHARED_STATE_FIELDS: dict[str, tuple[str, ...]] = {
    "blackjack": ("channel_id", "reset_hour", "reset_minute", "cooldown_enabled"),
    "lotto": ("enabled", "draw_hour", "draw_minute"),
    "spin_wheel": ("reset_hour", "reset_minute", "reward_overrides"),
}


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


def _base_guild_state() -> Dict[str, Any]:
    return {
        "users": {},
        "settings": {
            "inactive_loss_enabled": True,
            "bot_enabled": True,
            "bot_disabled_at": "",
            "bot_paused_seconds": 0.0,
            "bot_channel_snapshots": {},
        },
        "channels": {},
    }


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _normalize_state(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return _default_state()
    guilds = payload.get("guilds")
    if not isinstance(guilds, dict):
        payload["guilds"] = {}
    return payload


def _normalize_guild_state(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return _base_guild_state()
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
    if "bot_enabled" not in settings:
        settings["bot_enabled"] = True
    if "bot_disabled_at" not in settings:
        settings["bot_disabled_at"] = ""
    if "bot_paused_seconds" not in settings:
        settings["bot_paused_seconds"] = 0.0
    if not isinstance(settings.get("bot_channel_snapshots"), dict):
        settings["bot_channel_snapshots"] = {}
    return payload


def _migrate_guild_state(payload: Dict[str, Any]) -> bool:
    users = payload.get("users")
    if not isinstance(users, dict):
        return False

    changed = False
    for user in users.values():
        changed = migrate_user_boost_sources(user) or changed
    return changed


def _snapshot_from_active_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = deepcopy(payload)
    snapshot.pop(_DATABASE_RUNTIME_KEY, None)
    snapshot.pop(DATABASE_VERSION_KEY, None)
    snapshot.pop(_DATABASE_SCHEMA_VERSION_KEY, None)
    snapshot.pop(_DATABASE_SHARED_KEY, None)
    snapshot.pop(_DATABASE_STATE_KEY, None)
    return snapshot


def _extract_shared_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in _SHARED_TOP_LEVEL_KEYS:
        if key in payload:
            out[key] = deepcopy(payload[key])

    for state_key, fields in _SHARED_STATE_FIELDS.items():
        raw_state = payload.get(state_key)
        if not isinstance(raw_state, dict):
            continue
        subset = {
            field: deepcopy(raw_state[field])
            for field in fields
            if field in raw_state
        }
        if subset:
            out[state_key] = subset
    return out


def _apply_shared_payload(payload: Dict[str, Any], shared: Dict[str, Any]) -> Dict[str, Any]:
    _normalize_guild_state(payload)

    settings = shared.get("settings")
    if isinstance(settings, dict):
        payload["settings"] = deepcopy(settings)

    channels = shared.get("channels")
    if isinstance(channels, dict):
        payload["channels"] = deepcopy(channels)

    if _GUILD_OWNER_KEY in shared:
        payload[_GUILD_OWNER_KEY] = _coerce_int(shared.get(_GUILD_OWNER_KEY), 0)

    for state_key, fields in _SHARED_STATE_FIELDS.items():
        subset = shared.get(state_key)
        if not isinstance(subset, dict):
            continue
        state = payload.get(state_key)
        if not isinstance(state, dict):
            state = {}
            payload[state_key] = state
        for field in fields:
            if field in subset:
                state[field] = deepcopy(subset[field])
    return payload


def _default_persisted_guild_state() -> Dict[str, Any]:
    flat = _normalize_guild_state(_base_guild_state())
    shared = _extract_shared_payload(flat)
    return {
        _DATABASE_SCHEMA_VERSION_KEY: _DATABASE_SCHEMA_VERSION,
        DATABASE_VERSION_KEY: 1,
        _DATABASE_SHARED_KEY: shared,
        _DATABASE_STATE_KEY: {
            _DATABASE_NEXT_ID_KEY: 2,
            _DATABASE_VERSIONS_KEY: {
                "1": _snapshot_from_active_payload(flat),
            },
        },
    }


def _is_database_wrapper(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get(_DATABASE_STATE_KEY), dict)
        and isinstance(payload.get(_DATABASE_SHARED_KEY), dict)
    )


def _normalize_database_wrapper(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}

    state = payload.get(_DATABASE_STATE_KEY)
    if not isinstance(state, dict):
        state = {}

    versions_raw = state.get(_DATABASE_VERSIONS_KEY)
    if not isinstance(versions_raw, dict):
        versions_raw = {}

    versions: Dict[str, Dict[str, Any]] = {}
    ids: list[int] = []
    for raw_id, raw_snapshot in versions_raw.items():
        version_id = _coerce_int(raw_id, 0)
        if version_id <= 0:
            continue
        snapshot = _normalize_guild_state(
            deepcopy(raw_snapshot) if isinstance(raw_snapshot, dict) else _base_guild_state()
        )
        _migrate_guild_state(snapshot)
        versions[str(version_id)] = _snapshot_from_active_payload(snapshot)
        ids.append(version_id)

    if not versions:
        default_flat = _normalize_guild_state(_base_guild_state())
        versions["1"] = _snapshot_from_active_payload(default_flat)
        ids = [1]

    requested_active = _coerce_int(payload.get(DATABASE_VERSION_KEY), 0)
    active_id = requested_active if str(requested_active) in versions else min(ids)

    shared_raw = payload.get(_DATABASE_SHARED_KEY)
    shared_base = _normalize_guild_state(_base_guild_state())
    if isinstance(shared_raw, dict):
        _apply_shared_payload(shared_base, shared_raw)
    shared = _extract_shared_payload(shared_base)

    max_id = max(ids)
    next_id = max(_coerce_int(state.get(_DATABASE_NEXT_ID_KEY), 0), max_id + 1)

    return {
        _DATABASE_SCHEMA_VERSION_KEY: _DATABASE_SCHEMA_VERSION,
        DATABASE_VERSION_KEY: int(active_id),
        _DATABASE_SHARED_KEY: shared,
        _DATABASE_STATE_KEY: {
            _DATABASE_NEXT_ID_KEY: int(next_id),
            _DATABASE_VERSIONS_KEY: versions,
        },
    }


def _activate_database_wrapper(payload: Any) -> Dict[str, Any]:
    wrapper = _normalize_database_wrapper(payload)
    active_id = _coerce_int(wrapper.get(DATABASE_VERSION_KEY), 1)
    shared = deepcopy(wrapper.get(_DATABASE_SHARED_KEY, {}))
    state = wrapper.get(_DATABASE_STATE_KEY, {})
    versions = deepcopy(state.get(_DATABASE_VERSIONS_KEY, {}))
    next_id = _coerce_int(state.get(_DATABASE_NEXT_ID_KEY), active_id + 1)

    snapshot = versions.get(str(active_id))
    flat = _normalize_guild_state(
        deepcopy(snapshot) if isinstance(snapshot, dict) else _base_guild_state()
    )
    _apply_shared_payload(flat, shared)
    _migrate_guild_state(flat)
    flat[DATABASE_VERSION_KEY] = int(active_id)
    flat[_DATABASE_RUNTIME_KEY] = {
        _DATABASE_NEXT_ID_KEY: int(next_id),
        _DATABASE_VERSIONS_KEY: versions,
        _DATABASE_SHARED_KEY: shared,
    }
    return flat


def _serialize_active_guild_state(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return _default_persisted_guild_state()

    runtime = payload.get(_DATABASE_RUNTIME_KEY)
    if not isinstance(runtime, dict):
        flat = _normalize_guild_state(deepcopy(payload))
        _migrate_guild_state(flat)
        shared = _extract_shared_payload(flat)
        active_id = max(1, _coerce_int(flat.get(DATABASE_VERSION_KEY), 1))
        return _normalize_database_wrapper(
            {
                _DATABASE_SCHEMA_VERSION_KEY: _DATABASE_SCHEMA_VERSION,
                DATABASE_VERSION_KEY: active_id,
                _DATABASE_SHARED_KEY: shared,
                _DATABASE_STATE_KEY: {
                    _DATABASE_NEXT_ID_KEY: active_id + 1,
                    _DATABASE_VERSIONS_KEY: {
                        str(active_id): _snapshot_from_active_payload(flat),
                    },
                },
            }
        )

    active_id = max(1, _coerce_int(payload.get(DATABASE_VERSION_KEY), 1))
    versions_raw = runtime.get(_DATABASE_VERSIONS_KEY)
    if not isinstance(versions_raw, dict):
        versions_raw = {}
    versions: Dict[str, Dict[str, Any]] = {}
    for raw_id, raw_snapshot in versions_raw.items():
        version_id = _coerce_int(raw_id, 0)
        if version_id <= 0:
            continue
        snapshot = _normalize_guild_state(
            deepcopy(raw_snapshot) if isinstance(raw_snapshot, dict) else _base_guild_state()
        )
        _migrate_guild_state(snapshot)
        versions[str(version_id)] = _snapshot_from_active_payload(snapshot)
    versions[str(active_id)] = _snapshot_from_active_payload(
        _normalize_guild_state(deepcopy(payload))
    )

    shared = _extract_shared_payload(payload)
    next_id = max(
        _coerce_int(runtime.get(_DATABASE_NEXT_ID_KEY), 0),
        max((_coerce_int(raw_id, 0) for raw_id in versions.keys()), default=active_id) + 1,
    )

    return _normalize_database_wrapper(
        {
            _DATABASE_SCHEMA_VERSION_KEY: _DATABASE_SCHEMA_VERSION,
            DATABASE_VERSION_KEY: active_id,
            _DATABASE_SHARED_KEY: shared,
            _DATABASE_STATE_KEY: {
                _DATABASE_NEXT_ID_KEY: next_id,
                _DATABASE_VERSIONS_KEY: versions,
            },
        }
    )


def _runtime_database_state(g: Dict[str, Any]) -> Dict[str, Any]:
    runtime = g.get(_DATABASE_RUNTIME_KEY)
    if isinstance(runtime, dict):
        return runtime

    activated = _activate_database_wrapper(_serialize_active_guild_state(g))
    g.clear()
    g.update(activated)
    return g[_DATABASE_RUNTIME_KEY]


def _database_versions_for_guild(g: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    runtime = _runtime_database_state(g)
    versions = runtime.get(_DATABASE_VERSIONS_KEY)
    if not isinstance(versions, dict):
        versions = {}
        runtime[_DATABASE_VERSIONS_KEY] = versions
    return versions


def _replace_active_guild_version(g: Dict[str, Any], version_id: int) -> None:
    runtime = _runtime_database_state(g)
    shared = _extract_shared_payload(g)
    versions = _database_versions_for_guild(g)
    current_id = max(1, _coerce_int(g.get(DATABASE_VERSION_KEY), 1))
    versions[str(current_id)] = _snapshot_from_active_payload(g)

    snapshot = versions.get(str(version_id))
    if not isinstance(snapshot, dict):
        snapshot = _snapshot_from_active_payload(_normalize_guild_state(_base_guild_state()))
        versions[str(version_id)] = deepcopy(snapshot)

    next_id = max(
        _coerce_int(runtime.get(_DATABASE_NEXT_ID_KEY), 0),
        max((_coerce_int(raw_id, 0) for raw_id in versions.keys()), default=version_id) + 1,
    )

    activated = _activate_database_wrapper(
        {
            _DATABASE_SCHEMA_VERSION_KEY: _DATABASE_SCHEMA_VERSION,
            DATABASE_VERSION_KEY: int(version_id),
            _DATABASE_SHARED_KEY: shared,
            _DATABASE_STATE_KEY: {
                _DATABASE_NEXT_ID_KEY: next_id,
                _DATABASE_VERSIONS_KEY: versions,
            },
        }
    )
    g.clear()
    g.update(activated)


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
            "xp_debuffs": [],
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
    if "xp_debuffs" not in u or not isinstance(u.get("xp_debuffs"), list):
        u["xp_debuffs"] = []
    return u


def active_database_version(gid: int) -> int:
    g = _gdict(gid)
    return max(1, _coerce_int(g.get(DATABASE_VERSION_KEY), 1))


def next_database_version(gid: int) -> int:
    g = _gdict(gid)
    runtime = _runtime_database_state(g)
    return max(1, _coerce_int(runtime.get(_DATABASE_NEXT_ID_KEY), 1))


def available_database_versions(gid: int) -> list[int]:
    g = _gdict(gid)
    versions = _database_versions_for_guild(g)
    out = sorted({_coerce_int(raw_id, 0) for raw_id in versions.keys() if _coerce_int(raw_id, 0) > 0})
    active_id = active_database_version(gid)
    if active_id not in out:
        out.append(active_id)
        out.sort()
    return out


def has_database_version(gid: int, version_id: int) -> bool:
    target_id = max(1, _coerce_int(version_id, 0))
    return target_id in available_database_versions(gid)


def database_version_user_count(gid: int, version_id: int) -> int:
    target_id = max(1, _coerce_int(version_id, 0))
    g = _gdict(gid)
    if target_id == active_database_version(gid):
        users = g.get("users")
        return len(users) if isinstance(users, dict) else 0

    versions = _database_versions_for_guild(g)
    snapshot = versions.get(str(target_id))
    if not isinstance(snapshot, dict):
        return 0
    users = snapshot.get("users")
    return len(users) if isinstance(users, dict) else 0


async def create_database_version(gid: int) -> int:
    g = _gdict(gid)
    runtime = _runtime_database_state(g)
    versions = _database_versions_for_guild(g)
    new_id = max(1, _coerce_int(runtime.get(_DATABASE_NEXT_ID_KEY), 1))

    shared = _extract_shared_payload(g)
    fresh = _normalize_guild_state(_base_guild_state())
    _apply_shared_payload(fresh, shared)
    versions[str(new_id)] = _snapshot_from_active_payload(fresh)
    runtime[_DATABASE_NEXT_ID_KEY] = max(
        new_id + 1,
        max((_coerce_int(raw_id, 0) for raw_id in versions.keys()), default=new_id) + 1,
    )

    _replace_active_guild_version(g, new_id)
    await save_data()
    return new_id


async def set_active_database_version(gid: int, version_id: int) -> bool:
    target_id = max(1, _coerce_int(version_id, 0))
    g = _gdict(gid)
    versions = _database_versions_for_guild(g)
    if str(target_id) not in versions:
        return False
    if active_database_version(gid) == target_id:
        return True

    _replace_active_guild_version(g, target_id)
    await save_data()
    return True


def _load_guild_state(guild_id: int) -> Dict[str, Any]:
    path = _db_path_for_guild(guild_id)
    with _connect(path) as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT payload FROM app_state WHERE id = 1").fetchone()
        if row is None:
            persisted = _default_persisted_guild_state()
            _persist_snapshot_locked(conn, persisted)
            return _activate_database_wrapper(persisted)

        needs_persist = False
        try:
            loaded = pickle.loads(row["payload"])
        except Exception:
            loaded = _default_persisted_guild_state()
            needs_persist = True

        if _is_database_wrapper(loaded):
            persisted = _normalize_database_wrapper(loaded)
            needs_persist = needs_persist or persisted != loaded
        else:
            legacy = _normalize_guild_state(loaded)
            _migrate_guild_state(legacy)
            persisted = _normalize_database_wrapper(
                {
                    _DATABASE_SCHEMA_VERSION_KEY: _DATABASE_SCHEMA_VERSION,
                    DATABASE_VERSION_KEY: max(1, _coerce_int(legacy.get(DATABASE_VERSION_KEY), 1)),
                    _DATABASE_SHARED_KEY: _extract_shared_payload(legacy),
                    _DATABASE_STATE_KEY: {
                        _DATABASE_NEXT_ID_KEY: 2,
                        _DATABASE_VERSIONS_KEY: {
                            "1": _snapshot_from_active_payload(legacy),
                        },
                    },
                }
            )
            needs_persist = True

        if needs_persist:
            _persist_snapshot_locked(conn, persisted)
        return _activate_database_wrapper(persisted)


def _persist_all_guilds_snapshot(guilds_snapshot: Dict[str, Dict[str, Any]]) -> None:
    for gid_str, payload in guilds_snapshot.items():
        try:
            gid = int(gid_str)
        except (TypeError, ValueError):
            continue
        normalized = _serialize_active_guild_state(payload)
        _persist_snapshot({"guild_id": gid, "payload": normalized})
