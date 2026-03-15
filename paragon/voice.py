from __future__ import annotations

import asyncio
import traceback
from typing import Optional

import discord
from discord.ext import commands

from .ownership import owner_only
from .voice_runtime import cleanup_voice_client, ensure_voice_client
from .voice_support import dave_4017_message, dave_support_status, is_dave_close_4017


class VoiceCog(commands.Cog):
    """
    Voice scaffold for future AI call features.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="join")
    async def join(self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None):
        target = channel
        if target is None and getattr(ctx.author, "voice", None):
            target = ctx.author.voice.channel

        if target is None:
            await ctx.reply("Join a voice channel first or pass one explicitly.")
            return

        dave_ok, dave_reason = dave_support_status()
        if not dave_ok:
            await ctx.reply(f"Voice unavailable: {dave_reason}")
            return

        # If already in the target, do nothing
        if ctx.voice_client and ctx.voice_client.channel and ctx.voice_client.channel.id == target.id:
            await ctx.reply(f"Already in **{target.name}**.")
            return

        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            await ctx.reply("I can't move voice channels while audio is currently playing.")
            return

        try:
            moved = bool(ctx.voice_client and ctx.voice_client.channel and ctx.voice_client.channel.id != target.id)
            await ensure_voice_client(ctx, target)
            await ctx.reply(f"{'Moved to' if moved else 'Joined'} **{target.name}**.")
        except discord.ConnectionClosed as e:
            if is_dave_close_4017(e):
                await ctx.reply(dave_4017_message())
            else:
                await ctx.reply(f"Voice websocket closed while joining ({e.code}).")
            return
        except (discord.Forbidden, discord.HTTPException) as e:
            await ctx.reply(f"I couldn't join that channel (permissions or HTTP issue). ({type(e).__name__})")
            return
        except asyncio.TimeoutError as e:
            await ctx.reply(f"Timed out trying to join **{target.name}**. ({e})")
            return
        except RuntimeError as e:
            await ctx.reply(str(e))
            return
        except Exception as e:
            print("VOICE JOIN FAILED:", repr(e))
            traceback.print_exc()
            await ctx.reply(f"Voice join failed: `{type(e).__name__}: {str(e)[:180]}`")
            return

    @commands.command(name="leave", aliases=["disconnect", "dc"])
    async def leave(self, ctx: commands.Context):
        if ctx.guild is not None:
            playback_cog = self.bot.get_cog("PlaybackCog")
            if playback_cog is not None and hasattr(playback_cog, "shutdown_guild"):
                try:
                    await playback_cog.shutdown_guild(ctx.guild.id, clear_queue=True, disconnect=False)
                except Exception:
                    pass
            tts_cog = self.bot.get_cog("TTSCog")
            if tts_cog is not None and hasattr(tts_cog, "shutdown_guild"):
                try:
                    await tts_cog.shutdown_guild(ctx.guild.id, clear_queue=True, disconnect=False)
                except Exception:
                    pass

        vc = ctx.voice_client
        if not vc:
            await ctx.reply("I'm not connected to voice.")
            return

        await cleanup_voice_client(vc)

        await ctx.reply("Disconnected from voice.")
    
    @commands.command(name="voicehealth")
    @owner_only()
    async def voice_health(self, ctx: commands.Context):
        connected = "yes" if ctx.voice_client else "no"
        channel = ctx.voice_client.channel.name if (ctx.voice_client and ctx.voice_client.channel) else "n/a"
        is_conn = str(ctx.voice_client.is_connected()) if ctx.voice_client else "n/a"
        dave_ok, _ = dave_support_status()
        await ctx.reply(f"voice_client={connected} is_connected={is_conn} channel={channel} dave={str(dave_ok).lower()}")
