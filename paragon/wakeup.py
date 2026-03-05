from __future__ import annotations

import asyncio
import random
from typing import Optional

import discord
from discord.ext import commands

from .config import resolve_afk_channel_id

WAKEUP_HOPS = 10
WAKEUP_WAIT_SECONDS = 60
WAKEUP_HOP_DELAY_SECONDS = 0.6


class WakeupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._state_lock = asyncio.Lock()
        self._locked_targets: set[int] = set()

    async def _acquire_target_lock(self, uid: int) -> bool:
        async with self._state_lock:
            if uid in self._locked_targets:
                return False
            self._locked_targets.add(uid)
            return True

    async def _release_target_lock(self, uid: int):
        async with self._state_lock:
            self._locked_targets.discard(uid)

    async def _is_target_locked(self, uid: int) -> bool:
        async with self._state_lock:
            return uid in self._locked_targets

    def _member_can_join_voice(self, member: discord.Member, channel: discord.VoiceChannel) -> bool:
        perms = channel.permissions_for(member)
        if not perms.view_channel or not perms.connect:
            return False
        if channel.user_limit > 0 and len(channel.members) >= channel.user_limit and member not in channel.members:
            return False
        return True

    def _bot_can_move_to(self, guild: discord.Guild, channel: discord.VoiceChannel) -> bool:
        me = guild.me
        if me is None:
            return False
        perms = channel.permissions_for(me)
        return bool(perms.view_channel and perms.connect and perms.move_members)

    async def _move_member(self, member: discord.Member, channel: discord.VoiceChannel, *, reason: str) -> bool:
        try:
            await member.move_to(channel, reason=reason)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    @commands.command(name="wakeup", aliases=["wakeywakey"])
    async def wakeup(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        caller: discord.Member = ctx.author  # type: ignore

        if target is None:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}wakeup @user`")
            return
        if target.bot:
            await ctx.reply("Pick a non-bot user.")
            return

        caller_vc = caller.voice.channel if caller.voice and caller.voice.channel else None
        if caller_vc is None:
            await ctx.reply("You must be in a voice channel to use this command.")
            return

        afk_id = resolve_afk_channel_id(ctx.guild)
        afk_channel = ctx.guild.get_channel(afk_id) if afk_id > 0 else None
        if not isinstance(afk_channel, discord.VoiceChannel):
            await ctx.reply("No AFK voice channel could be resolved for this server.")
            return

        target_vc = target.voice.channel if target.voice and target.voice.channel else None
        if not isinstance(target_vc, discord.VoiceChannel) or target_vc.id != afk_channel.id:
            await ctx.reply(f"**{target.display_name}** is not currently in AFK.")
            return

        locked = await self._acquire_target_lock(target.id)
        if not locked:
            await ctx.reply(f"Wakeup is already active/locked for **{target.display_name}**.")
            return

        should_release = False
        try:
            eligible = [
                ch
                for ch in ctx.guild.voice_channels
                if ch.id != afk_channel.id
                and self._member_can_join_voice(target, ch)
                and self._bot_can_move_to(ctx.guild, ch)
            ]
            if not eligible:
                await ctx.reply("No eligible voice channels available to run wakeup hops.")
                should_release = True
                return

            current: Optional[discord.VoiceChannel] = target_vc
            for _ in range(WAKEUP_HOPS):
                pool = [ch for ch in eligible if current is None or ch.id != current.id] or eligible
                dest = random.choice(pool)
                moved = await self._move_member(target, dest, reason=f"Wakeup by {caller} ({caller.id})")
                if moved:
                    current = dest
                await asyncio.sleep(WAKEUP_HOP_DELAY_SECONDS)

            if not self._member_can_join_voice(target, caller_vc) or not self._bot_can_move_to(ctx.guild, caller_vc):
                await ctx.reply(
                    f"Finished wakeup hops for **{target.display_name}**, but couldn't move them to your channel due to permissions."
                )
            else:
                await self._move_member(target, caller_vc, reason=f"Wakeup final move by {caller} ({caller.id})")
                await ctx.reply(
                    f"Wakeup run complete for **{target.display_name}**. Waiting {WAKEUP_WAIT_SECONDS}s for a message response."
                )

            def _check(m: discord.Message) -> bool:
                return (
                    m.guild is not None
                    and m.guild.id == ctx.guild.id
                    and m.author.id == target.id
                    and not m.author.bot
                )

            try:
                await self.bot.wait_for("message", timeout=WAKEUP_WAIT_SECONDS, check=_check)
                await ctx.reply(
                    f"**{target.display_name}** responded. Their wakeup lock stays active until they return to AFK."
                )
            except asyncio.TimeoutError:
                moved_back = await self._move_member(
                    target,
                    afk_channel,
                    reason=f"Wakeup timeout return by {caller} ({caller.id})",
                )
                if moved_back:
                    await ctx.reply(f"**{target.display_name}** did not respond and was moved back to AFK.")
                    should_release = True
                else:
                    await ctx.reply(
                        f"**{target.display_name}** did not respond, but I couldn't move them back to AFK. "
                        "Lock remains until they re-enter AFK."
                    )
        finally:
            if not should_release:
                afk_id_now = resolve_afk_channel_id(ctx.guild)
                current_vc = target.voice.channel if target.voice and target.voice.channel else None
                if afk_id_now > 0 and current_vc and current_vc.id == afk_id_now:
                    should_release = True
            if should_release:
                await self._release_target_lock(target.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        if not await self._is_target_locked(member.id):
            return
        afk_id = resolve_afk_channel_id(member.guild)
        if afk_id <= 0:
            return
        if after.channel and after.channel.id == afk_id:
            await self._release_target_lock(member.id)
