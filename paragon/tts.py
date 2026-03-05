from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import discord
from discord.ext import commands

from .config import ELEVEN_API, ELEVEN_MODEL_ID, ELEVEN_OUTPUT_FORMAT, ELEVEN_VOICE_ID


MAX_SAY_CHARS = 350
VOICE_CACHE_TTL_SECONDS = 30 * 60
POST_PLAY_IDLE_SECONDS = 1.0
TTS_DEBUG = os.getenv("TTS_DEBUG", "true").strip().lower() in {"1", "true", "yes", "on"}


def _fetch_eleven_voice_ids() -> list[str]:
    if not ELEVEN_API:
        return []
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices",
        method="GET",
        headers={
            "xi-api-key": ELEVEN_API,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except Exception:
        return []
    try:
        payload = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return []
    voices = payload.get("voices")
    if not isinstance(voices, list):
        return []
    out: list[str] = []
    for v in voices:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("voice_id", "")).strip()
        if vid:
            out.append(vid)
    return out


def _synthesize_eleven_bytes(text: str, *, voice_id: str, voice_settings: dict, seed: int, speed: float) -> bytes:
    if not ELEVEN_API:
        raise RuntimeError("ELEVEN_API is not configured.")

    voice_id = (voice_id or ELEVEN_VOICE_ID or "21m00Tcm4TlvDq8ikWAM").strip()
    model_id = ELEVEN_MODEL_ID or "eleven_flash_v2_5"
    output_format = ELEVEN_OUTPUT_FORMAT or "mp3_44100_128"
    output_q = urllib.parse.quote(output_format, safe="_")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format={output_q}"

    payload_obj = {
        "text": text,
        "model_id": model_id,
        "voice_settings": dict(voice_settings),
        "seed": int(seed),
        "speed": float(speed),
    }

    def _do_post(payload_in: dict) -> bytes:
        payload = json.dumps(payload_in).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "xi-api-key": ELEVEN_API,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.read()

    first_body = ""
    try:
        return _do_post(payload_obj)
    except urllib.error.HTTPError as e:
        try:
            first_body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            first_body = ""
        # Some models/accounts may reject speed; retry once without it.
        if e.code == 400 and "speed" in payload_obj:
            fallback = dict(payload_obj)
            fallback.pop("speed", None)
            try:
                return _do_post(fallback)
            except urllib.error.HTTPError as e2:
                body2 = ""
                try:
                    body2 = e2.read().decode("utf-8", errors="ignore")
                except Exception:
                    body2 = ""
                raise RuntimeError(f"ElevenLabs error {e2.code}: {body2[:200]}") from e2
        raise RuntimeError(f"ElevenLabs error {e.code}: {first_body[:200]}") from e


def _probe_audio_seconds(path: str) -> float:
    """
    Best-effort duration probe via ffprobe; returns 0.0 on failure.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            return 0.0
        return max(0.0, float((proc.stdout or "").strip() or "0"))
    except Exception:
        return 0.0


class TTSCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._voice_ids_cache: list[str] = []
        self._voice_cache_ts: float = 0.0

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    def _debug(self, ctx: commands.Context, msg: str):
        if not TTS_DEBUG:
            return
        gid = int(getattr(ctx.guild, "id", 0) or 0)
        uid = int(getattr(ctx.author, "id", 0) or 0)
        print(f"[TTS][g={gid}][u={uid}] {msg}")

    async def _ensure_voice_client(
        self,
        ctx: commands.Context,
        target_channel: discord.VoiceChannel,
    ) -> discord.VoiceClient:
        async def _wait_connected(client: discord.VoiceClient, timeout_s: float) -> bool:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if client.is_connected() and client.channel and client.channel.id == target_channel.id:
                    return True
                await asyncio.sleep(0.1)
            return False

        async def _hard_cleanup(client: Optional[discord.VoiceClient]):
            if client is None:
                return
            try:
                if client.is_playing():
                    client.stop()
            except Exception:
                pass
            try:
                await client.disconnect(force=True)
            except Exception:
                pass
            try:
                client.cleanup()
            except Exception:
                pass

        vc = ctx.voice_client
        if vc is not None and not vc.is_connected():
            self._debug(ctx, "stale voice client detected; forcing disconnect")
            await _hard_cleanup(vc)
            vc = None

        if vc is None:
            self._debug(ctx, f"connecting to channel {target_channel.id}")
            vc = await target_channel.connect(timeout=30.0, reconnect=True)
        elif vc.channel is None or vc.channel.id != target_channel.id:
            self._debug(ctx, f"moving from {getattr(vc.channel, 'id', None)} to {target_channel.id}")
            await vc.move_to(target_channel)

        ok = await _wait_connected(vc, timeout_s=15.0)
        if ok:
            self._debug(ctx, "voice client connected")
            return vc

        # One reconnect retry before failing.
        self._debug(ctx, "voice client not connected after wait; retrying reconnect")
        await _hard_cleanup(vc)
        vc = await target_channel.connect(timeout=30.0, reconnect=True)
        ok = await _wait_connected(vc, timeout_s=15.0)
        if ok:
            self._debug(ctx, "voice client connected after retry")
            return vc

        await _hard_cleanup(vc)
        raise RuntimeError("Voice client failed to connect after retry.")

    async def _get_voice_ids(self) -> list[str]:
        now = time.monotonic()
        if self._voice_ids_cache and (now - self._voice_cache_ts) < VOICE_CACHE_TTL_SECONDS:
            return list(self._voice_ids_cache)
        ids = await asyncio.to_thread(_fetch_eleven_voice_ids)
        if not ids:
            fallback = (ELEVEN_VOICE_ID or "21m00Tcm4TlvDq8ikWAM").strip()
            ids = [fallback] if fallback else []
        self._voice_ids_cache = list(ids)
        self._voice_cache_ts = now
        return list(self._voice_ids_cache)

    def _voice_profile_for_user(self, user_id: int) -> tuple[dict, int, float]:
        r = random.Random(int(user_id))
        settings = {
            "stability": round(0.25 + (r.random() * 0.55), 3),
            "similarity_boost": round(0.25 + (r.random() * 0.65), 3),
            "style": round(r.random() * 0.45, 3),
            "use_speaker_boost": bool(r.random() >= 0.5),
        }
        seed = int(user_id % 2_147_483_647)
        speed = round(0.9 + (r.random() * 0.25), 3)
        return settings, seed, speed

    def _voice_id_for_user(self, user_id: int, voice_ids: list[str]) -> str:
        if not voice_ids:
            return (ELEVEN_VOICE_ID or "21m00Tcm4TlvDq8ikWAM").strip()
        idx = int(user_id) % len(voice_ids)
        return str(voice_ids[idx]).strip()

    @commands.command(name="say")
    @commands.cooldown(1, 300.0, commands.BucketType.user)
    async def say(self, ctx: commands.Context, *, payload: Optional[str] = None):
        success = False
        try:
            if payload is None or not payload.strip():
                await ctx.reply(f"Usage: `{ctx.clean_prefix}say {{message}} {{@user}}`")
                return

            mentions = [m for m in ctx.message.mentions if isinstance(m, discord.Member)]
            if not mentions:
                await ctx.reply(f"Mention a target user. Usage: `{ctx.clean_prefix}say {{message}} {{@user}}`")
                return
            target = mentions[-1]
            if target.bot:
                await ctx.reply("Target must be a non-bot user in voice.")
                return

            message = str(payload)
            message = message.replace(f"<@{target.id}>", "").replace(f"<@!{target.id}>", "").strip()
            if not message:
                await ctx.reply("Message cannot be empty.")
                return
            if len(message) > MAX_SAY_CHARS:
                await ctx.reply(f"Message is too long. Max {MAX_SAY_CHARS} characters.")
                return

            if not target.voice or not target.voice.channel:
                await ctx.reply(f"**{target.display_name}** is not in a voice channel.")
                return
            target_channel = target.voice.channel
            if not isinstance(target_channel, discord.VoiceChannel):
                await ctx.reply("Target must be in a standard voice channel (not Stage).")
                return

            me = ctx.guild.me
            if me is None:
                await ctx.reply("Bot voice state unavailable in this guild.")
                return
            perms = target_channel.permissions_for(me)
            if not perms.view_channel or not perms.connect:
                await ctx.reply("I don't have permission to join that channel.")
                return

            lock = self._lock_for(ctx.guild.id)
            if lock.locked():
                await ctx.reply("TTS is already running in this server. Try again shortly.")
                return

            async with lock:
                vc: Optional[discord.VoiceClient] = None
                temp_path = ""
                try:
                    self._debug(ctx, "starting synthesis")
                    voice_ids = await self._get_voice_ids()
                    voice_id = self._voice_id_for_user(ctx.author.id, voice_ids)
                    voice_settings, seed, speed = self._voice_profile_for_user(ctx.author.id)
                    self._debug(
                        ctx,
                        f"voice_id={voice_id} seed={seed} speed={speed} settings={voice_settings}",
                    )
                    audio_bytes = await asyncio.to_thread(
                        _synthesize_eleven_bytes,
                        message,
                        voice_id=voice_id,
                        voice_settings=voice_settings,
                        seed=seed,
                        speed=speed,
                    )
                    if not audio_bytes:
                        await ctx.reply("TTS generation returned empty audio.")
                        return

                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
                        temp_path = f.name
                        f.write(audio_bytes)
                    audio_seconds = await asyncio.to_thread(_probe_audio_seconds, temp_path)
                    self._debug(ctx, f"audio ready path={temp_path} bytes={len(audio_bytes)} duration={audio_seconds:.2f}s")

                    # Join/move only after audio file is fully ready.
                    vc = await self._ensure_voice_client(ctx, target_channel)
                    if not vc.is_connected():
                        raise RuntimeError("Not connected to voice after connect/move.")
                    if vc.is_playing():
                        vc.stop()

                    play_err: list[str] = []

                    def _after(err: Optional[Exception]):
                        if err:
                            play_err.append(str(err))

                    src = discord.FFmpegPCMAudio(temp_path)
                    self._debug(ctx, "starting playback")
                    vc.play(src, after=_after)
                    start_deadline = time.monotonic() + 3.0
                    started = False
                    while time.monotonic() < start_deadline:
                        if play_err:
                            break
                        if vc.is_playing() or vc.is_paused():
                            started = True
                            break
                        await asyncio.sleep(0.1)
                    if not started:
                        if play_err:
                            await ctx.reply(f"TTS playback error: {play_err[0][:160]}")
                        else:
                            await ctx.reply("TTS playback did not start.")
                        self._debug(ctx, f"playback start failed play_err={play_err}")
                        return

                    max_wait = max(8.0, min(300.0, audio_seconds + 10.0))
                    play_start = time.monotonic()
                    while vc.is_playing() or vc.is_paused():
                        if play_err:
                            break
                        if (time.monotonic() - play_start) > max_wait:
                            vc.stop()
                            await ctx.reply("TTS playback timed out.")
                            self._debug(ctx, f"playback timeout max_wait={max_wait:.2f}s")
                            return
                        await asyncio.sleep(0.2)

                    if play_err:
                        await ctx.reply(f"TTS playback error: {play_err[0][:160]}")
                        self._debug(ctx, f"playback after error={play_err[0]}")
                        return
                    await asyncio.sleep(POST_PLAY_IDLE_SECONDS)
                    self._debug(ctx, "playback finished successfully")
                    success = True
                except asyncio.TimeoutError:
                    await ctx.reply("TTS playback timed out.")
                    self._debug(ctx, "asyncio timeout raised")
                except discord.ClientException as e:
                    await ctx.reply(f"TTS failed: ClientException: {e}")
                    self._debug(ctx, f"ClientException={e!r}")
                except (discord.Forbidden, discord.HTTPException):
                    await ctx.reply("Couldn't join or play audio in that channel.")
                    self._debug(ctx, "Forbidden/HTTPException during voice operation")
                except RuntimeError as e:
                    await ctx.reply(str(e))
                    self._debug(ctx, f"RuntimeError={e}")
                except Exception as e:
                    await ctx.reply(f"TTS failed: {type(e).__name__}: {e}")
                    self._debug(ctx, f"Unhandled exception={type(e).__name__}: {e}")
                finally:
                    if vc:
                        try:
                            if vc.is_playing():
                                vc.stop()
                            await vc.disconnect(force=True)
                            self._debug(ctx, "voice client disconnected")
                        except Exception:
                            self._debug(ctx, "voice disconnect cleanup failed")
                            pass
                    if temp_path:
                        try:
                            os.remove(temp_path)
                            self._debug(ctx, "temp file deleted")
                        except OSError:
                            pass
        finally:
            if not success and ctx.command is not None:
                try:
                    ctx.command.reset_cooldown(ctx)
                except Exception:
                    pass
