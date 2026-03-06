from __future__ import annotations

import asyncio
import traceback
from typing import Optional

import discord
from discord.ext import commands

from .ownership import owner_only
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

        # If we're connected elsewhere, try moving first
        try:
            if ctx.voice_client:
                await ctx.voice_client.move_to(target)
                await ctx.reply(f"Moved to **{target.name}**.")
                return
        except discord.ConnectionClosed as e:
            if is_dave_close_4017(e):
                await ctx.reply(dave_4017_message())
                return
        except Exception:
            # If move fails, we will try a full reconnect below
            pass

        # Full (re)connect
        vc = None
        try:
            # timeout + reconnect + self_deaf avoids accidental feedback loops.
            vc = await target.connect(
                timeout=30.0,
                reconnect=True,
                self_deaf=True,
                self_mute=False,
            )

            # Give the library a moment to finalize the handshake
            deadline = asyncio.get_running_loop().time() + 10.0
            while asyncio.get_running_loop().time() < deadline:
                if vc and vc.is_connected():
                    break
                await asyncio.sleep(0.2)

            if not vc or not vc.is_connected():
                raise asyncio.TimeoutError("Voice client did not report connected after connect().")

            await ctx.reply(f"Joined **{target.name}**.")
            return

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

        except Exception as e:
            # THIS is what you were missing: most voice failures are not HTTPException/Forbidden
            print("VOICE JOIN FAILED:", repr(e))
            traceback.print_exc()
            await ctx.reply(f"Voice join failed: `{type(e).__name__}: {str(e)[:180]}`")
            return

        finally:
            # If we failed mid-handshake, make sure we don't leave a half-open voice session
            if (vc is not None) and (not vc.is_connected()):
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                try:
                    vc.cleanup()
                except Exception:
                    pass

    @commands.command(name="leave", aliases=["disconnect", "dc"])
    async def leave(self, ctx: commands.Context):
        vc = ctx.voice_client
        if not vc:
            await ctx.reply("I'm not connected to voice.")
            return

        try:
            if vc.is_playing():
                vc.stop()
        except Exception:
            pass

        try:
            await vc.disconnect(force=True)
        finally:
            # CRITICAL: prevents auto-rejoin + unclosed connections
            try:
                vc.cleanup()
            except Exception:
                pass

        await ctx.reply("Disconnected from voice.")
    
    @commands.command(name="voicehealth")
    @owner_only()
    async def voice_health(self, ctx: commands.Context):
        connected = "yes" if ctx.voice_client else "no"
        channel = ctx.voice_client.channel.name if (ctx.voice_client and ctx.voice_client.channel) else "n/a"
        is_conn = str(ctx.voice_client.is_connected()) if ctx.voice_client else "n/a"
        dave_ok, _ = dave_support_status()
        await ctx.reply(f"voice_client={connected} is_connected={is_conn} channel={channel} dave={str(dave_ok).lower()}")
