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
    st = g.setdefault(
        "surprise",
        {
            "next_at": None,
            "event_id": 0,
            "reward": 0,
            "pending_rewards": [],
            "claimed": [],
            "active": False,
        },
    )
    st.setdefault("next_at", None)
    st.setdefault("event_id", 0)
    st.setdefault("reward", 0)
    st.setdefault("claimed", [])
    pending = st.get("pending_rewards")
    if not isinstance(pending, list):
        pending = []
        st["pending_rewards"] = pending

    # Migrate legacy single-drop state into the new stacked queue.
    if bool(st.get("active")) and not pending:
        reward = int(st.get("reward", 0)) or _rand_xp()
        pending.append(int(reward))
        st["reward"] = int(reward)

    st["active"] = bool(pending)
    return st


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
                reward = _rand_xp()
                pending = st.get("pending_rewards")
                if not isinstance(pending, list):
                    pending = []
                    st["pending_rewards"] = pending
                st["event_id"] = int(now.timestamp())
                pending.append(int(reward))
                st["reward"] = int(reward)
                st["claimed"] = []
                st["active"] = bool(pending)
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
        st = _state(guild.id)
        pending_count = max(0, len(st.get("pending_rewards", [])))
        stack_line = f" Unclaimed stack: **{pending_count}**." if pending_count > 1 else ""
        msg = f"@here Surprise Drop! Type `{COMMAND_PREFIX}claim` to trigger a timed XP boost."
        msg += stack_line
        try:
            await ch.send(msg, allowed_mentions=discord.AllowedMentions(everyone=True))
        except Exception:
            try:
                await ch.send(
                    f"Surprise Drop! Type `{COMMAND_PREFIX}claim` to trigger a timed XP boost.{stack_line}"
                )
            except Exception:
                pass

    @commands.command(name="claim")
    async def claim(self, ctx: commands.Context):
        st = _state(ctx.guild.id)
        pending_raw = st.get("pending_rewards")
        pending_rewards = list(pending_raw) if isinstance(pending_raw, list) else []

        if not pending_rewards:
            await ctx.reply("There is not an active drop right now. Hang tight for the next one.")
            return

        uid_s = str(ctx.author.id)
        st["claimed"] = [uid_s]
        st["pending_rewards"] = []
        st["active"] = False
        st["reward"] = 0
        await save_data()

        rewards = [int(reward) if int(reward) > 0 else _rand_xp() for reward in pending_rewards]
        claim_count = len(rewards)
        total_reward = sum(rewards)
        total_pct = sum(_reward_to_pct(reward) for reward in rewards)
        boost = await grant_fixed_boost(
            ctx.author,
            pct=total_pct,
            minutes=SURPRISE_BOOST_MINUTES,
            source="surprise claim",
            reward_seed_xp=total_reward,
        )
        record_game_fields(
            ctx.guild.id,
            ctx.author.id,
            "surprise",
            claims=claim_count,
            boost_seed_xp_total=total_reward,
            boost_percent_total=boost["percent"],
            boost_minutes_total=(boost["minutes"] * claim_count),
        )
        await enforce_level6_exclusive(ctx.guild)
        if claim_count == 1:
            await ctx.reply(
                f"{ctx.author.mention} was the fastest! Boost gained: "
                f"**+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
            )
            return

        await ctx.reply(
            f"{ctx.author.mention} claimed **{claim_count}** stacked surprise drops at once! "
            f"Combined boost gained: **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
        )

    @commands.command(name="claimnow")
    @owner_only()
    async def claimnow(self, ctx: commands.Context):
        st = _state(ctx.guild.id)
        now = _utcnow()
        reward = _rand_xp()
        pending = st.get("pending_rewards")
        if not isinstance(pending, list):
            pending = []
            st["pending_rewards"] = pending
        st["event_id"] = int(now.timestamp())
        pending.append(int(reward))
        st["reward"] = int(reward)
        st["claimed"] = []
        st["active"] = bool(pending)
        st["next_at"] = _iso(now + timedelta(minutes=_rand_minutes()))
        await save_data()
        await self._announce_drop(ctx.guild)
        await ctx.reply("Triggered a surprise drop, stacked it onto any existing drops, and reset the timer.")
