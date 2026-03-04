from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import random
import time
from typing import Optional

import discord
from discord.ext import commands

from .stats_store import record_game_fields
from .storage import _udict, save_data

ROULETTE_COOLDOWN_SECONDS = 30 * 60

# Success chance is driven by the shooter's prestige and soft-capped at 50%.
# Higher-target prestige reduces that chance further.
ROULETTE_MIN_SUCCESS_CHANCE = 0.01
ROULETTE_MAX_SUCCESS_CHANCE = 0.50
ROULETTE_PRESTIGE_CURVE = 45.0
ROULETTE_TARGET_DEFENSE_GAP_SCALE = 40.0

# Timeout scales by prestige difference of loser relative to winner.
# If loser prestige <= winner prestige, timeout stays at the minimum.
ROULETTE_MIN_TIMEOUT_SECONDS = 10
ROULETTE_MAX_TIMEOUT_SECONDS = 5 * 60
ROULETTE_TIMEOUT_MAX_GAP = 49


def _get_user_prestige(member: discord.Member) -> int:
    u = _udict(member.guild.id, member.id)
    return max(0, int(u.get("prestige", 0)))


def _roulette_success_chance(shooter_prestige: int, target_prestige: int) -> float:
    sp = max(0, int(shooter_prestige))
    tp = max(0, int(target_prestige))

    base = ROULETTE_MIN_SUCCESS_CHANCE + (
        (ROULETTE_MAX_SUCCESS_CHANCE - ROULETTE_MIN_SUCCESS_CHANCE)
        * (1.0 - math.exp(-float(sp) / ROULETTE_PRESTIGE_CURVE))
    )

    defense_gap = max(0, tp - sp)
    defense_mult = 1.0 / (1.0 + (float(defense_gap) / ROULETTE_TARGET_DEFENSE_GAP_SCALE))

    chance = base * defense_mult
    return max(ROULETTE_MIN_SUCCESS_CHANCE, min(ROULETTE_MAX_SUCCESS_CHANCE, chance))


def _timeout_seconds_for_loser(loser_prestige: int, winner_prestige: int) -> int:
    lp = max(0, int(loser_prestige))
    wp = max(0, int(winner_prestige))
    gap = max(0, lp - wp)

    ratio = min(1.0, float(gap) / float(ROULETTE_TIMEOUT_MAX_GAP))
    seconds = int(
        round(
            ROULETTE_MIN_TIMEOUT_SECONDS
            + (ROULETTE_MAX_TIMEOUT_SECONDS - ROULETTE_MIN_TIMEOUT_SECONDS) * ratio
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
    - Success chance scales with shooter prestige and soft-caps at 50%
    - If shooter targets much higher prestige, success odds are reduced
    - Loser timeout scales from 10s up to 5m by prestige gap (loser minus winner)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="roulette", aliases=["r"])
    async def roulette(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        author: discord.Member = ctx.author  # type: ignore

        if target is None:
            await ctx.reply("Usage: `!roulette @user`")
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
        chance = _roulette_success_chance(author_p, target_p)
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
            timeout_seconds = _timeout_seconds_for_loser(target_p, author_p)
            applied = await _timeout_member(
                target,
                timeout_seconds,
                f"Roulette by {author} (success, chance {chance_pct:.2f}%)",
            )
            record_game_fields(ctx.guild.id, author.id, "roulette", successes=1)
            if applied:
                record_game_fields(ctx.guild.id, target.id, "roulette", got_timed_out=1)
                await ctx.reply(
                    f"Roulette: {author.mention} landed the shot.\n"
                    f"{target.mention} timed out for **{_fmt_remaining(timeout_seconds)}**.\n"
                    f"Odds this shot: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                    f"Cooldown: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
                )
            else:
                await ctx.reply(
                    f"Roulette: {author.mention} rolled success, but I could not time out "
                    f"{target.mention} (permission/hierarchy).\n"
                    f"Odds this shot: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                    f"Cooldown still applied: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
                )
            return

        timeout_seconds = _timeout_seconds_for_loser(author_p, target_p)
        applied = await _timeout_member(
            author,
            timeout_seconds,
            f"Roulette by {author} (backfire, chance {chance_pct:.2f}%)",
        )
        record_game_fields(ctx.guild.id, author.id, "roulette", backfires=1)
        if applied:
            record_game_fields(ctx.guild.id, author.id, "roulette", got_timed_out=1)
            await ctx.reply(
                f"Roulette: {author.mention} backfired.\n"
                f"{author.mention} timed out for **{_fmt_remaining(timeout_seconds)}**.\n"
                f"Odds this shot: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                f"Cooldown: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
            )
        else:
            await ctx.reply(
                f"Roulette: {author.mention} backfired, but I could not apply timeout "
                f"(permission/hierarchy).\n"
                f"Odds this shot: **{chance_pct:.2f}%** (P{author_p} vs P{target_p}).\n"
                f"Cooldown still applied: **{_fmt_remaining(ROULETTE_COOLDOWN_SECONDS)}**."
            )
