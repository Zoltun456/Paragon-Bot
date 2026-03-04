# paragon/core.py
# core v2
from typing import Optional, Dict
import discord
from discord.ext import commands, tasks

from .config import AFK_CHANNEL_ID
from .guild_setup import ensure_guild_setup
from .storage import load_data, _gdict, _udict
from .xp import apply_delta, get_gain_state
from .roles import enforce_level6_exclusive

def _settings(gid: int) -> dict:
    g = _gdict(gid)
    st = g.get("settings")
    if st is None:
        st = {"inactive_loss_enabled": True}
        g["settings"] = st
    elif "inactive_loss_enabled" not in st:
        st["inactive_loss_enabled"] = True
    return st


def is_in_countable_vc(channel: Optional[discord.VoiceChannel]) -> bool:
    if channel is None: 
        return False
    if AFK_CHANNEL_ID and channel.id == AFK_CHANNEL_ID:
        return False
    return True

def should_apply_inactive_loss(member: discord.Member) -> bool:
    """
    Inactive loss conditions for XP v2:
    - Not in any VC                       -> loss
    - In AFK channel                      -> loss
    - In a VC but muted/deafened (self or server) -> loss
    """
    v = member.voice
    if not v or not v.channel:
        return True  # not in any call
    if AFK_CHANNEL_ID and v.channel.id == AFK_CHANNEL_ID:
        return True
    if v.mute or v.deaf or v.self_mute or v.self_deaf:
        return True
    return False  # fully active in a normal VC

def is_inactive_state(vstate: discord.VoiceState) -> bool:
    """
    'Inactive' means: user is in a countable VC but muted/deafened in any way.
    Outside VC (or in AFK VC) = not inactive for loss purposes.
    """
    if not is_in_countable_vc(vstate.channel): return False
    return bool(vstate.mute or vstate.deaf or vstate.self_mute or vstate.self_deaf)

class CoreCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        load_data()
        changed = 0
        for guild in self.bot.guilds:
            if await ensure_guild_setup(guild):
                changed += 1
        print(f"Logged in as {self.bot.user} (ID: {self.bot.user.id})")
        if changed:
            print(f"Synced guild setup for {changed} guild(s).")
        if not self.award_loop.is_running():
            self.award_loop.start()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await ensure_guild_setup(guild)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if before.owner_id != after.owner_id:
            await ensure_guild_setup(after)

    @tasks.loop(minutes=1)
    async def award_loop(self):
        # One passive gain tick per minute for every non-bot member.
        for guild in self.bot.guilds:
            _gdict(guild.id)
            for member in guild.members:
                if member.bot:
                    continue
                await apply_delta(member, minutes=1, inactive_minutes=0, source="voice minute")
            # Repurposed function now syncs Gold/Silver/Bronze podium roles.
            await enforce_level6_exclusive(guild)


    # Simple ping & public commands
    @commands.command(name="re")
    async def ping(self, ctx):
        await ctx.reply("tard!")

    @commands.command(name="rank", aliases=["xp", "level"])
    async def rank(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        u = _udict(ctx.guild.id, member.id)
        total = int(u.get("xp_f", u.get("xp", 0)))
        st = await get_gain_state(member)
        boosts = st.get("boosts", [])
        if boosts:
            first = boosts[0]
            extra = f" | Active boosts: **{len(boosts)}** (next expires in {first['minutes_left']}m)"
        else:
            extra = " | Active boosts: **0**"
        await ctx.reply(
            f"**{member.display_name}** Total XP: **{total}**"
            f" | Rate: **{st['rate_per_min']:.2f} XP/min** (x{st['multiplier']:.2f})"
            f"{extra}"
        )

    @commands.command(name="leaderboard", aliases=["lb", "xps"])
    async def leaderboard(self, ctx, limit: Optional[int] = 10):
        limit = max(1, min(25, int(limit or 10)))
        g = _gdict(ctx.guild.id); users = g.get("users", {})
        rows = []
        for uid_str, u in users.items():
            uid = int(uid_str)
            total = int(u.get("xp_f", u.get("xp", 0)))
            rows.append((uid, total))
        rows.sort(key=lambda t: (-t[1], t[0]))
        rows = rows[:limit]
        if not rows:
            await ctx.reply("No data yet."); return

        lines = []
        for i, (uid, total) in enumerate(rows, start=1):
            m = ctx.guild.get_member(uid)
            name = m.display_name if m else f"User {uid}"
            medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "•"))
            lines.append(f"`{i:>2}.` {medal} **{name}** - {total} XP")
        await ctx.reply("\n".join(lines))

    @commands.command(name="boosts", aliases=["rate", "mult"])
    async def boosts(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        st = await get_gain_state(member)
        lines = [
            f"**{member.display_name}** gain rate: **{st['rate_per_min']:.2f} XP/min** (base {st['base_per_min']:.2f}, x{st['multiplier']:.2f})"
        ]
        boosts = st.get("boosts", [])
        if not boosts:
            lines.append("No active boosts.")
        else:
            lines.append("Active boosts:")
            for b in boosts[:8]:
                lines.append(f"- **+{b['percent']:.1f}%** for **{b['minutes_left']}m** ({b['source']})")
            if len(boosts) > 8:
                lines.append(f"- ...and {len(boosts) - 8} more")
        await ctx.reply("\n".join(lines))

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        from discord.ext.commands import (
            CommandNotFound, MissingPermissions, CheckFailure,
            MissingRequiredArgument, BadArgument, CommandOnCooldown, DisabledCommand
        )
        orig = getattr(error, "original", error)
        try:
            if isinstance(orig, CommandNotFound):
                await ctx.reply("Unknown command. Try `!help`."); return
            if isinstance(orig, CheckFailure):
                await ctx.reply("You don't have permission to use that command."); return
            if isinstance(orig, MissingPermissions):
                await ctx.reply("You're missing required Discord permissions."); return
            if isinstance(orig, MissingRequiredArgument):
                await ctx.reply("Missing argument(s). Try `!help` or check the usage."); return
            if isinstance(orig, BadArgument):
                await ctx.reply("Bad argument. Please check your input."); return
            if isinstance(orig, DisabledCommand):
                await ctx.reply("That command is currently disabled."); return
            if isinstance(orig, CommandOnCooldown):
                await ctx.reply(f"Slow down-try again in {orig.retry_after:.1f}s."); return
            await ctx.reply("Something went wrong running that command.")
        except Exception:
            pass
        print(f"[Command Error] {type(orig).__name__}: {orig}")
