# paragon/xp.py
from __future__ import annotations
from typing import Optional, Tuple
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
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
    PRESTIGE_LINEAR_MINUTES_PER_LEVEL,
    PRESTIGE_LINEAR_START_LEVEL,
    PRESTIGE_LINEAR_START_MINUTES,
    PRESTIGE_MAX_BASE_PROGRESS_MINUTES,
    PRESTIGE_RATE_K,
    PRESTIGE_STACK_SOFTCAP,
)
from .guild_state import effective_unix_ts
from .source_keys import canonical_boost_source
from .spin_support import consume_mulligan_charge
from .storage import _udict, save_data
from .stats_store import record_xp_boost, record_xp_change


def _now_ts(guild_id: Optional[int] = None) -> int:
    if guild_id is None:
        return int(time.time())
    return effective_unix_ts(guild_id)


def get_xp_balance(u: dict) -> int:
    """Return the canonical arbitrary-precision XP balance."""
    try:
        return max(0, int(u.get("xp", u.get("xp_f", 0))))
    except (TypeError, ValueError, OverflowError):
        return 0


def _xp_remainder(u: dict) -> Decimal:
    try:
        value = Decimal(str(u.get("xp_remainder", "0")))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return value if value.is_finite() and Decimal(0) <= value < Decimal(1) else Decimal(0)


def set_xp_balance(u: dict, amount: int, *, remainder: Decimal = Decimal(0)) -> None:
    balance = max(0, int(amount))
    u["xp"] = balance
    # Compatibility mirror: deliberately an int, despite the historical name.
    u["xp_f"] = balance
    u["xp_remainder"] = format(remainder, "f") if remainder else "0"
    u["level"] = 1


def apply_xp_delta_to_user(u: dict, delta_xp: int | float | Decimal) -> int | float:
    """Apply a delta exactly for integers and with a persisted fractional remainder."""
    old_balance = get_xp_balance(u)
    old_remainder = _xp_remainder(u)
    if isinstance(delta_xp, int) and not isinstance(delta_xp, bool) and not old_remainder:
        new_balance = max(0, old_balance + delta_xp)
        set_xp_balance(u, new_balance)
        return new_balance - old_balance
    try:
        delta = Decimal(delta_xp) if isinstance(delta_xp, int) else Decimal(str(delta_xp))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("XP delta must be a finite number")
    if not delta.is_finite():
        raise ValueError("XP delta must be a finite number")

    with localcontext() as ctx:
        ctx.prec = max(50, len(str(old_balance)) + 50, len(str(delta).replace("-", "")) + 50)
        total = Decimal(old_balance) + old_remainder + delta
        if total <= 0:
            new_balance = 0
            new_remainder = Decimal(0)
        else:
            new_balance = int(total.to_integral_value(rounding=ROUND_FLOOR))
            new_remainder = total - Decimal(new_balance)
        set_xp_balance(u, new_balance, remainder=new_remainder)
        applied = (Decimal(new_balance) + new_remainder) - (Decimal(old_balance) + old_remainder)
    if applied == applied.to_integral_value():
        return int(applied)
    return float(applied)


def prestige_base_rate(prestige_level: int) -> float:
    p = max(0, int(prestige_level))
    step_bonus = float((p // PRESTIGE_BASE_STEP_LEVELS) * PRESTIGE_BASE_STEP_XP_PER_MIN)
    return max(0.0, float(BASE_XP_PER_MINUTE) + step_bonus)


def prestige_multiplier(prestige_level: int) -> float:
    p = max(0, int(prestige_level))
    return 1.0 + (PRESTIGE_RATE_K * p)


def prestige_permanent_rate(prestige_level: int) -> float:
    p = max(0, int(prestige_level))
    return prestige_base_rate(p) * prestige_multiplier(p)


def prestige_legacy_cost(prestige_level: int) -> int:
    p = max(0, int(prestige_level))
    val = PRESTIGE_COST_C0 * (1.0 + (PRESTIGE_COST_A * p) + (PRESTIGE_COST_B * (p ** 2)))
    base_cap = prestige_base_rate(p) * float(PRESTIGE_MAX_BASE_PROGRESS_MINUTES)
    return max(1, min(int(round(val)), int(max(1.0, math.floor(base_cap)))))


def prestige_target_minutes(prestige_level: int) -> float:
    """
    Target unboosted time to earn a prestige.
    The legacy curve is preserved through `PRESTIGE_LINEAR_START_LEVEL`, then a
    linear time ramp takes over using permanent passive rate only.
    """
    p = max(0, int(prestige_level))
    if p < max(0, int(PRESTIGE_LINEAR_START_LEVEL)):
        legacy_rate = max(0.01, prestige_permanent_rate(p))
        return max(1.0, prestige_legacy_cost(p) / legacy_rate)

    extra_levels = max(0, p - int(PRESTIGE_LINEAR_START_LEVEL))
    start_minutes = max(1.0, float(PRESTIGE_LINEAR_START_MINUTES))
    per_level = max(0.0, float(PRESTIGE_LINEAR_MINUTES_PER_LEVEL))
    return max(1.0, start_minutes + (per_level * float(extra_levels)))


def prestige_cost(prestige_level: int) -> int:
    p = max(0, int(prestige_level))
    if p < max(0, int(PRESTIGE_LINEAR_START_LEVEL)):
        return prestige_legacy_cost(p)

    # Decimal avoids both precision loss and float overflow for very large
    # prestige values.
    with localcontext() as ctx:
        ctx.prec = max(50, len(str(p)) * 4 + 30)
        pd = Decimal(p)
        base = Decimal(str(BASE_XP_PER_MINUTE)) + (
            Decimal(p // PRESTIGE_BASE_STEP_LEVELS) * Decimal(str(PRESTIGE_BASE_STEP_XP_PER_MIN))
        )
        mult = Decimal(1) + Decimal(str(PRESTIGE_RATE_K)) * pd
        extra = max(0, p - int(PRESTIGE_LINEAR_START_LEVEL))
        target = Decimal(str(PRESTIGE_LINEAR_START_MINUTES)) + (
            Decimal(str(PRESTIGE_LINEAR_MINUTES_PER_LEVEL)) * Decimal(extra)
        )
        return max(1, int((base * mult * target).to_integral_value(rounding=ROUND_HALF_EVEN)))


def _sum_powers(count: int, power: int) -> int:
    """sum(p**power for p in range(count)), for powers used by the curve."""
    n = max(0, int(count))
    if power == 0:
        return n
    if power == 1:
        return n * (n - 1) // 2
    if power == 2:
        return n * (n - 1) * (2 * n - 1) // 6
    if power == 3:
        t = n * (n - 1) // 2
        return t * t
    raise ValueError("unsupported power")


def _sum_floor_block_power(count: int, block_size: int, power: int) -> int:
    """sum(floor(p/block_size) * p**power for p in range(count))."""
    n = max(0, int(count))
    s = max(1, int(block_size))
    blocks, rem = divmod(n, s)
    sum_q1 = _sum_powers(blocks, 1)
    if power == 0:
        full = s * sum_q1
    elif power == 1:
        r1 = s * (s - 1) // 2
        full = s * s * _sum_powers(blocks, 2) + r1 * sum_q1
    elif power == 2:
        r1 = s * (s - 1) // 2
        r2 = s * (s - 1) * (2 * s - 1) // 6
        full = (
            s**3 * _sum_powers(blocks, 3)
            + 2 * s * r1 * _sum_powers(blocks, 2)
            + r2 * sum_q1
        )
    else:
        raise ValueError("unsupported power")

    start = blocks * s
    tail_power = _sum_powers(start + rem, power) - _sum_powers(start, power)
    return full + blocks * tail_power


def prestige_cumulative_cost(prestige_level: int) -> int:
    """Cumulative cost to reach a prestige level in constant time."""
    end = max(0, int(prestige_level))
    start = max(0, int(PRESTIGE_LINEAR_START_LEVEL))
    legacy_end = min(end, start)
    legacy = sum(prestige_legacy_cost(p) for p in range(legacy_end))
    if end <= start:
        return legacy

    # For p >= start, rate*target is a quadratic multiplied by a stepped
    # linear base.  Sum that polynomial and floor-block term analytically.
    with localcontext() as ctx:
        ctx.prec = max(50, len(str(end)) * 5 + 30)
        k = Decimal(str(PRESTIGE_RATE_K))
        slope = Decimal(str(PRESTIGE_LINEAR_MINUTES_PER_LEVEL))
        intercept = Decimal(str(PRESTIGE_LINEAR_START_MINUTES)) - slope * Decimal(start)
        q0 = intercept
        q1 = slope + k * intercept
        q2 = k * slope
        base0 = Decimal(str(BASE_XP_PER_MINUTE))
        base_step = Decimal(str(PRESTIGE_BASE_STEP_XP_PER_MIN))
        block = max(1, int(PRESTIGE_BASE_STEP_LEVELS))

        def range_sum(fn, power: int) -> int:
            return fn(end, block, power) - fn(start, block, power)

        plain = [
            _sum_powers(end, power) - _sum_powers(start, power)
            for power in range(3)
        ]
        stepped = [range_sum(_sum_floor_block_power, power) for power in range(3)]
        raw = sum(
            coeff * (base0 * Decimal(ps) + base_step * Decimal(fs))
            for coeff, ps, fs in zip((q0, q1, q2), plain, stepped)
        )
        return legacy + max(0, int(raw.to_integral_value(rounding=ROUND_HALF_EVEN)))


def prestige_bulk_cost(prestige_level: int, count: int) -> int:
    p = max(0, int(prestige_level))
    n = max(0, int(count))
    return prestige_cumulative_cost(p + n) - prestige_cumulative_cost(p)


def prestige_passive_rate(prestige_level: int, *, boost_multiplier: float = 1.0) -> float:
    p = max(0, int(prestige_level))
    return prestige_permanent_rate(p) * max(0.0, float(boost_multiplier))


def prestige_state_from_spent_xp(spent_xp: int | float) -> tuple[int, int, int]:
    if isinstance(spent_xp, int) and not isinstance(spent_xp, bool):
        available = max(0, spent_xp)
    else:
        try:
            available = max(0, int(Decimal(str(spent_xp)).to_integral_value(rounding=ROUND_HALF_EVEN)))
        except (InvalidOperation, ValueError, OverflowError):
            available = 0
    low, high = 0, 1
    while prestige_cumulative_cost(high) <= available:
        low, high = high, high * 2
    while low + 1 < high:
        mid = (low + high) // 2
        if prestige_cumulative_cost(mid) <= available:
            low = mid
        else:
            high = mid
    spent_used = prestige_cumulative_cost(low)
    return low, spent_used, available - spent_used


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
        source = canonical_boost_source(b.get("source", "activity"), default="activity")
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
        source = canonical_boost_source(b.get("source", "activity"), default="activity")
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
    stacks: int = 1,
) -> dict:
    """
    Grant an explicit temporary XP/min boost.
    - pct is decimal form (0.25 = +25%)
    - minutes is duration in whole minutes
    """
    u = _udict(member.guild.id, member.id)
    now = _now_ts(member.guild.id)
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    boosts = _coerce_boosts(u)
    stack_count = max(1, int(stacks))
    pct_each = max(0.0, float(pct))
    pct = pct_each * stack_count
    minutes = max(1, int(minutes))
    until = now + (minutes * 60)
    source_key = canonical_boost_source(source, default="activity")
    boosts.append({
        "pct": float(pct),
        "until": int(until),
        "source": source_key,
    })
    u["xp_boosts"] = boosts
    record_xp_boost(
        member.guild.id,
        member.id,
        source=source_key,
        reward_seed_xp=float(reward_seed_xp),
        pct=float(pct_each),
        minutes=int(minutes),
        count=stack_count,
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
        "source": source_key,
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


def _blocked_debuff_result(member, *, source: str, now: int, changed: bool) -> dict:
    u = _udict(member.guild.id, member.id)
    prestige = int(u.get("prestige", 0))
    rate_per_min = prestige_passive_rate(prestige, boost_multiplier=_actual_boost_multiplier(u, now=now))
    source_key = canonical_boost_source(source, default="activity")
    return {
        "pct": 0.0,
        "percent": 0.0,
        "minutes": 0,
        "until": int(now),
        "rate_per_min": float(rate_per_min),
        "source": source_key,
        "pruned": bool(changed),
        "blocked": True,
        "blocked_reason": "mulligan",
    }


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
    now = _now_ts(member.guild.id)
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
    now = _now_ts(member.guild.id)
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    if consume_mulligan_charge(member.guild.id, member.id):
        if persist:
            await save_data()
        return _blocked_debuff_result(member, source=source, now=now, changed=changed)
    debuffs = _coerce_debuffs(u)
    pct = max(0.0, min(1.0, float(pct)))
    minutes = max(1, int(minutes))
    until = now + (minutes * 60)
    source_key = canonical_boost_source(source, default="activity")
    debuffs.append({
        "pct": float(pct),
        "until": int(until),
        "source": source_key,
    })
    u["xp_debuffs"] = debuffs
    record_xp_boost(
        member.guild.id,
        member.id,
        source=source_key,
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
        "source": source_key,
        "pruned": bool(changed),
        "blocked": False,
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
    now = _now_ts(member.guild.id)
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    if consume_mulligan_charge(member.guild.id, member.id):
        if persist:
            await save_data()
        result = _blocked_debuff_result(member, source=source, now=now, changed=changed)
        result["stacked_pct"] = 0.0
        result["stacked_minutes"] = 0
        return result
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
    now = _now_ts(member.guild.id)
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
    try:
        total = max(0, int(total_xp))
    except (TypeError, ValueError, OverflowError):
        total = 0
    return 1, total, 0


async def apply_xp_change(
    member,
    delta_xp: int | float,
    *,
    source: str = "unspecified",
    persist: bool = True,
) -> Optional[Tuple[int, int]]:
    """
    Directly add/subtract from total XP (currency-style), clamp at 0.
    Level logic is retired; this always returns None.
    """
    u = _udict(member.guild.id, member.id)
    applied_delta = apply_xp_delta_to_user(u, delta_xp)
    if applied_delta != 0:
        record_xp_change(member.guild.id, member.id, applied_delta, source=source)
    if persist:
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
    now = _now_ts(member.guild.id)
    changed = _prune_expired_boosts(u, now=now)
    changed = _prune_expired_debuffs(u, now=now) or changed
    prestige = int(u.get("prestige", 0))
    gain_per_min = prestige_passive_rate(prestige, boost_multiplier=_actual_boost_multiplier(u, now=now))
    delta = Decimal(int(minutes)) * Decimal(str(gain_per_min))
    applied_delta = apply_xp_delta_to_user(u, delta)
    u["total_active_minutes"] = int(u.get("total_active_minutes", 0)) + int(minutes)
    if inactive_minutes > 0:
        u["total_inactive_minutes"] = int(u.get("total_inactive_minutes", 0)) + int(inactive_minutes)
    if applied_delta != 0 or minutes > 0:
        record_xp_change(
            member.guild.id,
            member.id,
            applied_delta,
            source=source,
            passive_minutes=minutes,
        )

    if changed or minutes > 0:
        await save_data()
    return None
