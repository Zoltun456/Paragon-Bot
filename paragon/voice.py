from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands

from .ownership import owner_only


class VoiceCog(commands.Cog):
    """
    Voice scaffold for future AI call features.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="join")
    async def join(self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None):
        target = channel
        if target is None and ctx.author.voice:
            target = ctx.author.voice.channel
        if target is None:
            await ctx.reply("Join a voice channel first or pass one explicitly.")
            return

        if ctx.voice_client and ctx.voice_client.channel.id == target.id:
            await ctx.reply(f"Already in **{target.name}**.")
            return

        try:
            if ctx.voice_client:
                await ctx.voice_client.move_to(target)
            else:
                await target.connect()
            await ctx.reply(f"Joined **{target.name}**.")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.reply("I couldn't join that channel (permissions or connection issue).")

    @commands.command(name="leave", aliases=["disconnect", "dc"])
    async def leave(self, ctx: commands.Context):
        if not ctx.voice_client:
            await ctx.reply("I'm not connected to voice.")
            return
        await ctx.voice_client.disconnect(force=True)
        await ctx.reply("Disconnected from voice.")

    @commands.command(name="voicehealth")
    @owner_only()
    async def voice_health(self, ctx: commands.Context):
        connected = "yes" if ctx.voice_client else "no"
        channel = ctx.voice_client.channel.name if ctx.voice_client else "n/a"
        await ctx.reply(f"voice_connected={connected} channel={channel}")
