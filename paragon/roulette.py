# paragon/roulette.py
from __future__ import annotations
from typing import Optional
import random
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from .config import (
    ROULETTE_COST_XP,
    ROULETTE_TIMEOUT_SECONDS,
    ROULETTE_SUCCESS_N,
    ROULETTE_SUCCESS_OUTOF,
)
from .storage import _udict  # to check current XP:contentReference[oaicite:0]{index=0}
from .stats_store import record_game_fields
from .xp import apply_xp_change  # to deduct XP:contentReference[oaicite:1]{index=1}
from .roles import sync_level_roles, enforce_level6_exclusive, announce_level_up  # role sync + crown:contentReference[oaicite:2]{index=2}


def _get_user_xp_int(member: discord.Member) -> int:
    u = _udict(member.guild.id, member.id)  # ensures user record:contentReference[oaicite:3]{index=3}
    return int(u.get("xp_f", u.get("xp", 0)))


async def _timeout_member(m: discord.Member, seconds: int, reason: str) -> bool:
    """
    Apply timeout. Returns True if applied, False if missing perms / HTTP error.
    Compatible with discord.py 2.0+.
    """
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    try:
        # discord.py 2.0+ expects a datetime here
        if hasattr(m, "timeout"):
            await m.timeout(until, reason=reason)  # <-- FIXED
        else:
            # Fallback for versions with edit()
            await m.edit(communication_disabled_until=until, reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


class RouletteCog(commands.Cog):
    """
    !roulette @user
    - Requires caller to have >= ROULETTE_COST_XP
    - Always deducts ROULETTE_COST_XP from the caller
    - 1/ROULETTE_SUCCESS_OUTOF chance (default 1/6) to timeout the target for ROULETTE_TIMEOUT_SECONDS
      otherwise the caller is timed out for the same duration
    - Provides feedback message
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="roulette", aliases=["r"])
    async def roulette(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        author: discord.Member = ctx.author  # type: ignore

        if target is None:
            await ctx.reply("Usage: `!roulette @user`")
            return
        if target.bot:
            await ctx.reply("You can’t roulette a bot.")
            return
        if target.guild.id != ctx.guild.id:
            await ctx.reply("Target must be a member of this server.")
            return

        # ✅ New check: both must be in *a* voice channel
        if not author.voice or not author.voice.channel:
            await ctx.reply("You must be in a voice channel to use roulette.")
            return
        if not target.voice or not target.voice.channel:
            await ctx.reply(f"{target.display_name} must be in a voice channel for roulette.")
            return
        # If they’re in different channels, we still allow it

        # Check XP
        cur_xp = _get_user_xp_int(author)
        if cur_xp < ROULETTE_COST_XP:
            await ctx.reply(f"You need at least **{ROULETTE_COST_XP} XP** to play roulette.")
            return

        # Deduct XP first (always)
        lvl_change = await apply_xp_change(author, -ROULETTE_COST_XP, source="roulette play")  # may change level:contentReference[oaicite:4]{index=4}
        record_game_fields(
            ctx.guild.id,
            author.id,
            "roulette",
            plays=1,
            xp_spent_total=ROULETTE_COST_XP,
        )
        if lvl_change:
            old_lvl, new_lvl = lvl_change
            if new_lvl > old_lvl:
                await announce_level_up(author, new_lvl)  # only announces on level-up:contentReference[oaicite:5]{index=5}
            await sync_level_roles(author, new_lvl)       # keep roles in sync:contentReference[oaicite:6]{index=6}
        else:
            # Even without a level change, keep roles tidy
            u = _udict(ctx.guild.id, author.id)
            await sync_level_roles(author, int(u.get("level", 1)))       #:contentReference[oaicite:7]{index=7}
        await enforce_level6_exclusive(ctx.guild)                         # crown enforcement:contentReference[oaicite:8]{index=8}

        # Roll the roulette
        roll = random.randint(1, ROULETTE_SUCCESS_OUTOF)
        success = (roll == ROULETTE_SUCCESS_N)

        # Try to apply timeout
        if success:
            applied = await _timeout_member(target, ROULETTE_TIMEOUT_SECONDS, f"Roulette by {author} (success)")
            record_game_fields(ctx.guild.id, author.id, "roulette", successes=1)
            if applied:
                record_game_fields(ctx.guild.id, target.id, "roulette", got_timed_out=1)
                await ctx.reply(
                    f"🎲 **Roulette!** {author.mention} took the shot… **Success!**\n"
                    f"{target.mention} is timed out for **{ROULETTE_TIMEOUT_SECONDS}s**.\n"
                    f"−{ROULETTE_COST_XP} XP from {author.mention}."
                )
            else:
                await ctx.reply(
                    f"🎲 **Roulette!** {author.mention} rolled success, but I couldn’t time out {target.mention} "
                    f"(missing permission?). You still spent **{ROULETTE_COST_XP} XP**."
                )
        else:
            applied = await _timeout_member(author, ROULETTE_TIMEOUT_SECONDS, f"Roulette by {author} (backfire)")
            record_game_fields(ctx.guild.id, author.id, "roulette", backfires=1)
            if applied:
                record_game_fields(ctx.guild.id, author.id, "roulette", got_timed_out=1)
                await ctx.reply(
                    f"🎲 **Roulette!** {author.mention} took the shot… **Backfire!**\n"
                    f"{author.mention} is timed out for **{ROULETTE_TIMEOUT_SECONDS}s**.\n"
                    f"−{ROULETTE_COST_XP} XP spent."
                )
            else:
                await ctx.reply(
                    f"🎲 **Roulette!** {author.mention} took the shot… **Backfire!** "
                    f"but I couldn’t time you out (missing permission?). "
                    f"−{ROULETTE_COST_XP} XP spent."
                )
