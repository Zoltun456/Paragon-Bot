from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
import random

import discord
from discord.ext import commands, tasks

from .config import (
    COMMAND_PREFIX,
    DROP_MAX_MINUTES,
    DROP_MAX_XP,
    DROP_MIN_MINUTES,
    DROP_MIN_XP,
    SURPRISE_BOOST_MINUTES,
    SURPRISE_MAX_PCT,
    SURPRISE_MIN_PCT,
    SURPRISE_PCT_STEP,
)
from .guild_setup import get_log_channel
from .ownership import owner_only
from .roles import enforce_level6_exclusive
from .stats_store import record_game_fields
from .storage import _gdict, save_data
from .xp import grant_fixed_boost


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rand_minutes() -> int:
    return random.randint(DROP_MIN_MINUTES, DROP_MAX_MINUTES)


def _rand_xp() -> int:
    return random.randint(DROP_MIN_XP, DROP_MAX_XP)


def _reward_to_pct(reward: int) -> float:
    lo = min(int(DROP_MIN_XP), int(DROP_MAX_XP))
    hi = max(int(DROP_MIN_XP), int(DROP_MAX_XP))
    if hi <= lo:
        return float(SURPRISE_MAX_PCT)
    clamped = max(lo, min(int(reward), hi))
    ratio = (clamped - lo) / float(hi - lo)
    steps = int(round(ratio * ((SURPRISE_MAX_PCT - SURPRISE_MIN_PCT) / SURPRISE_PCT_STEP)))
    pct = SURPRISE_MIN_PCT + (steps * SURPRISE_PCT_STEP)
    return max(SURPRISE_MIN_PCT, min(SURPRISE_MAX_PCT, float(pct)))


def _state(gid: int) -> dict:
    g = _gdict(gid)
    return g.setdefault(
        "surprise",
        {
            "next_at": None,
            "event_id": 0,
            "reward": 0,
            "claimed": [],
            "active": False,
        },
    )


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class SurpriseCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not self.drop_loop.is_running():
            self.drop_loop.start()

    @tasks.loop(minutes=1)
    async def drop_loop(self):
        now = _utcnow()
        for guild in list(self.bot.guilds):
            st = _state(guild.id)
            next_at = _parse_iso(st.get("next_at"))
            if next_at is None:
                st["next_at"] = _iso(now + timedelta(minutes=_rand_minutes()))
                await save_data()
                continue
            if now >= next_at:
                st["event_id"] = int(now.timestamp())
                st["reward"] = _rand_xp()
                st["claimed"] = []
                st["active"] = True
                st["next_at"] = _iso(now + timedelta(minutes=_rand_minutes()))
                await self._announce_drop(guild)
                await save_data()

    async def _announce_drop(self, guild: discord.Guild):
        ch = get_log_channel(guild)
        if not ch:
            return
        perms = ch.permissions_for(guild.me)
        if not perms.send_messages:
            return
        msg = f"@here Surprise Drop! Type `{COMMAND_PREFIX}claim` to trigger a timed XP boost."
        try:
            await ch.send(msg, allowed_mentions=discord.AllowedMentions(everyone=True))
        except Exception:
            try:
                await ch.send(f"Surprise Drop! Type `{COMMAND_PREFIX}claim` to trigger a timed XP boost.")
            except Exception:
                pass

    @commands.command(name="claim")
    async def claim(self, ctx: commands.Context):
        st = _state(ctx.guild.id)

        if not st.get("active"):
            await ctx.reply("There is not an active drop right now. Hang tight for the next one.")
            return

        if st.get("claimed"):
            claimer_id = st["claimed"][0]
            claimer = ctx.guild.get_member(int(claimer_id)) if claimer_id else None
            name = claimer.display_name if claimer else f"User {claimer_id}"
            await ctx.reply(f"Too late. The drop was already claimed by **{name}**.")
            return

        uid_s = str(ctx.author.id)
        st["claimed"].append(uid_s)
        st["active"] = False
        await save_data()

        reward = int(st.get("reward", 0)) or _rand_xp()
        pct = _reward_to_pct(reward)
        boost = await grant_fixed_boost(
            ctx.author,
            pct=pct,
            minutes=SURPRISE_BOOST_MINUTES,
            source="surprise claim",
            reward_seed_xp=reward,
        )
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
            f"{ctx.author.mention} was the fastest! Boost gained: "
            f"**+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
        )

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
        await self._announce_drop(ctx.guild)
        await ctx.reply("Triggered a surprise drop and reset the timer.")
