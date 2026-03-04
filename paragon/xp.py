# paragon/xp.py
from __future__ import annotations
from typing import Optional, Tuple
import math
import time

from .config import (
    BASE_XP_PER_MINUTE,
    XP_REWARD_BOOST_MIN_PCT,
    XP_REWARD_BOOST_MAX_PCT,
    XP_REWARD_BOOST_SCALE,
    XP_REWARD_BOOST_MIN_MINUTES,
    XP_REWARD_BOOST_MAX_MINUTES,
)
from .storage import _udict, save_data
from .stats_store import record_xp_boost, record_xp_change


# ---------------------------
# Prestige tuning (starter build)
# ---------------------------
# cost(p) = C0 * (1 + a*p + b*p^2), p >= 0
PRESTIGE_COST_C0 = 120.0
PRESTIGE_COST_A = 0.45
PRESTIGE_COST_B = 0.065

# prestige multiplier = 1 + k*p
PRESTIGE_RATE_K = 0.025

# Optional boost-stack compression mode:
# - "progress_only": only affects prestige ETA/progress estimates
# - "global": affects all XP gain
# - "off": disabled
PRESTIGE_COMPRESSION_MODE = "progress_only"
PRESTIGE_STACK_SOFTCAP = 6.0


def _now_ts() -> int:
    return int(time.time())


def prestige_cost(prestige_level: int) -> int:
    p = max(0, int(prestige_level))
    val = PRESTIGE_COST_C0 * (1.0 + (PRESTIGE_COST_A * p) + (PRESTIGE_COST_B * (p ** 2)))
    return max(1, int(round(val)))


def prestige_multiplier(prestige_level: int) -> float:
    p = max(0, int(prestige_level))
    return 1.0 + (PRESTIGE_RATE_K * p)


def compress_stack_multiplier(raw_multiplier: float, *, cap: float = PRESTIGE_STACK_SOFTCAP) -> float:
    """
    Compress only the bonus portion above x1.0:
      raw m = 1 + bonus_raw
      bonus_eff = cap * (1 - exp(-bonus_raw / cap))
      out = 1 + bonus_eff
    """
    m = max(1.0, float(raw_multiplier))
    if cap <= 0:
        return m
    bonus_raw = m - 1.0
    bonus_eff = cap * (1.0 - math.exp(-(bonus_raw / cap)))
    return 1.0 + bonus_eff


def _coerce_boosts(u: dict) -> list[dict]:
    raw = u.get("xp_boosts")
    if not isinstance(raw, list):
        raw = []
    out = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        try:
            pct = float(b.get("pct", 0.0))
            until = int(b.get("until", 0))
        except Exception:
            continue
        if pct <= 0.0 or until <= 0:
            continue
        source = str(b.get("source", "activity")).strip() or "activity"
        out.append({"pct": pct, "until": until, "source": source})
    u["xp_boosts"] = out
    return out


def _prune_expired_boosts(u: dict, *, now: Optional[int] = None) -> bool:
    now = _now_ts() if now is None else int(now)
    boosts = _coerce_boosts(u)
    kept = [b for b in boosts if int(b.get("until", 0)) > now]
    changed = len(kept) != len(boosts)
    if changed:
        u["xp_boosts"] = kept
    return changed


def _raw_boost_multiplier(u: dict, *, now: Optional[int] = None) -> float:
    now = _now_ts() if now is None else int(now)
    boosts = _coerce_boosts(u)
    total_pct = 0.0
    for b in boosts:
        if int(b.get("until", 0)) > now:
            total_pct += max(0.0, float(b.get("pct", 0.0)))
    return 1.0 + total_pct


def _actual_boost_multiplier(u: dict, *, now: Optional[int] = None) -> float:
    raw = _raw_boost_multiplier(u, now=now)
    if PRESTIGE_COMPRESSION_MODE == "global":
        return compress_stack_multiplier(raw)
    return raw


def _progress_boost_multiplier(u: dict, *, now: Optional[int] = None) -> float:
    raw = _raw_boost_multiplier(u, now=now)
    if PRESTIGE_COMPRESSION_MODE in ("global", "progress_only"):
        return compress_stack_multiplier(raw)
    return raw


def _reward_to_boost(reward_xp: int | float) -> tuple[float, int]:
    reward = max(1.0, float(reward_xp))
    scaled = math.log10(reward + 1.0)
    pct = XP_REWARD_BOOST_MIN_PCT + (scaled * XP_REWARD_BOOST_SCALE)
    pct = max(XP_REWARD_BOOST_MIN_PCT, min(XP_REWARD_BOOST_MAX_PCT, pct))

    minutes = int(round(XP_REWARD_BOOST_MIN_MINUTES + (scaled * 18.0) + math.sqrt(reward)))
    minutes = max(XP_REWARD_BOOST_MIN_MINUTES, min(XP_REWARD_BOOST_MAX_MINUTES, minutes))
    return pct, minutes


async def grant_reward_boost(member, reward_xp: int | float, *, source: str = "activity") -> dict:
    """
    Convert a reward into a temporary XP/min multiplier boost.
    Returns a summary dict for user-facing messaging.
    """
    u = _udict(member.guild.id, member.id)
    now = _now_ts()
    changed = _prune_expired_boosts(u, now=now)
    boosts = _coerce_boosts(u)

    pct, minutes = _reward_to_boost(reward_xp)
    until = now + (minutes * 60)
    boosts.append({
        "pct": float(pct),
        "until": int(until),
        "source": str(source).strip() or "activity",
    })
    u["xp_boosts"] = boosts
    record_xp_boost(
        member.guild.id,
        member.id,
        source=str(source).strip() or "activity",
        reward_seed_xp=float(reward_xp),
        pct=float(pct),
        minutes=int(minutes),
    )
    await save_data()

    prestige = int(u.get("prestige", 0))
    total_mult = prestige_multiplier(prestige) * _actual_boost_multiplier(u, now=now)
    rate_per_min = BASE_XP_PER_MINUTE * total_mult
    return {
        "pct": float(pct),
        "percent": float(pct * 100.0),
        "minutes": int(minutes),
        "until": int(until),
        "rate_per_min": float(rate_per_min),
        "source": str(source).strip() or "activity",
        "pruned": bool(changed),
    }


async def get_gain_state(member) -> dict:
    """
    Runtime state for rank/boost UI and prestige pacing display.
    """
    u = _udict(member.guild.id, member.id)
    now = _now_ts()
    changed = _prune_expired_boosts(u, now=now)
    boosts = _coerce_boosts(u)
    if changed:
        await save_data()

    total_xp = int(u.get("xp_f", u.get("xp", 0)))
    prestige = int(u.get("prestige", 0))
    p_mult = prestige_multiplier(prestige)
    b_raw = _raw_boost_multiplier(u, now=now)
    b_actual = _actual_boost_multiplier(u, now=now)
    b_progress = _progress_boost_multiplier(u, now=now)

    total_mult_actual = p_mult * b_actual
    total_mult_progress = p_mult * b_progress
    rate_per_min = BASE_XP_PER_MINUTE * total_mult_actual
    progress_rate_per_min = BASE_XP_PER_MINUTE * total_mult_progress

    cur_cost = prestige_cost(prestige)
    need = max(0, cur_cost - total_xp)
    eta_minutes = None
    if need > 0 and progress_rate_per_min > 0:
        eta_minutes = int(math.ceil(need / progress_rate_per_min))

    rows = []
    for b in sorted(boosts, key=lambda x: int(x.get("until", 0))):
        until = int(b.get("until", 0))
        if until <= now:
            continue
        left_min = max(1, math.ceil((until - now) / 60.0))
        pct = max(0.0, float(b.get("pct", 0.0)))
        rows.append({
            "source": str(b.get("source", "activity")),
            "minutes_left": int(left_min),
            "percent": float(pct * 100.0),
        })

    return {
        "base_per_min": float(BASE_XP_PER_MINUTE),
        "multiplier": float(total_mult_actual),  # compatibility for !rank
        "rate_per_min": float(rate_per_min),
        "boosts": rows,
        "prestige": int(prestige),
        "prestige_multiplier": float(p_mult),
        "boost_multiplier_raw": float(b_raw),
        "boost_multiplier_progress": float(b_progress),
        "prestige_progress_rate_per_min": float(progress_rate_per_min),
        "next_prestige_cost": int(cur_cost),
        "xp_to_next_prestige": int(need),
        "prestige_progress_eta_minutes": eta_minutes,
        "compression_mode": PRESTIGE_COMPRESSION_MODE,
    }


def _total_xp_to_reach_level(level: int) -> int:
    # Legacy helper kept for command compatibility; levels are retired.
    return 0


def _compute_level_from_total_xp(total_xp: float) -> int:
    # Legacy helper kept for compatibility with older admin flows.
    return 1


def level_progress(total_xp: float) -> tuple[int, int, int]:
    # Legacy shape: (level, xp_into_level, xp_needed_for_next)
    # We now expose total XP in the middle position.
    return 1, int(max(0.0, float(total_xp))), 0


async def apply_xp_change(
    member,
    delta_xp: int | float,
    *,
    source: str = "unspecified",
) -> Optional[Tuple[int, int]]:
    """
    Directly add/subtract from total XP (currency-style), clamp at 0.
    Level logic is retired; this always returns None.
    """
    u = _udict(member.guild.id, member.id)
    total_xp = float(u.get("xp_f", u.get("xp", 0)))
    delta = float(delta_xp)
    new_total = max(0.0, total_xp + delta)
    applied_delta = new_total - total_xp
    u["xp_f"] = float(new_total)
    u["xp"] = int(new_total)
    u["level"] = 1
    if applied_delta != 0.0:
        record_xp_change(member.guild.id, member.id, applied_delta, source=source)
    await save_data()
    return None


async def apply_delta(
    member,
    *,
    minutes: int = 0,
    inactive_minutes: int = 0,
    source: str = "passive voice",
) -> Optional[Tuple[int, int]]:
    """
    Passive XP gain:
      - Base `BASE_XP_PER_MINUTE`
      - Prestige multiplier `1 + k*p`
      - Stacked active temporary boosts (optionally compressed by mode)
    `inactive_minutes` is currently ignored (reserved for future mechanics).
    """
    if minutes < 0 or inactive_minutes < 0:
        raise ValueError("apply_delta expects non-negative minute counts")

    u = _udict(member.guild.id, member.id)
    now = _now_ts()
    changed = _prune_expired_boosts(u, now=now)
    prestige = int(u.get("prestige", 0))
    total_mult = prestige_multiplier(prestige) * _actual_boost_multiplier(u, now=now)
    gain_per_min = BASE_XP_PER_MINUTE * total_mult
    delta = float(minutes) * gain_per_min

    total_xp = float(u.get("xp_f", u.get("xp", 0)))
    new_total = max(0.0, total_xp + delta)
    u["xp_f"] = float(new_total)
    u["xp"] = int(new_total)
    u["level"] = 1
    u["total_active_minutes"] = int(u.get("total_active_minutes", 0)) + int(minutes)
    if inactive_minutes > 0:
        u["total_inactive_minutes"] = int(u.get("total_inactive_minutes", 0)) + int(inactive_minutes)
    if delta != 0.0 or minutes > 0:
        record_xp_change(
            member.guild.id,
            member.id,
            delta,
            source=source,
            passive_minutes=minutes,
        )

    if changed or minutes > 0:
        await save_data()
    return None
