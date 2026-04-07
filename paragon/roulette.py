from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import random
import time
from typing import Optional

import discord
from discord.ext import commands

from .spin_support import consume_roulette_accuracy_bonus, consume_roulette_backfire_shield
from .stats_store import record_game_fields
from .storage import _udict, save_data

ROULETTE_COOLDOWN_SECONDS = 30 * 60

# Base success chance is fixed at 20%, then shifts by 2.5% per prestige gap.
# Shooter advantage raises the odds; target advantage lowers them.
ROULETTE_BASE_SUCCESS_CHANCE = 0.20
ROULETTE_GAP_STEP_CHANCE = 0.025
ROULETTE_MIN_SUCCESS_CHANCE = 0.025
ROULETTE_MAX_SUCCESS_CHANCE = 0.60

# Timeout scales off the base shot odds only:
# 2.5% odds -> 3m timeout, 60% odds -> 30s timeout.
ROULETTE_MIN_TIMEOUT_SECONDS = 30
ROULETTE_MAX_TIMEOUT_SECONDS = 3 * 60


def _get_user_prestige(member: discord.Member) -> int:
    u = _udict(member.guild.id, member.id)
    return max(0, int(u.get("prestige", 0)))


def _roulette_success_chance(shooter_prestige: int, target_prestige: int) -> float:
    sp = max(0, int(shooter_prestige))
    tp = max(0, int(target_prestige))
    gap = sp - tp
    chance = ROULETTE_BASE_SUCCESS_CHANCE + (float(gap) * ROULETTE_GAP_STEP_CHANCE)
    return max(ROULETTE_MIN_SUCCESS_CHANCE, min(ROULETTE_MAX_SUCCESS_CHANCE, chance))


def _timeout_seconds_for_chance(base_chance: float) -> int:
    chance = max(ROULETTE_MIN_SUCCESS_CHANCE, min(ROULETTE_MAX_SUCCESS_CHANCE, float(base_chance)))
    ratio = (chance - ROULETTE_MIN_SUCCESS_CHANCE) / (ROULETTE_MAX_SUCCESS_CHANCE - ROULETTE_MIN_SUCCESS_CHANCE)
    seconds = int(
        round(
            ROULETTE_MAX_TIMEOUT_SECONDS
            - (ROULETTE_MAX_TIMEOUT_SECONDS - ROULETTE_MIN_TIMEOUT_SECONDS) * ratio
        )
    )
    return max(ROULETTE_MIN_TIMEOUT_SECONDS, min(ROULETTE_MAX_TIMEOUT_SECONDS, seconds))


def _fmt_remaining(seconds: int) -> str:
    secs = max(0, int(seconds))
    mins, rem = divmod(secs, 60)
    if mins <= 0:
        return f"{rem}s"
    return f"{mins}m {rem:02d}s"


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
    - Timeout scales from 3m down to 30s based on the base shot odds
    - Wheel aim bonus increases hit odds only and can push final odds above 60%
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

        if not author.voice or not author.voice.channel:
            await ctx.reply("You must be in a voice channel to use roulette.")
            return
        if not target.voice or not target.voice.channel:
            await ctx.reply(f"{target.display_name} must be in a voice channel for roulette.")
            return

        user_state = _udict(ctx.guild.id, author.id)
        now_ts = time.time()
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
        timeout_seconds = _timeout_seconds_for_chance(base_chance)
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
            applied = await _timeout_member(
                target,
                timeout_seconds,
                f"Roulette by {author} (success, chance {chance_pct:.2f}%)",
            )
            record_game_fields(ctx.guild.id, author.id, "roulette", successes=1)
            if applied:
                record_game_fields(ctx.guild.id, target.id, "roulette", got_timed_out=1)
                wheel_line = (
                    f"Wheel aim bonus applied: **+{wheel_aim_bonus * 100.0:.1f}%**.\n"
                    if wheel_aim_bonus > 0.0
                    else ""
                )
                await ctx.reply(
                    f"Roulette: {author.mention} landed the shot.\n"
                    f"{target.mention} timed out for **{_fmt_remaining(timeout_seconds)}**.\n"
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
                    f"Roulette: {author.mention} rolled success, but I could not time out "
                    f"{target.mention} (permission/hierarchy).\n"
                    f"{wheel_line}"
                    f"Base odds: **{base_chance_pct:.2f}%** | Final odds: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                    f"Cooldown still applied: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
                )
            return

        shielded = consume_roulette_backfire_shield(ctx.guild.id, author.id)
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
            timeout_seconds,
            f"Roulette by {author} (backfire, chance {chance_pct:.2f}%)",
        )
        record_game_fields(ctx.guild.id, author.id, "roulette", backfires=1)
        if applied:
            record_game_fields(ctx.guild.id, author.id, "roulette", got_timed_out=1)
            wheel_line = (
                f"Wheel aim bonus applied: **+{wheel_aim_bonus * 100.0:.1f}%**.\n"
                if wheel_aim_bonus > 0.0
                else ""
            )
            await ctx.reply(
                f"Roulette: {author.mention} backfired.\n"
                f"{author.mention} timed out for **{_fmt_remaining(timeout_seconds)}**.\n"
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
