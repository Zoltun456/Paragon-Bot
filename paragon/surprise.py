# paragon/surprise.py
from __future__ import annotations
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks

from .config import (
    DROP_MIN_MINUTES, DROP_MAX_MINUTES,
    DROP_MIN_XP, DROP_MAX_XP,
    COMMAND_PREFIX,
)
from .guild_setup import get_log_channel
from .storage import _gdict, save_data
from .stats_store import record_game_fields
from .xp import grant_reward_boost
from .roles import enforce_level6_exclusive
from .ownership import owner_only

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _rand_minutes() -> int:
    return random.randint(DROP_MIN_MINUTES, DROP_MAX_MINUTES)

def _rand_xp() -> int:
    return random.randint(DROP_MIN_XP, DROP_MAX_XP)

def _state(gid: int) -> dict:
    g = _gdict(gid)  # ensure guild entry exists:contentReference[oaicite:8]{index=8}
    return g.setdefault("surprise", {
        "next_at": None,          # ISO string in UTC
        "event_id": 0,            # increments each drop
        "reward": 0,              # XP for this event
        "claimed": [],            # list[str] user IDs who claimed
        "active": False
    })

def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()

def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s: return None
    try:
        # ensure timezone awareness
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class SurpriseCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not self.drop_loop.is_running():
            self.drop_loop.start()

    # ----------- Loop to schedule/trigger surprise drops -----------
    @tasks.loop(minutes=1)
    async def drop_loop(self):
        now = _utcnow()
        for guild in list(self.bot.guilds):
            st = _state(guild.id)
            next_at = _parse_iso(st.get("next_at"))
            # Initialize a schedule if missing
            if next_at is None:
                st["next_at"] = _iso(now + timedelta(minutes=_rand_minutes()))
                await save_data()
                continue
            # Time for a new drop?
            if now >= next_at:
                # Start a new event
                st["event_id"] = int(now.timestamp())
                st["reward"] = _rand_xp()
                st["claimed"] = []
                st["active"] = True
                # Schedule next drop now (event remains claimable until the next one fires)
                st["next_at"] = _iso(now + timedelta(minutes=_rand_minutes()))
                await self._announce_drop(guild, st["reward"])
                await save_data()

    async def _announce_drop(self, guild: discord.Guild, reward: int):
        ch = get_log_channel(guild)
        if not ch:
            return
        perms = ch.permissions_for(guild.me)
        if not perms.send_messages:
            return
        # Try to @here (requires “Mention @everyone” permission in that channel)
        try:
            await ch.send(
                f"@here 🎁 **Surprise Drop!** Type `{COMMAND_PREFIX}claim` to trigger a timed XP boost (power seed **{reward}**).",
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
        except Exception:
            # Fallback without an actual ping
            try:
                await ch.send(f"🎁 **Surprise Drop!** Type `{COMMAND_PREFIX}claim` to trigger a timed XP boost (power seed **{reward}**).")
            except Exception:
                pass

    # ----------- User command to claim the active drop -----------
    @commands.command(name="claim")
    async def claim(self, ctx: commands.Context):
        st = _state(ctx.guild.id)

        if not st.get("active"):
            await ctx.reply("There isn’t an active drop right now. Hang tight for the next one!")
            return

        # If someone already claimed, block further claims
        if st.get("claimed"):
            claimer_id = st["claimed"][0]
            claimer = ctx.guild.get_member(int(claimer_id)) if claimer_id else None
            name = claimer.display_name if claimer else f"User {claimer_id}"
            await ctx.reply(f"❌ Too late! The drop was already claimed by **{name}**.")
            return

        # First person to claim
        uid_s = str(ctx.author.id)
        st["claimed"].append(uid_s)
        st["active"] = False   # deactivate drop after first claim
        await save_data()

        reward = int(st.get("reward", 0)) or _rand_xp()

        # Apply XP reward
        boost = await grant_reward_boost(ctx.author, reward, source="surprise claim")
        record_game_fields(
            ctx.guild.id,
            ctx.author.id,
            "surprise",
            claims=1,
            boost_seed_xp_total=reward,
            boost_percent_total=boost["percent"],
            boost_minutes_total=boost["minutes"],
        )
        await enforce_level6_exclusive(ctx.guild)
        await ctx.reply(
            f"🎉 {ctx.author.mention} was the fastest! Boost gained: "
            f"**+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
        )

    # ----------- Owner-only: trigger a drop now -----------
    @commands.command(name="claimnow")
    @owner_only()
    async def claimnow(self, ctx: commands.Context):
        st = _state(ctx.guild.id)
        now = _utcnow()
        st["event_id"] = int(now.timestamp())
        st["reward"] = _rand_xp()
        st["claimed"] = []
        st["active"] = True
        st["next_at"] = _iso(now + timedelta(minutes=_rand_minutes()))
        await save_data()
        await self._announce_drop(ctx.guild, st["reward"])
        await ctx.reply("Triggered a surprise drop and reset the timer.")
