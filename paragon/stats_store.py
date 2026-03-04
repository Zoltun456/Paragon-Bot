from __future__ import annotations

from typing import Any

from .storage import _gdict, _udict


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _inc_num(d: dict, key: str, amount: int | float) -> None:
    old = d.get(key, 0)
    if isinstance(old, bool):
        old = 0
    if isinstance(amount, int) and isinstance(old, int):
        d[key] = old + amount
        return
    d[key] = _as_float(old) + float(amount)


def ensure_user_stats(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    stats = _as_dict(u.get("stats"))
    u["stats"] = stats

    xp = _as_dict(stats.get("xp"))
    stats["xp"] = xp
    xp.setdefault("gained_total", 0.0)
    xp.setdefault("lost_total", 0.0)
    xp.setdefault("net_total", 0.0)
    xp.setdefault("event_count", 0)
    xp.setdefault("gain_events", 0)
    xp.setdefault("loss_events", 0)
    xp.setdefault("zero_events", 0)
    xp.setdefault("passive_minutes_total", 0)
    xp.setdefault("passive_ticks", 0)
    xp.setdefault("by_source", {})
    xp.setdefault("boosts_by_source", {})
    xp["by_source"] = _as_dict(xp.get("by_source"))
    xp["boosts_by_source"] = _as_dict(xp.get("boosts_by_source"))

    games = _as_dict(stats.get("games"))
    stats["games"] = games
    return stats


def ensure_game_stats(gid: int, uid: int, game: str) -> dict:
    stats = ensure_user_stats(gid, uid)
    games = _as_dict(stats.get("games"))
    stats["games"] = games
    g = _as_dict(games.get(game))
    games[game] = g
    return g


def record_game_event(gid: int, uid: int, game: str, field: str, amount: int | float = 1) -> None:
    g = ensure_game_stats(gid, uid, game)
    _inc_num(g, field, amount)


def record_game_fields(gid: int, uid: int, game: str, **deltas: int | float) -> None:
    g = ensure_game_stats(gid, uid, game)
    for field, amount in deltas.items():
        _inc_num(g, field, amount)


def record_xp_change(
    gid: int,
    uid: int,
    delta_xp: int | float,
    *,
    source: str = "unspecified",
    passive_minutes: int = 0,
) -> None:
    stats = ensure_user_stats(gid, uid)
    xp = stats["xp"]
    delta = float(delta_xp)
    src = (source or "unspecified").strip().lower()

    _inc_num(xp, "event_count", 1)
    _inc_num(xp, "net_total", delta)
    if delta > 0:
        _inc_num(xp, "gained_total", delta)
        _inc_num(xp, "gain_events", 1)
    elif delta < 0:
        _inc_num(xp, "lost_total", abs(delta))
        _inc_num(xp, "loss_events", 1)
    else:
        _inc_num(xp, "zero_events", 1)

    if passive_minutes > 0:
        _inc_num(xp, "passive_minutes_total", int(passive_minutes))
        _inc_num(xp, "passive_ticks", 1)

    by_source = _as_dict(xp.get("by_source"))
    xp["by_source"] = by_source
    row = _as_dict(by_source.get(src))
    by_source[src] = row
    _inc_num(row, "event_count", 1)
    _inc_num(row, "net_total", delta)
    if delta > 0:
        _inc_num(row, "gained_total", delta)
        _inc_num(row, "gain_events", 1)
    elif delta < 0:
        _inc_num(row, "lost_total", abs(delta))
        _inc_num(row, "loss_events", 1)
    else:
        _inc_num(row, "zero_events", 1)


def record_xp_boost(
    gid: int,
    uid: int,
    *,
    source: str,
    reward_seed_xp: int | float,
    pct: int | float,
    minutes: int,
) -> None:
    stats = ensure_user_stats(gid, uid)
    xp = stats["xp"]
    src = (source or "activity").strip().lower()

    boosts_by_source = _as_dict(xp.get("boosts_by_source"))
    xp["boosts_by_source"] = boosts_by_source
    row = _as_dict(boosts_by_source.get(src))
    boosts_by_source[src] = row

    _inc_num(row, "count", 1)
    _inc_num(row, "reward_seed_xp_total", float(reward_seed_xp))
    _inc_num(row, "percent_total", float(pct) * 100.0)
    _inc_num(row, "minutes_total", int(minutes))


def get_user_stats(gid: int, uid: int) -> dict:
    return ensure_user_stats(gid, uid)


def iter_guild_user_stats(gid: int) -> list[tuple[int, dict]]:
    g = _gdict(gid)
    users = _as_dict(g.get("users"))
    out: list[tuple[int, dict]] = []
    for uid_s, u in users.items():
        try:
            uid = int(uid_s)
        except Exception:
            continue
        stats = _as_dict(_as_dict(u).get("stats"))
        if stats:
            out.append((uid, stats))
    return out
