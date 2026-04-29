from __future__ import annotations

import asyncio
import time
from typing import Optional

import discord
from discord.ext import commands

from .config import COMMAND_PREFIX


QUIET_COOLDOWN_SECONDS = 30 * 60
QUIET_DURATION_SECONDS = 30


def _now_ts() -> int:
    return int(time.time())


def _fmt_remaining(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    minutes, secs = divmod(total, 60)
    if minutes <= 0:
        return f"{secs}s"
    return f"{minutes}m {secs:02d}s"


def _command_text(command: str, *, prefix: str = COMMAND_PREFIX) -> str:
    return f"`{prefix}{command}`"


class QuietCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldowns: dict[tuple[int, int], int] = {}
        self._active_mutes: dict[tuple[int, int], int] = {}
        self._unmute_tasks: dict[tuple[int, int], asyncio.Task] = {}

    def cog_unload(self):
        for task in self._unmute_tasks.values():
            task.cancel()
        self._cooldowns.clear()
        self._unmute_tasks.clear()
        self._active_mutes.clear()

    def _key(self, guild_id: int, user_id: int) -> tuple[int, int]:
        return (int(guild_id), int(user_id))

    def _cooldown_remaining(self, guild_id: int, user_id: int) -> int:
        key = self._key(guild_id, user_id)
        expires_at = int(self._cooldowns.get(key, 0))
        remaining = max(0, expires_at - _now_ts())
        if remaining <= 0:
            self._cooldowns.pop(key, None)
        return remaining

    def _set_cooldown(self, guild_id: int, user_id: int) -> None:
        key = self._key(guild_id, user_id)
        self._cooldowns[key] = _now_ts() + QUIET_COOLDOWN_SECONDS

    def _clear_expired_mute(self, guild_id: int, user_id: int, *, still_muted: bool) -> None:
        key = self._key(guild_id, user_id)
        expires_at = int(self._active_mutes.get(key, 0))
        if expires_at > _now_ts():
            return
        if still_muted:
            return
        self._active_mutes.pop(key, None)
        task = self._unmute_tasks.pop(key, None)
        if task is not None:
            task.cancel()

    def _schedule_unmute(self, guild_id: int, user_id: int) -> None:
        key = self._key(guild_id, user_id)
        task = self._unmute_tasks.get(key)
        if task is not None:
            task.cancel()
        self._unmute_tasks[key] = asyncio.create_task(self._finish_mute(guild_id, user_id))

    async def _finish_mute(self, guild_id: int, user_id: int) -> None:
        key = self._key(guild_id, user_id)
        expires_at = int(self._active_mutes.get(key, 0))
        if expires_at <= 0:
            self._unmute_tasks.pop(key, None)
            return

        delay = max(0, expires_at - _now_ts())
        if delay > 0:
            await asyncio.sleep(delay)

        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild is not None else None
        try:
            if member is not None and getattr(member, "voice", None) and member.voice.channel is not None and bool(member.voice.mute):
                await member.edit(mute=False, reason=f"{COMMAND_PREFIX}shh expired")
            self._active_mutes.pop(key, None)
        except (discord.Forbidden, discord.HTTPException):
            pass
        finally:
            self._unmute_tasks.pop(key, None)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot or member.guild is None:
            return

        key = self._key(member.guild.id, member.id)
        expires_at = int(self._active_mutes.get(key, 0))
        if expires_at <= 0:
            return

        if expires_at <= _now_ts():
            self._unmute_tasks.pop(key, None)
            if after.channel is not None:
                if bool(after.mute):
                    try:
                        await member.edit(mute=False, reason=f"{COMMAND_PREFIX}shh expired")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                self._active_mutes.pop(key, None)
            return

        if after.channel is not None and before.mute and not bool(after.mute):
            return

        if before.channel is None and after.channel is not None and not bool(after.mute):
            try:
                await member.edit(mute=True, reason=f"{COMMAND_PREFIX}shh still active")
            except (discord.Forbidden, discord.HTTPException):
                pass

    @commands.command(name="shh")
    async def shh(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if target is None:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}shh @user`")
            return
        if target.bot:
            await ctx.reply("Bots do not need to be shushed.")
            return
        if target.id == ctx.author.id:
            await ctx.reply("You cannot shush yourself.")
            return
        if target.guild.id != ctx.guild.id:
            await ctx.reply("Target must be a member of this server.")
            return
        if not getattr(target, "voice", None) or target.voice.channel is None:
            await ctx.reply(f"**{target.display_name}** is not in a voice channel.")
            return

        remaining = self._cooldown_remaining(ctx.guild.id, ctx.author.id)
        if remaining > 0:
            await ctx.reply(
                f"{_command_text('shh', prefix=ctx.clean_prefix)} is on cooldown for you. "
                f"Try again in **{_fmt_remaining(remaining)}**."
            )
            return

        self._clear_expired_mute(ctx.guild.id, target.id, still_muted=bool(target.voice.mute))
        key = self._key(ctx.guild.id, target.id)
        now = _now_ts()
        active_until = int(self._active_mutes.get(key, 0))
        is_chaining = active_until > now

        if bool(target.voice.mute) and not is_chaining:
            await ctx.reply(f"**{target.display_name}** is already server muted.")
            return

        me = ctx.guild.me
        if me is None:
            await ctx.reply("Bot member state is unavailable right now.")
            return
        if ctx.guild.owner_id == target.id or target.top_role >= me.top_role:
            await ctx.reply(f"I can't mute **{target.display_name}** because their role is too high.")
            return

        try:
            await target.edit(mute=True, reason=f"{COMMAND_PREFIX}shh by {ctx.author} ({ctx.author.id})")
        except discord.Forbidden:
            await ctx.reply("I don't have permission to server mute that user.")
            return
        except discord.HTTPException as e:
            await ctx.reply(f"I couldn't mute **{target.display_name}**. ({type(e).__name__})")
            return

        self._set_cooldown(ctx.guild.id, ctx.author.id)
        expires_at = max(now, active_until) + QUIET_DURATION_SECONDS
        self._active_mutes[key] = expires_at
        self._schedule_unmute(ctx.guild.id, target.id)
        total_remaining = max(0, expires_at - now)

        if is_chaining:
            await ctx.reply(
                f"{target.mention} has been shushed again. Total remaining mute time is now **{_fmt_remaining(total_remaining)}**. "
                f"Your {_command_text('shh', prefix=ctx.clean_prefix)} cooldown is now **{_fmt_remaining(QUIET_COOLDOWN_SECONDS)}**."
            )
            return

        await ctx.reply(
            f"{target.mention} has been shushed for **{QUIET_DURATION_SECONDS}s**. "
            f"Your {_command_text('shh', prefix=ctx.clean_prefix)} cooldown is now **{_fmt_remaining(QUIET_COOLDOWN_SECONDS)}**."
        )
