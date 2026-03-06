from __future__ import annotations

import asyncio
from dataclasses import dataclass
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

from .config import (
    ELEVEN_API,
    ELEVEN_FREE_CATEGORY,
    ELEVEN_FREE_ONLY,
    ELEVEN_FREE_VOICE_LIMIT,
    ELEVEN_MODEL_ID,
    ELEVEN_OUTPUT_FORMAT,
    ELEVEN_VOICE_ID,
)
from .ownership import is_control_user_id
from .voice_support import dave_4017_message, dave_support_status, is_dave_close_4017


MAX_SAY_CHARS = 350
VOICE_CACHE_TTL_SECONDS = 30 * 60
POST_PLAY_IDLE_SECONDS = 1.0
TTS_COOLDOWN_SECONDS = 300.0
TTS_DEBUG = os.getenv("TTS_DEBUG", "true").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class _SayRequest:
    ctx: commands.Context
    payload: str
    requester_id: int
    requester_name: str
    target_id: int
    target_name: str
    message_preview: str
    enqueued_at: float


def _fetch_eleven_voice_ids(*, free_only: bool, free_category: str, free_limit: int) -> list[str]:
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
        if free_only:
            category = str(v.get("category", "")).strip().lower()
            if category != free_category:
                continue
        vid = str(v.get("voice_id", "")).strip()
        if vid:
            out.append(vid)

    if free_only and free_limit > 0:
        return out[:free_limit]
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
        self._guild_queues: dict[int, asyncio.Queue[_SayRequest]] = {}
        self._guild_workers: dict[int, asyncio.Task] = {}
        self._guild_current: dict[int, _SayRequest] = {}
        self._guild_active_vc: dict[int, discord.VoiceClient] = {}
        self._guild_skip_events: dict[int, asyncio.Event] = {}
        self._guild_processing: set[int] = set()
        self._cooldown_enabled: dict[int, bool] = {}
        self._cooldown = commands.CooldownMapping.from_cooldown(
            1,
            TTS_COOLDOWN_SECONDS,
            commands.BucketType.member,
        )
        self._voice_ids_cache: list[str] = []
        self._voice_cache_ts: float = 0.0

    def cog_unload(self):
        for task in self._guild_workers.values():
            task.cancel()
        self._guild_workers.clear()
        self._guild_current.clear()
        self._guild_active_vc.clear()
        self._guild_skip_events.clear()
        self._guild_processing.clear()

    def _queue_for(self, guild_id: int) -> asyncio.Queue[_SayRequest]:
        queue = self._guild_queues.get(guild_id)
        if queue is None:
            queue = asyncio.Queue()
            self._guild_queues[guild_id] = queue
        return queue

    def _cooldown_is_enabled(self, guild_id: int) -> bool:
        return self._cooldown_enabled.get(guild_id, True)

    def _skip_event_for(self, guild_id: int) -> asyncio.Event:
        ev = self._guild_skip_events.get(guild_id)
        if ev is None:
            ev = asyncio.Event()
            self._guild_skip_events[guild_id] = ev
        return ev

    def _ensure_worker(self, guild_id: int):
        task = self._guild_workers.get(guild_id)
        if task and not task.done():
            return
        queue = self._queue_for(guild_id)
        self._guild_workers[guild_id] = asyncio.create_task(self._worker_loop(guild_id, queue))

    async def _worker_loop(self, guild_id: int, queue: asyncio.Queue[_SayRequest]):
        while True:
            req = await queue.get()
            try:
                self._guild_processing.add(guild_id)
                self._skip_event_for(guild_id).clear()
                self._guild_current[guild_id] = req
                await self._process_say(req)
            except Exception as e:
                self._debug(req.ctx, f"worker unhandled exception={type(e).__name__}: {e}")
            finally:
                self._guild_current.pop(guild_id, None)
                self._guild_active_vc.pop(guild_id, None)
                self._skip_event_for(guild_id).clear()
                self._guild_processing.discard(guild_id)
                queue.task_done()

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
            try:
                vc = await target_channel.connect(
                    timeout=30.0,
                    reconnect=True,
                    self_deaf=True,
                    self_mute=False,
                )
            except discord.ConnectionClosed as e:
                if is_dave_close_4017(e):
                    raise RuntimeError(dave_4017_message()) from e
                raise
        elif vc.channel is None or vc.channel.id != target_channel.id:
            self._debug(ctx, f"moving from {getattr(vc.channel, 'id', None)} to {target_channel.id}")
            try:
                await vc.move_to(target_channel)
            except discord.ConnectionClosed as e:
                if is_dave_close_4017(e):
                    raise RuntimeError(dave_4017_message()) from e
                raise

        ok = await _wait_connected(vc, timeout_s=15.0)
        if ok:
            self._debug(ctx, "voice client connected")
            return vc

        # One reconnect retry before failing.
        self._debug(ctx, "voice client not connected after wait; retrying reconnect")
        await _hard_cleanup(vc)
        try:
            vc = await target_channel.connect(
                timeout=30.0,
                reconnect=True,
                self_deaf=True,
                self_mute=False,
            )
        except discord.ConnectionClosed as e:
            if is_dave_close_4017(e):
                raise RuntimeError(dave_4017_message()) from e
            raise
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
        ids = await asyncio.to_thread(
            _fetch_eleven_voice_ids,
            free_only=bool(ELEVEN_FREE_ONLY),
            free_category=str(ELEVEN_FREE_CATEGORY or "premade").strip().lower(),
            free_limit=max(0, int(ELEVEN_FREE_VOICE_LIMIT)),
        )
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

    def _is_tts_admin(self, ctx: commands.Context) -> bool:
        perms = getattr(ctx.author, "guild_permissions", None)
        is_admin = bool(perms and perms.administrator)
        return bool(is_admin or is_control_user_id(ctx.guild, ctx.author.id))

    def _extract_target_and_message(self, ctx: commands.Context, payload: Optional[str]) -> tuple[discord.Member, str]:
        if payload is None or not payload.strip():
            raise ValueError(f"Usage: `{ctx.clean_prefix}say {{message}} {{@user}}`")

        mentions = [m for m in ctx.message.mentions if isinstance(m, discord.Member)]
        if not mentions:
            raise ValueError(f"Mention a target user. Usage: `{ctx.clean_prefix}say {{message}} {{@user}}`")

        target = mentions[-1]
        if target.bot:
            raise ValueError("Target must be a non-bot user in voice.")

        message = str(payload)
        message = message.replace(f"<@{target.id}>", "").replace(f"<@!{target.id}>", "").strip()
        if not message:
            raise ValueError("Message cannot be empty.")
        if len(message) > MAX_SAY_CHARS:
            raise ValueError(f"Message is too long. Max {MAX_SAY_CHARS} characters.")

        return target, message

    @commands.command(name="ttscooldown", aliases=["saycooldown"])
    async def tts_cooldown(self, ctx: commands.Context, mode: Optional[str] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if not self._is_tts_admin(ctx):
            await ctx.reply("You don't have permission to manage TTS cooldown.")
            return

        current = self._cooldown_is_enabled(ctx.guild.id)
        token = (mode or "toggle").strip().lower()

        if token in {"toggle", "t"}:
            new_state = not current
        elif token in {"on", "enable", "enabled", "true", "1"}:
            new_state = True
        elif token in {"off", "disable", "disabled", "false", "0"}:
            new_state = False
        elif token in {"status", "show"}:
            await ctx.reply(
                f"TTS cooldown is currently **{'ON' if current else 'OFF'}** "
                f"(when ON: per-user, per-server, {int(TTS_COOLDOWN_SECONDS)}s)."
            )
            return
        else:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}ttscooldown [on|off|toggle|status]`")
            return

        self._cooldown_enabled[ctx.guild.id] = new_state
        self._cooldown = commands.CooldownMapping.from_cooldown(
            1,
            TTS_COOLDOWN_SECONDS,
            commands.BucketType.member,
        )
        await ctx.reply(
            f"TTS cooldown is now **{'ON' if new_state else 'OFF'}** "
            f"(when ON: per-user, per-server, {int(TTS_COOLDOWN_SECONDS)}s)."
        )

    @commands.command(name="ttsqueue", aliases=["sayqueue"])
    async def tts_queue(self, ctx: commands.Context, action: Optional[str] = None, count: Optional[int] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if not self._is_tts_admin(ctx):
            await ctx.reply("You don't have permission to manage the TTS queue.")
            return

        queue = self._queue_for(ctx.guild.id)
        token = (action or "status").strip().lower()

        if token in {"status", "show", "list"}:
            pending = queue.qsize()
            processing = ctx.guild.id in self._guild_processing
            current = self._guild_current.get(ctx.guild.id)
            cooldown = "ON" if self._cooldown_is_enabled(ctx.guild.id) else "OFF"

            lines = [
                f"**TTS Queue ({ctx.guild.name})**",
                f"Cooldown: **{cooldown}** ({int(TTS_COOLDOWN_SECONDS)}s, per-user per-server when ON)",
                f"Now processing: **{'yes' if processing else 'no'}**",
                f"Pending: **{pending}**",
            ]

            if current is not None:
                lines.append(
                    f"Current: **{current.requester_name} -> {current.target_name}**: {current.message_preview}"
                )

            if pending > 0:
                snapshot = list(getattr(queue, "_queue", []))
                preview = snapshot[:5]
                for i, req in enumerate(preview, start=1):
                    lines.append(f"`{i}.` {req.requester_name} -> {req.target_name}: {req.message_preview}")
                if pending > 5:
                    lines.append(f"...and {pending - 5} more queued request(s).")

            await ctx.reply("\n".join(lines))
            return

        if token == "skip":
            if count is not None:
                await ctx.reply(f"Usage: `{ctx.clean_prefix}ttsqueue skip`")
                return
            if ctx.guild.id not in self._guild_processing:
                await ctx.reply("No TTS request is currently processing.")
                return

            self._skip_event_for(ctx.guild.id).set()
            vc = self._guild_active_vc.get(ctx.guild.id)
            if vc is not None:
                try:
                    if vc.is_playing() or vc.is_paused():
                        vc.stop()
                except Exception:
                    pass

            await ctx.reply("Skip requested for current TTS request.")
            return

        if token == "clear":
            if count is not None and count < 1:
                await ctx.reply("Count must be >= 1 when provided.")
                return
            target_count = queue.qsize() if count is None else min(queue.qsize(), int(count))
            cleared = 0
            for _ in range(target_count):
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    queue.task_done()
                    cleared += 1
            processing = ctx.guild.id in self._guild_processing
            suffix = " One request is currently processing." if processing else " No request is currently processing."
            await ctx.reply(f"Cleared **{cleared}** queued TTS request(s).{suffix}")
            return

        await ctx.reply(f"Usage: `{ctx.clean_prefix}ttsqueue [status|skip|clear [count]]`")

    @commands.command(name="say")
    async def say(self, ctx: commands.Context, *, payload: Optional[str] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        dave_ok, dave_reason = dave_support_status()
        if not dave_ok:
            await ctx.reply(f"Voice unavailable: {dave_reason}")
            return

        try:
            target, message = self._extract_target_and_message(ctx, payload)
        except ValueError as e:
            await ctx.reply(str(e))
            return

        if self._cooldown_is_enabled(ctx.guild.id):
            bucket = self._cooldown.get_bucket(ctx.message)
            retry_after = bucket.update_rate_limit() if bucket else None
            if retry_after:
                await ctx.reply(f"Slow down-try again in {retry_after:.1f}s.")
                return

        queue = self._queue_for(ctx.guild.id)
        preview = message if len(message) <= 96 else f"{message[:93]}..."
        await queue.put(
            _SayRequest(
                ctx=ctx,
                payload=str(payload or ""),
                requester_id=int(ctx.author.id),
                requester_name=str(ctx.author.display_name),
                target_id=int(target.id),
                target_name=str(target.display_name),
                message_preview=preview,
                enqueued_at=time.monotonic(),
            )
        )
        self._ensure_worker(ctx.guild.id)

        position = queue.qsize() + (1 if ctx.guild.id in self._guild_processing else 0)
        if position <= 1:
            await ctx.reply("Queued TTS request. Starting shortly.")
        else:
            await ctx.reply(f"Queued TTS request at position **{position}**.")

    async def _process_say(self, req: _SayRequest):
        ctx = req.ctx
        payload = req.payload
        try:
            if ctx.guild is None:
                return
            guild_id = int(ctx.guild.id)
            skip_event = self._skip_event_for(guild_id)

            dave_ok, dave_reason = dave_support_status()
            if not dave_ok:
                await ctx.reply(f"Voice unavailable: {dave_reason}")
                return

            target, message = self._extract_target_and_message(ctx, payload)

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

            vc: Optional[discord.VoiceClient] = None
            temp_path = ""
            try:
                await ctx.send(f"{req.requester_name} says: {message} to {target.display_name}.")
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
                if skip_event.is_set():
                    await ctx.reply("Current TTS request was skipped.")
                    return
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
                self._guild_active_vc[guild_id] = vc
                if not vc.is_connected():
                    raise RuntimeError("Not connected to voice after connect/move.")
                if skip_event.is_set():
                    await ctx.reply("Current TTS request was skipped.")
                    return
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
                    if skip_event.is_set():
                        vc.stop()
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
                    if skip_event.is_set():
                        vc.stop()
                        await ctx.reply("Current TTS request was skipped.")
                        self._debug(ctx, "playback skipped by admin")
                        return
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
            except asyncio.TimeoutError:
                await ctx.reply("TTS playback timed out.")
                self._debug(ctx, "asyncio timeout raised")
            except discord.ConnectionClosed as e:
                if is_dave_close_4017(e):
                    await ctx.reply(dave_4017_message())
                else:
                    await ctx.reply(f"TTS failed: voice websocket closed ({e.code}).")
                self._debug(ctx, f"ConnectionClosed={getattr(e, 'code', None)}")
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
                        try:
                            vc.cleanup()  # CRITICAL
                        except Exception:
                            pass
                        self._debug(ctx, "voice client disconnected")
                    except Exception:
                        self._debug(ctx, "voice disconnect cleanup failed")
                        pass
                self._guild_active_vc.pop(guild_id, None)
                if temp_path:
                    try:
                        os.remove(temp_path)
                        self._debug(ctx, "temp file deleted")
                    except OSError:
                        pass
        except ValueError as e:
            await ctx.reply(str(e))
