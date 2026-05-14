from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import random
import time
from typing import Optional

import discord
from discord.ext import commands

from .config import (
    ROULETTE_BASE_SUCCESS_CHANCE,
    ROULETTE_COOLDOWN_SECONDS,
    ROULETTE_GAP_STEP_CHANCE,
    ROULETTE_MAX_SUCCESS_CHANCE,
    ROULETTE_MAX_TIMEOUT_SECONDS,
    ROULETTE_MIN_SUCCESS_CHANCE,
    ROULETTE_MIN_TIMEOUT_SECONDS,
)
from .guild_state import effective_date_key, effective_unix_ts
from .spin_support import (
    consume_roulette_accuracy_bonus,
    consume_roulette_backfire_shield,
    consume_roulette_timeout_bonus_seconds,
    get_roulette_timeout_bonus_seconds,
)
from .stats_store import record_game_fields
from .storage import _udict, save_data

ROULETTE_CENTER_TIMEOUT_SECONDS = 60
ROULETTE_TIMEOUT_UNTIL_KEY = "roulette_timeout_until_ts"
ROULETTE_PAUSED_TIMEOUT_SECONDS_KEY = "roulette_timeout_paused_seconds"


def _get_user_prestige(member: discord.Member) -> int:
    u = _udict(member.guild.id, member.id)
    return max(0, int(u.get("prestige", 0)))


def _roulette_daily_state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("roulette_daily")
    if not isinstance(st, dict):
        st = {}
        u["roulette_daily"] = st
    today = effective_date_key(gid)
    if str(st.get("date", "")) != today:
        st["date"] = today
        st["backfired"] = False
    else:
        st["backfired"] = bool(st.get("backfired", False))
    return st


def _roulette_success_chance(shooter_prestige: int, target_prestige: int) -> float:
    sp = max(0, int(shooter_prestige))
    tp = max(0, int(target_prestige))
    gap = sp - tp
    chance = ROULETTE_BASE_SUCCESS_CHANCE + (float(gap) * ROULETTE_GAP_STEP_CHANCE)
    return max(ROULETTE_MIN_SUCCESS_CHANCE, min(ROULETTE_MAX_SUCCESS_CHANCE, chance))


def _lerp(start: float, end: float, ratio: float) -> float:
    r = max(0.0, min(1.0, float(ratio)))
    return float(start) + (float(end) - float(start)) * r


def _timeout_seconds_for_chance(base_chance: float, *, success: bool) -> int:
    chance = max(ROULETTE_MIN_SUCCESS_CHANCE, min(ROULETTE_MAX_SUCCESS_CHANCE, float(base_chance)))
    center = max(ROULETTE_MIN_SUCCESS_CHANCE, min(ROULETTE_MAX_SUCCESS_CHANCE, ROULETTE_BASE_SUCCESS_CHANCE))
    center_timeout = float(ROULETTE_CENTER_TIMEOUT_SECONDS)

    if chance <= center:
        span = max(1e-9, center - ROULETTE_MIN_SUCCESS_CHANCE)
        ratio = (chance - ROULETTE_MIN_SUCCESS_CHANCE) / span
        if success:
            seconds = _lerp(ROULETTE_MAX_TIMEOUT_SECONDS, center_timeout, ratio)
        else:
            seconds = _lerp(ROULETTE_MIN_TIMEOUT_SECONDS, center_timeout, ratio)
    else:
        span = max(1e-9, ROULETTE_MAX_SUCCESS_CHANCE - center)
        ratio = (chance - center) / span
        if success:
            seconds = _lerp(center_timeout, ROULETTE_MIN_TIMEOUT_SECONDS, ratio)
        else:
            seconds = _lerp(center_timeout, ROULETTE_MAX_TIMEOUT_SECONDS, ratio)
    seconds = int(round(seconds))
    return max(ROULETTE_MIN_TIMEOUT_SECONDS, min(ROULETTE_MAX_TIMEOUT_SECONDS, seconds))


def _fmt_remaining(seconds: int) -> str:
    secs = max(0, int(seconds))
    mins, rem = divmod(secs, 60)
    if mins <= 0:
        return f"{rem}s"
    return f"{mins}m {rem:02d}s"


def _member_timeout_until(member: discord.Member) -> Optional[datetime]:
    until = getattr(member, "communication_disabled_until", None)
    if isinstance(until, datetime):
        return until
    until = getattr(member, "timed_out_until", None)
    if isinstance(until, datetime):
        return until
    return None


def _clear_timeout_state(gid: int, uid: int) -> bool:
    u = _udict(gid, uid)
    changed = False
    for key in (ROULETTE_TIMEOUT_UNTIL_KEY, ROULETTE_PAUSED_TIMEOUT_SECONDS_KEY):
        if key in u:
            u.pop(key, None)
            changed = True
    return changed


def _stored_timeout_until_ts(gid: int, uid: int) -> int:
    u = _udict(gid, uid)
    return max(0, int(float(u.get(ROULETTE_TIMEOUT_UNTIL_KEY, 0.0) or 0.0)))


def _stored_timeout_remaining_seconds(gid: int, uid: int) -> int:
    u = _udict(gid, uid)
    paused = max(0, int(float(u.get(ROULETTE_PAUSED_TIMEOUT_SECONDS_KEY, 0.0) or 0.0)))
    if paused > 0:
        return paused
    until_ts = _stored_timeout_until_ts(gid, uid)
    if until_ts <= 0:
        return 0
    remaining = max(0, until_ts - effective_unix_ts(gid))
    if remaining <= 0:
        _clear_timeout_state(gid, uid)
    return remaining


def _store_active_timeout(gid: int, uid: int, seconds: int) -> None:
    u = _udict(gid, uid)
    remaining = max(1, int(seconds))
    u[ROULETTE_TIMEOUT_UNTIL_KEY] = float(effective_unix_ts(gid) + remaining)
    u[ROULETTE_PAUSED_TIMEOUT_SECONDS_KEY] = 0


async def _clear_timeout_member(member: discord.Member, reason: str) -> bool:
    try:
        if hasattr(member, "timeout"):
            await member.timeout(None, reason=reason)
        else:
            await member.edit(communication_disabled_until=None, reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def _timeout_member(member: discord.Member, seconds: int, reason: str) -> bool:
    """
    Apply timeout. Returns True if applied, False if missing perms / HTTP error.
    Compatible with discord.py 2.0+.
    """
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    try:
        if hasattr(member, "timeout"):
            await member.timeout(until, reason=reason)
        else:
            await member.edit(communication_disabled_until=until, reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


class RouletteCog(commands.Cog):
    """
    !roulette @user
    - No XP cost
    - 30 minute personal cooldown after each use
    - Base success chance is 20%
    - Prestige gap changes that base by 2.5% per level, capped to 2.5%..60%
    - Same-prestige shots timeout for 60s either way; lower hit odds lengthen hits and shorten backfires, while higher hit odds do the reverse
    - Wheel aim bonus increases hit odds only and can push final odds above 60%
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def pause_guild(self, guild_id: int) -> None:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        changed = False
        now_utc = datetime.now(timezone.utc)
        for member in guild.members:
            if member.bot:
                continue
            stored_until_ts = _stored_timeout_until_ts(guild.id, member.id)
            if stored_until_ts <= 0:
                continue
            stored_remaining = max(0, stored_until_ts - effective_unix_ts(guild.id))
            if stored_remaining <= 0:
                changed = _clear_timeout_state(guild.id, member.id) or changed
                continue
            live_until = _member_timeout_until(member)
            if live_until is None:
                changed = _clear_timeout_state(guild.id, member.id) or changed
                continue
            live_remaining = max(0, int(math.ceil((live_until.astimezone(timezone.utc) - now_utc).total_seconds())))
            if abs(live_remaining - stored_remaining) > 5:
                continue
            if await _clear_timeout_member(member, "Roulette timeout paused while Paragon is disabled"):
                u = _udict(guild.id, member.id)
                u[ROULETTE_TIMEOUT_UNTIL_KEY] = 0
                u[ROULETTE_PAUSED_TIMEOUT_SECONDS_KEY] = stored_remaining
                changed = True
        if changed:
            await save_data()

    async def resume_guild(self, guild_id: int) -> None:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        changed = False
        for member in guild.members:
            if member.bot:
                continue
            remaining = _stored_timeout_remaining_seconds(guild.id, member.id)
            u = _udict(guild.id, member.id)
            paused = max(0, int(float(u.get(ROULETTE_PAUSED_TIMEOUT_SECONDS_KEY, 0.0) or 0.0)))
            if paused <= 0:
                continue
            if remaining <= 0:
                changed = _clear_timeout_state(guild.id, member.id) or changed
                continue
            if await _timeout_member(member, remaining, "Roulette timeout resumed after Paragon re-enabled"):
                _store_active_timeout(guild.id, member.id, remaining)
                changed = True
            else:
                changed = _clear_timeout_state(guild.id, member.id) or changed
        if changed:
            await save_data()

    @commands.command(name="roulette", aliases=["r"])
    async def roulette(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        author: discord.Member = ctx.author  # type: ignore

        if target is None:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}roulette @user`")
            return
        if target.id == author.id:
            await ctx.reply("You cannot roulette yourself.")
            return
        if target.bot:
            await ctx.reply("You cannot roulette a bot.")
            return
        if target.guild.id != ctx.guild.id:
            await ctx.reply("Target must be a member of this server.")
            return

        _stored_timeout_remaining_seconds(ctx.guild.id, author.id)
        _stored_timeout_remaining_seconds(ctx.guild.id, target.id)

        if not author.voice or not author.voice.channel:
            await ctx.reply("You must be in a voice channel to use roulette.")
            return
        if not target.voice or not target.voice.channel:
            await ctx.reply(f"{target.display_name} must be in a voice channel for roulette.")
            return

        user_state = _udict(ctx.guild.id, author.id)
        daily_state = _roulette_daily_state(ctx.guild.id, author.id)
        now_ts = float(effective_unix_ts(ctx.guild.id))
        next_ts = float(user_state.get("roulette_next_ts", 0.0) or 0.0)
        if now_ts < next_ts:
            remaining = int(math.ceil(next_ts - now_ts))
            await ctx.reply(
                f"Roulette is on cooldown for you. Try again in **{_fmt_remaining(remaining)}**."
            )
            return

        author_p = _get_user_prestige(author)
        target_p = _get_user_prestige(target)
        base_chance = _roulette_success_chance(author_p, target_p)
        chance = base_chance
        wheel_aim_bonus = float(consume_roulette_accuracy_bonus(ctx.guild.id, author.id))
        if wheel_aim_bonus > 0.0:
            chance = max(ROULETTE_MIN_SUCCESS_CHANCE, min(1.0, chance + wheel_aim_bonus))
        hit_timeout_seconds = _timeout_seconds_for_chance(base_chance, success=True)
        backfire_timeout_seconds = _timeout_seconds_for_chance(base_chance, success=False)
        base_chance_pct = base_chance * 100.0
        chance_pct = chance * 100.0

        # Consume cooldown on use, regardless of outcome.
        user_state["roulette_next_ts"] = float(now_ts + ROULETTE_COOLDOWN_SECONDS)
        await save_data()

        record_game_fields(
            ctx.guild.id,
            author.id,
            "roulette",
            plays=1,
            chance_pct_total=chance_pct,
        )

        success = random.random() < chance
        if success:
            timeout_bonus_seconds = get_roulette_timeout_bonus_seconds(ctx.guild.id, author.id)
            final_timeout_seconds = hit_timeout_seconds + timeout_bonus_seconds
            applied = await _timeout_member(
                target,
                final_timeout_seconds,
                f"Roulette by {author} (success, chance {chance_pct:.2f}%)",
            )
            success_fields: dict[str, int] = {"successes": 1}
            if wheel_aim_bonus > 0.0:
                success_fields["successes_with_aim_bonus"] = 1
            if bool(daily_state.get("backfired", False)):
                success_fields["successes_after_backfire"] = 1
            record_game_fields(ctx.guild.id, author.id, "roulette", **success_fields)
            if applied:
                _store_active_timeout(ctx.guild.id, target.id, final_timeout_seconds)
                if timeout_bonus_seconds > 0:
                    consume_roulette_timeout_bonus_seconds(ctx.guild.id, author.id)
                await save_data()
                record_game_fields(ctx.guild.id, target.id, "roulette", got_timed_out=1)
                wheel_line = (
                    f"Wheel aim bonus applied: **+{wheel_aim_bonus * 100.0:.1f}%**.\n"
                    if wheel_aim_bonus > 0.0
                    else ""
                )
                timeout_line = (
                    f"Wheel timeout extend applied: **+{timeout_bonus_seconds}s**.\n"
                    if timeout_bonus_seconds > 0
                    else ""
                )
                await ctx.reply(
                    f"Roulette: {author.mention} landed the shot.\n"
                    f"{target.mention} timed out for **{_fmt_remaining(final_timeout_seconds)}**.\n"
                    f"{wheel_line}"
                    f"{timeout_line}"
                    f"Base odds: **{base_chance_pct:.2f}%** | Final odds: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                    f"Cooldown: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
                )
            else:
                wheel_line = (
                    f"Wheel aim bonus applied: **+{wheel_aim_bonus * 100.0:.1f}%**.\n"
                    if wheel_aim_bonus > 0.0
                    else ""
                )
                await ctx.reply(
                    f"Roulette: {author.mention} rolled success, but I could not time out "
                    f"{target.mention} (permission/hierarchy).\n"
                    f"{wheel_line}"
                    f"Base odds: **{base_chance_pct:.2f}%** | Final odds: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                    f"Cooldown still applied: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
                )
            return

        shielded = consume_roulette_backfire_shield(ctx.guild.id, author.id)
        daily_state["backfired"] = True
        if shielded:
            record_game_fields(ctx.guild.id, author.id, "roulette", backfires=1, shield_saves=1)
            await save_data()
            wheel_line = (
                f"Wheel aim bonus applied: **+{wheel_aim_bonus * 100.0:.1f}%**.\n"
                if wheel_aim_bonus > 0.0
                else ""
            )
            await ctx.reply(
                f"Roulette: {author.mention} backfired, but your wheel shield blocked the timeout.\n"
                f"{wheel_line}"
                f"Base odds: **{base_chance_pct:.2f}%** | Final odds: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                f"Cooldown: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
            )
            return

        applied = await _timeout_member(
            author,
            backfire_timeout_seconds,
            f"Roulette by {author} (backfire, chance {chance_pct:.2f}%)",
        )
        record_game_fields(ctx.guild.id, author.id, "roulette", backfires=1)
        if applied:
            _store_active_timeout(ctx.guild.id, author.id, backfire_timeout_seconds)
            await save_data()
            record_game_fields(ctx.guild.id, author.id, "roulette", got_timed_out=1)
            wheel_line = (
                f"Wheel aim bonus applied: **+{wheel_aim_bonus * 100.0:.1f}%**.\n"
                if wheel_aim_bonus > 0.0
                else ""
            )
            await ctx.reply(
                f"Roulette: {author.mention} backfired.\n"
                f"{author.mention} timed out for **{_fmt_remaining(backfire_timeout_seconds)}**.\n"
                f"{wheel_line}"
                f"Base odds: **{base_chance_pct:.2f}%** | Final odds: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                f"Cooldown: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
            )
        else:
            wheel_line = (
                f"Wheel aim bonus applied: **+{wheel_aim_bonus * 100.0:.1f}%**.\n"
                if wheel_aim_bonus > 0.0
                else ""
            )
            await ctx.reply(
                f"Roulette: {author.mention} backfired, but I could not apply timeout "
                f"(permission/hierarchy).\n"
                f"{wheel_line}"
                f"Base odds: **{base_chance_pct:.2f}%** | Final odds: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                f"Cooldown still applied: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
            )
