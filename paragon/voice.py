from __future__ import annotations

import asyncio
import time
import traceback
from typing import Optional

import discord
from discord.ext import commands, tasks

from .ownership import owner_only
from .voice_runtime import cleanup_voice_client, ensure_voice_client
from .voice_support import dave_4017_message, dave_support_status, is_dave_close_4017

VOICE_IDLE_DISCONNECT_SECONDS = 30.0


class VoiceCog(commands.Cog):
    """
    Voice scaffold for future AI call features.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._idle_since: dict[int, float] = {}

    def cog_unload(self):
        if self.idle_watchdog.is_running():
            self.idle_watchdog.cancel()
        self._idle_since.clear()

    def _should_keep_voice_connected(self, guild_id: int, vc: discord.VoiceClient) -> bool:
        try:
            if vc.is_playing() or vc.is_paused():
                return True
        except Exception:
            pass

        for cog_name in ("PlaybackCog", "TTSCog"):
            cog = self.bot.get_cog(cog_name)
            if cog is None or not hasattr(cog, "should_keep_voice_connected"):
                continue
            try:
                if bool(cog.should_keep_voice_connected(guild_id)):
                    return True
            except Exception:
                continue
        return False

    def _notify_voice_disconnected(self, guild_id: int) -> None:
        self._idle_since.pop(guild_id, None)
        for cog_name in ("PlaybackCog", "TTSCog"):
            cog = self.bot.get_cog(cog_name)
            if cog is None or not hasattr(cog, "note_voice_disconnected"):
                continue
            try:
                cog.note_voice_disconnected(guild_id)
            except Exception:
                continue

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.idle_watchdog.is_running():
            self.idle_watchdog.start()

    @tasks.loop(seconds=1.0)
    async def idle_watchdog(self):
        now = time.monotonic()
        for guild in self.bot.guilds:
            guild_id = int(guild.id)
            vc = guild.voice_client

            if vc is None or not vc.is_connected():
                self._idle_since.pop(guild_id, None)
                continue

            if self._should_keep_voice_connected(guild_id, vc):
                self._idle_since.pop(guild_id, None)
                continue

            idle_since = self._idle_since.setdefault(guild_id, now)
            if (now - idle_since) < VOICE_IDLE_DISCONNECT_SECONDS:
                continue

            await cleanup_voice_client(vc)
            self._notify_voice_disconnected(guild_id)

    @idle_watchdog.before_loop
    async def _before_idle_watchdog(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if not self.bot.user or member.id != self.bot.user.id or member.guild is None:
            return
        guild_id = int(member.guild.id)
        self._idle_since.pop(guild_id, None)
        if after.channel is None:
            self._notify_voice_disconnected(guild_id)

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
        self._notify_voice_disconnected(int(ctx.guild.id))

        await ctx.reply("Disconnected from voice.")
    
    @commands.command(name="voicehealth")
    @owner_only()
    async def voice_health(self, ctx: commands.Context):
        connected = "yes" if ctx.voice_client else "no"
        channel = ctx.voice_client.channel.name if (ctx.voice_client and ctx.voice_client.channel) else "n/a"
        is_conn = str(ctx.voice_client.is_connected()) if ctx.voice_client else "n/a"
        dave_ok, _ = dave_support_status()
        await ctx.reply(f"voice_client={connected} is_connected={is_conn} channel={channel} dave={str(dave_ok).lower()}")
