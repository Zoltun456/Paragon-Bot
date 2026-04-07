# paragon/xp.py
from __future__ import annotations
from typing import Optional, Tuple
import math
import time

from .config import (
    BASE_XP_PER_MINUTE,
    BOOST_VALUE_MAX_MINUTES,
    BOOST_VALUE_PCT_ROUND_STEP,
    BOOST_VALUE_PREFERRED_PCTS,
    PRESTIGE_BASE_STEP_LEVELS,
    PRESTIGE_BASE_STEP_XP_PER_MIN,
    PRESTIGE_COMPRESSION_MODE,
    PRESTIGE_COST_A,
    PRESTIGE_COST_B,
    PRESTIGE_COST_C0,
    PRESTIGE_MAX_BASE_PROGRESS_MINUTES,
    PRESTIGE_RATE_K,
    PRESTIGE_STACK_SOFTCAP,
)
from .storage import _udict, save_data
from .stats_store import record_xp_boost, record_xp_change


def _now_ts() -> int:
    return int(time.time())


def prestige_base_rate(prestige_level: int) -> float:
    p = max(0, int(prestige_level))
    step_bonus = float((p // PRESTIGE_BASE_STEP_LEVELS) * PRESTIGE_BASE_STEP_XP_PER_MIN)
    return max(0.0, float(BASE_XP_PER_MINUTE) + step_bonus)


def prestige_cost(prestige_level: int) -> int:
    p = max(0, int(prestige_level))
    val = PRESTIGE_COST_C0 * (1.0 + (PRESTIGE_COST_A * p) + (PRESTIGE_COST_B * (p ** 2)))
    base_cap = prestige_base_rate(p) * float(PRESTIGE_MAX_BASE_PROGRESS_MINUTES)
    return max(1, min(int(round(val)), int(max(1.0, math.floor(base_cap)))))


def prestige_multiplier(prestige_level: int) -> float:
    p = max(0, int(prestige_level))
    return 1.0 + (PRESTIGE_RATE_K * p)


def prestige_passive_rate(prestige_level: int, *, boost_multiplier: float = 1.0) -> float:
    p = max(0, int(prestige_level))
    return prestige_base_rate(p) * prestige_multiplier(p) * max(0.0, float(boost_multiplier))


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


def _coerce_debuffs(u: dict) -> list[dict]:
    raw = u.get("xp_debuffs")
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
    u["xp_debuffs"] = out
    return out


def _prune_expired_boosts(u: dict, *, now: Optional[int] = None) -> bool:
    now = _now_ts() if now is None else int(now)
    boosts = _coerce_boosts(u)
    kept = [b for b in boosts if int(b.get("until", 0)) > now]
    changed = len(kept) != len(boosts)
    if changed:
        u["xp_boosts"] = kept
    return changed


def _prune_expired_debuffs(u: dict, *, now: Optional[int] = None) -> bool:
    now = _now_ts() if now is None else int(now)
    debuffs = _coerce_debuffs(u)
    kept = [b for b in debuffs if int(b.get("until", 0)) > now]
    changed = len(kept) != len(debuffs)
    if changed:
        u["xp_debuffs"] = kept
    return changed


def _raw_boost_multiplier(u: dict, *, now: Optional[int] = None) -> float:
    now = _now_ts() if now is None else int(now)
    boosts = _coerce_boosts(u)
    total_pct = 0.0
    for b in boosts:
        if int(b.get("until", 0)) > now:
            total_pct += max(0.0, float(b.get("pct", 0.0)))
    return 1.0 + total_pct


def _debuff_multiplier(u: dict, *, now: Optional[int] = None) -> float:
    now = _now_ts() if now is None else int(now)
    debuffs = _coerce_debuffs(u)
    mult = 1.0
    for b in debuffs:
        if int(b.get("until", 0)) <= now:
            continue
        pct = max(0.0, min(1.0, float(b.get("pct", 0.0))))
        mult *= max(0.05, 1.0 - pct)
    return max(0.05, mult)


def _actual_boost_multiplier(u: dict, *, now: Optional[int] = None) -> float:
    raw = _raw_boost_multiplier(u, now=now)
    debuff_mult = _debuff_multiplier(u, now=now)
    if PRESTIGE_COMPRESSION_MODE == "global":
        return compress_stack_multiplier(raw) * debuff_mult
    return raw * debuff_mult


def _progress_boost_multiplier(u: dict, *, now: Optional[int] = None) -> float:
    raw = _raw_boost_multiplier(u, now=now)
    debuff_mult = _debuff_multiplier(u, now=now)
    if PRESTIGE_COMPRESSION_MODE in ("global", "progress_only"):
        return compress_stack_multiplier(raw) * debuff_mult
    return raw * debuff_mult


def bonus_xp_boost_profile(
    rate_per_min: int | float,
    bonus_xp: int | float,
    *,
    preferred_pcts: tuple[float, ...] = BOOST_VALUE_PREFERRED_PCTS,
    max_minutes: int = BOOST_VALUE_MAX_MINUTES,
    pct_round_step: float = BOOST_VALUE_PCT_ROUND_STEP,
) -> dict:
    """
    Convert a target amount of bonus XP into a clean fixed boost profile.
    The returned profile is based on the user's passive rate before this boost.
    """
    rate = max(0.01, float(rate_per_min))
    target_bonus_xp = max(1.0, float(bonus_xp))
    max_mins = max(1, int(max_minutes))

    for raw_pct in preferred_pcts:
        pct = max(0.01, float(raw_pct))
        minutes = max(1, int(math.ceil(target_bonus_xp / (rate * pct))))
        if minutes <= max_mins:
            return {
                "pct": float(pct),
                "minutes": int(minutes),
                "rate_basis_per_min": float(rate),
                "target_bonus_xp": float(target_bonus_xp),
                "equivalent_bonus_xp": float(rate * pct * minutes),
            }

    pct = max(0.01, target_bonus_xp / (rate * max_mins))
    if pct_round_step > 0.0:
        pct = math.ceil(pct / pct_round_step) * pct_round_step
    return {
        "pct": float(pct),
        "minutes": int(max_mins),
        "rate_basis_per_min": float(rate),
        "target_bonus_xp": float(target_bonus_xp),
        "equivalent_bonus_xp": float(rate * pct * max_mins),
    }


async def grant_bonus_xp_equivalent_boost(
    member,
    bonus_xp: int | float,
    *,
    source: str = "activity",
    reward_seed_xp: int | float | None = None,
) -> dict:
    """
    Grant a fixed boost worth approximately `bonus_xp` extra XP over its lifetime,
    measured against the user's passive rate before this reward boost.
    """
    u = _udict(member.guild.id, member.id)
    prestige = int(u.get("prestige", 0))
    profile = bonus_xp_boost_profile(prestige_passive_rate(prestige), bonus_xp)
    result = await grant_fixed_boost(
        member,
        pct=profile["pct"],
        minutes=profile["minutes"],
        source=source,
        reward_seed_xp=profile["target_bonus_xp"] if reward_seed_xp is None else reward_seed_xp,
    )
    result["rate_basis_per_min"] = float(profile["rate_basis_per_min"])
    result["target_bonus_xp"] = float(profile["target_bonus_xp"])
    result["equivalent_bonus_xp"] = float(profile["equivalent_bonus_xp"])
    return result


def prestige_reward_scale(
    prestige_level: int,
    *,
    min_scale: float = 0.25,
    curve: float = 25.0,
    power: float = 1.15,
) -> float:
    """
    Return a multiplier in [min_scale, 1.0] for prestige-dampened reward boosts.
    - Low prestige stays near 1.0
    - Higher prestige trends toward min_scale
    """
    p = max(0, int(prestige_level))
    floor = max(0.0, min(1.0, float(min_scale)))
    c = max(1e-9, float(curve))
    pw = max(0.1, float(power))
    decay = 1.0 / (1.0 + (float(p) / c) ** pw)
    return floor + ((1.0 - floor) * decay)


async def grant_fixed_boost(
    member,
    *,
    pct: int | float,
    minutes: int,
    source: str = "activity",
    reward_seed_xp: int | float = 0,
    persist: bool = True,
) -> dict:
    """
    Grant an explicit temporary XP/min boost.
    - pct is decimal form (0.25 = +25%)
    - minutes is duration in whole minutes
    """
    u = _udict(member.guild.id, member.id)
    now = _now_ts()
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    boosts = _coerce_boosts(u)
    pct = max(0.0, float(pct))
    minutes = max(1, int(minutes))
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
        reward_seed_xp=float(reward_seed_xp),
        pct=float(pct),
        minutes=int(minutes),
    )
    if persist:
        await save_data()

    prestige = int(u.get("prestige", 0))
    rate_per_min = prestige_passive_rate(prestige, boost_multiplier=_actual_boost_multiplier(u, now=now))
    return {
        "pct": float(pct),
        "percent": float(pct * 100.0),
        "minutes": int(minutes),
        "until": int(until),
        "rate_per_min": float(rate_per_min),
        "source": str(source).strip() or "activity",
        "pruned": bool(changed),
    }


def _pop_matching_effect(
    u: dict,
    *,
    effect_type: str,
    source_prefix: str,
    now: Optional[int] = None,
) -> tuple[float, int]:
    now = _now_ts() if now is None else int(now)
    prefix = str(source_prefix).strip().lower()
    if effect_type == "boost":
        rows = _coerce_boosts(u)
        key = "xp_boosts"
    else:
        rows = _coerce_debuffs(u)
        key = "xp_debuffs"

    kept = []
    total_pct = 0.0
    total_minutes = 0
    for row in rows:
        try:
            until = int(row.get("until", 0))
        except Exception:
            until = 0
        if until <= now:
            continue
        src = str(row.get("source", "")).strip().lower()
        if prefix and src.startswith(prefix):
            total_pct += max(0.0, float(row.get("pct", 0.0)))
            total_minutes += max(1, int(math.ceil((until - now) / 60.0)))
            continue
        kept.append(row)
    u[key] = kept
    return float(total_pct), int(total_minutes)


async def grant_stacked_fixed_boost(
    member,
    *,
    pct_add: int | float,
    minutes_add: int,
    pct_cap: int | float | None = None,
    minutes_cap: int | None = None,
    source: str = "activity",
    source_prefix: str | None = None,
    reward_seed_xp: int | float = 0,
    persist: bool = True,
) -> dict:
    u = _udict(member.guild.id, member.id)
    now = _now_ts()
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    existing_pct, existing_minutes = _pop_matching_effect(
        u,
        effect_type="boost",
        source_prefix=source_prefix or source,
        now=now,
    )
    pct = max(0.0, existing_pct + float(pct_add))
    minutes = max(1, existing_minutes + int(minutes_add))
    if pct_cap is not None:
        pct = min(float(pct_cap), pct)
    if minutes_cap is not None:
        minutes = min(int(minutes_cap), minutes)
    result = await grant_fixed_boost(
        member,
        pct=pct,
        minutes=minutes,
        source=source,
        reward_seed_xp=reward_seed_xp,
        persist=False,
    )
    result["stacked_pct"] = float(pct)
    result["stacked_minutes"] = int(minutes)
    result["pruned"] = bool(changed or result.get("pruned", False))
    if persist:
        await save_data()
    return result


async def grant_fixed_debuff(
    member,
    *,
    pct: int | float,
    minutes: int,
    source: str = "activity",
    reward_seed_xp: int | float = 0,
    persist: bool = True,
) -> dict:
    """
    Grant an explicit temporary XP/min debuff.
    - pct is decimal form (0.25 = -25%)
    - minutes is duration in whole minutes
    """
    u = _udict(member.guild.id, member.id)
    now = _now_ts()
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    debuffs = _coerce_debuffs(u)
    pct = max(0.0, min(1.0, float(pct)))
    minutes = max(1, int(minutes))
    until = now + (minutes * 60)
    debuffs.append({
        "pct": float(pct),
        "until": int(until),
        "source": str(source).strip() or "activity",
    })
    u["xp_debuffs"] = debuffs
    record_xp_boost(
        member.guild.id,
        member.id,
        source=str(source).strip() or "activity",
        reward_seed_xp=float(reward_seed_xp),
        pct=float(-pct),
        minutes=int(minutes),
    )
    if persist:
        await save_data()

    prestige = int(u.get("prestige", 0))
    rate_per_min = prestige_passive_rate(prestige, boost_multiplier=_actual_boost_multiplier(u, now=now))
    return {
        "pct": float(pct),
        "percent": float(pct * 100.0),
        "minutes": int(minutes),
        "until": int(until),
        "rate_per_min": float(rate_per_min),
        "source": str(source).strip() or "activity",
        "pruned": bool(changed),
    }


async def grant_stacked_fixed_debuff(
    member,
    *,
    pct_add: int | float,
    minutes_add: int,
    pct_cap: int | float | None = None,
    minutes_cap: int | None = None,
    source: str = "activity",
    source_prefix: str | None = None,
    reward_seed_xp: int | float = 0,
    persist: bool = True,
) -> dict:
    u = _udict(member.guild.id, member.id)
    now = _now_ts()
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    existing_pct, existing_minutes = _pop_matching_effect(
        u,
        effect_type="debuff",
        source_prefix=source_prefix or source,
        now=now,
    )
    pct = max(0.0, existing_pct + float(pct_add))
    minutes = max(1, existing_minutes + int(minutes_add))
    if pct_cap is not None:
        pct = min(float(pct_cap), pct)
    if minutes_cap is not None:
        minutes = min(int(minutes_cap), minutes)
    result = await grant_fixed_debuff(
        member,
        pct=pct,
        minutes=minutes,
        source=source,
        reward_seed_xp=reward_seed_xp,
        persist=False,
    )
    result["stacked_pct"] = float(pct)
    result["stacked_minutes"] = int(minutes)
    result["pruned"] = bool(changed or result.get("pruned", False))
    if persist:
        await save_data()
    return result


async def get_gain_state(member) -> dict:
    """
    Runtime state for rank/boost UI and prestige pacing display.
    """
    u = _udict(member.guild.id, member.id)
    now = _now_ts()
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    boosts = _coerce_boosts(u)
    debuffs = _coerce_debuffs(u)
    if changed:
        await save_data()

    total_xp = int(u.get("xp_f", u.get("xp", 0)))
    prestige = int(u.get("prestige", 0))
    base_rate = prestige_base_rate(prestige)
    p_mult = prestige_multiplier(prestige)
    b_raw = _raw_boost_multiplier(u, now=now)
    b_actual = _actual_boost_multiplier(u, now=now)
    b_progress = _progress_boost_multiplier(u, now=now)

    total_mult_actual = p_mult * b_actual
    total_mult_progress = p_mult * b_progress
    rate_per_min = prestige_passive_rate(prestige, boost_multiplier=b_actual)
    progress_rate_per_min = prestige_passive_rate(prestige, boost_multiplier=b_progress)

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

    debuff_rows = []
    for b in sorted(debuffs, key=lambda x: int(x.get("until", 0))):
        until = int(b.get("until", 0))
        if until <= now:
            continue
        left_min = max(1, math.ceil((until - now) / 60.0))
        pct = max(0.0, min(1.0, float(b.get("pct", 0.0))))
        debuff_rows.append({
            "source": str(b.get("source", "activity")),
            "minutes_left": int(left_min),
            "percent": float(pct * 100.0),
        })

    return {
        "base_per_min": float(base_rate),
        "multiplier": float(total_mult_actual),  # compatibility for !rank
        "rate_per_min": float(rate_per_min),
        "boosts": rows,
        "debuffs": debuff_rows,
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
      - Stepped base `BASE_XP_PER_MINUTE + floor(prestige / 5)`
      - Prestige multiplier `1 + k*p`
      - Stacked active temporary boosts (optionally compressed by mode)
    `inactive_minutes` is currently ignored (reserved for future mechanics).
    """
    if minutes < 0 or inactive_minutes < 0:
        raise ValueError("apply_delta expects non-negative minute counts")

    u = _udict(member.guild.id, member.id)
    now = _now_ts()
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    prestige = int(u.get("prestige", 0))
    gain_per_min = prestige_passive_rate(prestige, boost_multiplier=_actual_boost_multiplier(u, now=now))
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
