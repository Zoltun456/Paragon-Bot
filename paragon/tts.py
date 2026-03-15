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
    ELEVEN_MODEL_ID,
    ELEVEN_OUTPUT_FORMAT,
    ELEVEN_VOICE_ID,
)
from .ownership import is_control_user_id
from .storage import _gdict, save_data
from .user_settings import get_user_tts_profile, set_user_tts_profile
from .voice_runtime import cleanup_voice_client, ensure_voice_client
from .voice_support import dave_4017_message, dave_support_status, is_dave_close_4017


MAX_SAY_CHARS = 350
VOICE_CACHE_TTL_SECONDS = 30 * 60
MODEL_CACHE_TTL_SECONDS = 30 * 60
POST_PLAY_IDLE_SECONDS = 1.0
TTS_COOLDOWN_SECONDS = 300.0
TTS_DEBUG = os.getenv("TTS_DEBUG", "true").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_MANUAL_STABILITY = 0.5
DEFAULT_MANUAL_SIMILARITY_BOOST = 0.5
DEFAULT_MANUAL_STYLE = 0.1
DEFAULT_MANUAL_USE_SPEAKER_BOOST = True
DEFAULT_MANUAL_SPEED = 1.0
TTS_TAG_GROUPS: list[tuple[str, list[str]]] = [
    (
        "Emotion Tags",
        [
            "[happy]",
            "[sad]",
            "[angry]",
            "[excited]",
            "[curious]",
            "[sarcastic]",
            "[mischievously]",
            "[crying]",
            "[tired]",
            "[awe]",
        ],
    ),
    (
        "Style/Delivery Tags",
        [
            "[whispers]",
            "[shouting]",
            "[calmly]",
            "[dramatic tone]",
            "[slowly]",
            "[rushed]",
            "[drawn out]",
            "[interrupting]",
            "[overlapping]",
            "[narrating]",
        ],
    ),
    (
        "Non-Verbal/SFX Tags",
        [
            "[laughs]",
            "[laughs harder]",
            "[starts laughing]",
            "[chuckles]",
            "[wheezing]",
            "[sighs]",
            "[exhales]",
            "[snorts]",
            "[swallows]",
            "[gulps]",
            "[coughs]",
            "[applause]",
            "[clapping]",
            "[gunshot]",
            "[explosion]",
        ],
    ),
    (
        "Pacing Tags",
        [
            "[pause]",
            "[short pause]",
            "[long pause]",
        ],
    ),
]


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


def _fetch_eleven_voice_catalog() -> list[tuple[str, str]]:
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
    out: list[tuple[str, str]] = []
    for v in voices:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("voice_id", "")).strip()
        if not vid:
            continue
        name = str(v.get("name", "")).strip() or vid
        out.append((vid, name))
    return out


def _fetch_eleven_models() -> list[tuple[str, str]]:
    if not ELEVEN_API:
        return []
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/models",
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
    if not isinstance(payload, list):
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("can_do_text_to_speech", False)):
            continue
        model_id = str(item.get("model_id", "")).strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        name = str(item.get("name", "")).strip() or model_id
        out.append((model_id, name))
    return out


def _synthesize_eleven_bytes(
    text: str,
    *,
    voice_id: str,
    model_id: str,
    voice_settings: dict,
    seed: int,
    speed: float,
) -> bytes:
    if not ELEVEN_API:
        raise RuntimeError("ELEVEN_API is not configured.")

    voice_id = (voice_id or ELEVEN_VOICE_ID or "21m00Tcm4TlvDq8ikWAM").strip()
    model_id = (model_id or ELEVEN_MODEL_ID or "eleven_flash_v2_5").strip()
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
        self._guild_disconnect_on_idle: dict[int, bool] = {}
        self._guild_processing: set[int] = set()
        self._cooldown_enabled: dict[int, bool] = {}
        self._cooldown = commands.CooldownMapping.from_cooldown(
            1,
            TTS_COOLDOWN_SECONDS,
            commands.BucketType.member,
        )
        self._voice_catalog_cache: list[tuple[str, str]] = []
        self._voice_name_cache: dict[str, str] = {}
        self._voice_cache_ts: float = 0.0
        self._voice_profile_locks: dict[int, asyncio.Lock] = {}
        self._model_catalog_cache: list[tuple[str, str]] = []
        self._model_name_cache: dict[str, str] = {}
        self._model_cache_ts: float = 0.0

    def cog_unload(self):
        for task in self._guild_workers.values():
            task.cancel()
        self._guild_workers.clear()
        self._guild_current.clear()
        self._guild_active_vc.clear()
        self._guild_skip_events.clear()
        self._guild_disconnect_on_idle.clear()
        self._guild_processing.clear()
        self._voice_profile_locks.clear()
        self._model_catalog_cache.clear()
        self._model_name_cache.clear()

    def _queue_for(self, guild_id: int) -> asyncio.Queue[_SayRequest]:
        queue = self._guild_queues.get(guild_id)
        if queue is None:
            queue = asyncio.Queue()
            self._guild_queues[guild_id] = queue
        return queue

    def _cooldown_is_enabled(self, guild_id: int) -> bool:
        return self._cooldown_enabled.get(guild_id, False)

    def _skip_event_for(self, guild_id: int) -> asyncio.Event:
        ev = self._guild_skip_events.get(guild_id)
        if ev is None:
            ev = asyncio.Event()
            self._guild_skip_events[guild_id] = ev
        return ev

    def should_keep_voice_connected(self, guild_id: int) -> bool:
        queue = self._guild_queues.get(guild_id)
        pending = queue.qsize() if queue is not None else 0
        if guild_id in self._guild_processing or pending > 0:
            return True
        return not self._guild_disconnect_on_idle.get(guild_id, True)

    def _clear_pending_queue(self, guild_id: int) -> int:
        queue = self._queue_for(guild_id)
        cleared = 0
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                queue.task_done()
                cleared += 1
        return cleared

    async def shutdown_guild(self, guild_id: int, *, clear_queue: bool = True, disconnect: bool = False) -> None:
        if clear_queue:
            self._clear_pending_queue(guild_id)
        self._skip_event_for(guild_id).set()

        vc = self._guild_active_vc.get(guild_id)
        if vc is None:
            guild = self.bot.get_guild(guild_id)
            vc = getattr(guild, "voice_client", None) if guild is not None else None
        if vc is not None:
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                pass
            if disconnect:
                await cleanup_voice_client(vc)
        if disconnect:
            self._guild_disconnect_on_idle.pop(guild_id, None)

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
                self._skip_event_for(guild_id).clear()
                self._guild_processing.discard(guild_id)
                queue.task_done()
                if queue.empty():
                    await self._maybe_disconnect_when_idle(guild_id)

    async def _maybe_disconnect_when_idle(self, guild_id: int) -> None:
        if not self._guild_disconnect_on_idle.get(guild_id, False):
            return
        if guild_id in self._guild_processing:
            return
        queue = self._guild_queues.get(guild_id)
        if queue is not None and queue.qsize() > 0:
            return

        playback_cog = self.bot.get_cog("PlaybackCog")
        if playback_cog is not None and hasattr(playback_cog, "should_keep_voice_connected"):
            try:
                if bool(playback_cog.should_keep_voice_connected(guild_id)):
                    return
            except Exception:
                pass

        guild = self.bot.get_guild(guild_id)
        vc = self._guild_active_vc.get(guild_id)
        if vc is None and guild is not None:
            vc = guild.voice_client
        if vc is None:
            self._guild_disconnect_on_idle.pop(guild_id, None)
            return
        try:
            if vc.is_playing() or vc.is_paused():
                return
        except Exception:
            pass
        await cleanup_voice_client(vc)
        self._guild_active_vc.pop(guild_id, None)
        self._guild_disconnect_on_idle.pop(guild_id, None)

    def _debug(self, ctx: commands.Context, msg: str):
        if not TTS_DEBUG:
            return
        gid = int(getattr(ctx.guild, "id", 0) or 0)
        uid = int(getattr(ctx.author, "id", 0) or 0)
        print(f"[TTS][g={gid}][u={uid}] {msg}")

    def _voice_profile_lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._voice_profile_locks.get(int(user_id))
        if lock is None:
            lock = asyncio.Lock()
            self._voice_profile_locks[int(user_id)] = lock
        return lock

    def _voice_name_for(self, voice_id: str) -> str:
        vid = str(voice_id or "").strip()
        if not vid:
            return "Unknown Voice"
        return self._voice_name_cache.get(vid, vid)

    def _model_name_for(self, model_id: str) -> str:
        mid = str(model_id or "").strip()
        if not mid:
            return "Unknown Model"
        return self._model_name_cache.get(mid, mid)

    def _guild_settings(self, guild_id: int) -> dict:
        g = _gdict(guild_id)
        st = g.get("settings")
        if not isinstance(st, dict):
            st = {}
            g["settings"] = st
        if "inactive_loss_enabled" not in st:
            st["inactive_loss_enabled"] = True
        return st

    def _tts_model_for_guild(self, guild_id: int) -> str:
        st = self._guild_settings(guild_id)
        selected = str(st.get("tts_model_id", "")).strip()
        if selected:
            return selected
        return (ELEVEN_MODEL_ID or "eleven_flash_v2_5").strip()

    async def _ensure_voice_client(
        self,
        ctx: commands.Context,
        target_channel: discord.VoiceChannel,
    ) -> discord.VoiceClient:
        return await ensure_voice_client(ctx, target_channel, debug=lambda msg: self._debug(ctx, msg))

    async def _get_voice_catalog(self) -> list[tuple[str, str]]:
        now = time.monotonic()
        if self._voice_catalog_cache and (now - self._voice_cache_ts) < VOICE_CACHE_TTL_SECONDS:
            return list(self._voice_catalog_cache)

        catalog = await asyncio.to_thread(_fetch_eleven_voice_catalog)
        deduped: list[tuple[str, str]] = []
        seen: set[str] = set()
        for voice_id, voice_name in catalog:
            vid = str(voice_id or "").strip()
            if not vid or vid in seen:
                continue
            seen.add(vid)
            deduped.append((vid, str(voice_name or vid)))

        if not deduped:
            fallback = (ELEVEN_VOICE_ID or "21m00Tcm4TlvDq8ikWAM").strip()
            if fallback:
                deduped = [(fallback, fallback)]

        self._voice_catalog_cache = list(deduped)
        self._voice_name_cache = {voice_id: voice_name for voice_id, voice_name in deduped}
        self._voice_cache_ts = now
        return list(self._voice_catalog_cache)

    async def _get_voice_ids(self) -> list[str]:
        catalog = await self._get_voice_catalog()
        return [voice_id for voice_id, _ in catalog]

    async def _get_model_catalog(self) -> list[tuple[str, str]]:
        now = time.monotonic()
        if self._model_catalog_cache and (now - self._model_cache_ts) < MODEL_CACHE_TTL_SECONDS:
            return list(self._model_catalog_cache)

        catalog = await asyncio.to_thread(_fetch_eleven_models)
        deduped: list[tuple[str, str]] = []
        seen: set[str] = set()
        for model_id, model_name in catalog:
            mid = str(model_id or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            deduped.append((mid, str(model_name or mid)))

        fallback = (ELEVEN_MODEL_ID or "eleven_flash_v2_5").strip()
        if fallback and fallback not in seen:
            deduped.append((fallback, fallback))

        self._model_catalog_cache = list(deduped)
        self._model_name_cache = {model_id: model_name for model_id, model_name in deduped}
        self._model_cache_ts = now
        return list(self._model_catalog_cache)

    def _build_random_voice_profile(self, voice_ids: list[str], *, exclude_voice_id: str = "") -> dict:
        rng = random.SystemRandom()
        choices = [str(v).strip() for v in voice_ids if str(v).strip()]
        if exclude_voice_id and len(choices) > 1:
            filtered = [v for v in choices if v != exclude_voice_id]
            if filtered:
                choices = filtered

        fallback = (ELEVEN_VOICE_ID or "21m00Tcm4TlvDq8ikWAM").strip()
        voice_id = rng.choice(choices) if choices else fallback
        settings = {
            "stability": round(0.25 + (rng.random() * 0.55), 3),
            "similarity_boost": round(0.25 + (rng.random() * 0.65), 3),
            "style": round(rng.random() * 0.45, 3),
            "use_speaker_boost": bool(rng.random() >= 0.5),
        }
        seed = int(rng.randint(1, 2_147_483_646))
        speed = round(0.9 + (rng.random() * 0.25), 3)
        return {
            "voice_id": str(voice_id or fallback).strip(),
            "settings": settings,
            "seed": seed,
            "speed": speed,
            "assigned_at_unix": int(time.time()),
        }

    def _build_manual_voice_profile(
        self,
        *,
        voice_id: str,
        stability: Optional[float] = None,
        similarity_boost: Optional[float] = None,
        style: Optional[float] = None,
        use_speaker_boost: Optional[bool] = None,
        speed: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> dict:
        rng = random.SystemRandom()
        out_stability = DEFAULT_MANUAL_STABILITY if stability is None else float(stability)
        out_similarity = DEFAULT_MANUAL_SIMILARITY_BOOST if similarity_boost is None else float(similarity_boost)
        out_style = DEFAULT_MANUAL_STYLE if style is None else float(style)
        out_speaker_boost = (
            DEFAULT_MANUAL_USE_SPEAKER_BOOST if use_speaker_boost is None else bool(use_speaker_boost)
        )
        out_speed = DEFAULT_MANUAL_SPEED if speed is None else float(speed)
        out_seed = int(rng.randint(1, 2_147_483_646) if seed is None else int(seed))

        return {
            "voice_id": str(voice_id or "").strip(),
            "settings": {
                "stability": max(0.0, min(1.0, out_stability)),
                "similarity_boost": max(0.0, min(1.0, out_similarity)),
                "style": max(0.0, min(1.0, out_style)),
                "use_speaker_boost": out_speaker_boost,
            },
            "seed": out_seed,
            "speed": out_speed,
            "assigned_at_unix": int(time.time()),
        }

    def _parse_bool_token(self, token: str) -> Optional[bool]:
        t = str(token or "").strip().lower()
        if t in {"1", "true", "yes", "y", "on"}:
            return True
        if t in {"0", "false", "no", "n", "off"}:
            return False
        return None

    def _normalize_voice_profile(self, raw: dict) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None
        voice_id = str(raw.get("voice_id", "")).strip()
        if not voice_id:
            return None

        settings_in = raw.get("settings")
        if not isinstance(settings_in, dict):
            return None
        try:
            settings = {
                "stability": float(settings_in.get("stability", 0.5)),
                "similarity_boost": float(settings_in.get("similarity_boost", 0.5)),
                "style": float(settings_in.get("style", 0.1)),
                "use_speaker_boost": bool(settings_in.get("use_speaker_boost", True)),
            }
        except Exception:
            return None
        settings["stability"] = max(0.0, min(1.0, settings["stability"]))
        settings["similarity_boost"] = max(0.0, min(1.0, settings["similarity_boost"]))
        settings["style"] = max(0.0, min(1.0, settings["style"]))

        try:
            seed = int(raw.get("seed", 1))
        except Exception:
            return None
        if seed <= 0:
            return None

        try:
            speed = float(raw.get("speed", 1.0))
        except Exception:
            return None
        if speed <= 0:
            return None

        assigned_at = int(raw.get("assigned_at_unix", int(time.time())))
        return {
            "voice_id": voice_id,
            "settings": settings,
            "seed": seed,
            "speed": speed,
            "assigned_at_unix": assigned_at,
        }

    async def _get_or_create_voice_profile(self, user_id: int, voice_ids: list[str]) -> dict:
        lock = self._voice_profile_lock_for(user_id)
        async with lock:
            raw = await asyncio.to_thread(get_user_tts_profile, int(user_id))
            profile = self._normalize_voice_profile(raw)
            if profile is not None:
                return profile

            profile = self._build_random_voice_profile(voice_ids)
            await asyncio.to_thread(set_user_tts_profile, int(user_id), profile)
            return profile

    async def _reroll_voice_profile(self, user_id: int, voice_ids: list[str]) -> dict:
        lock = self._voice_profile_lock_for(user_id)
        async with lock:
            raw = await asyncio.to_thread(get_user_tts_profile, int(user_id))
            current_voice_id = ""
            if isinstance(raw, dict):
                current_voice_id = str(raw.get("voice_id", "")).strip()

            profile = self._build_random_voice_profile(voice_ids, exclude_voice_id=current_voice_id)
            await asyncio.to_thread(set_user_tts_profile, int(user_id), profile)
            return profile

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

    @commands.command(name="ttsmodel", aliases=["saymodel"])
    async def tts_model(self, ctx: commands.Context, model_id: Optional[str] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if not self._is_tts_admin(ctx):
            await ctx.reply("You don't have permission to manage the TTS model.")
            return

        catalog = await self._get_model_catalog()
        available_ids = {mid for mid, _ in catalog}
        current = self._tts_model_for_guild(int(ctx.guild.id))

        if model_id is None or not str(model_id).strip():
            lines = [
                f"Current TTS model: **{self._model_name_for(current)}** (`{current}`)",
                f"Usage: `{ctx.clean_prefix}ttsmodel <model_id>`",
                "Available models:",
            ]
            for mid, name in catalog:
                marker = " (current)" if mid == current else ""
                lines.append(f"- `{mid}` - {name}{marker}")
            await ctx.reply("\n".join(lines))
            return

        target = str(model_id).strip()
        if target not in available_ids:
            await ctx.reply(
                f"Unknown TTS model `{target}`. "
                f"Run `{ctx.clean_prefix}ttsmodel` to list currently available models."
            )
            return

        st = self._guild_settings(int(ctx.guild.id))
        st["tts_model_id"] = target
        await save_data()
        await ctx.reply(f"TTS model set to **{self._model_name_for(target)}** (`{target}`) for this server.")

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
            if count is None:
                cleared = self._clear_pending_queue(ctx.guild.id)
            else:
                target_count = min(queue.qsize(), int(count))
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

    @commands.command(name="tts", aliases=["ttstags", "ttshelp"])
    async def tts_tags(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        model_id = self._tts_model_for_guild(int(ctx.guild.id))
        lines = [
            "**ElevenLabs TTS Tag Guide**",
            f"Current server model: `{model_id}`",
            "Use tags directly in your `say` message, e.g. `[laughs] that was wild!`",
            "Tags work best on expressive models (especially `eleven_v3`).",
            "These are common tags; expressive prompting supports natural variation.",
            "",
        ]

        for title, tags in TTS_TAG_GROUPS:
            lines.append(f"**{title}:**")
            lines.append(", ".join(tags))
            lines.append("")

        await ctx.reply("\n".join(lines).strip())

    @commands.command(name="rerollvoice", aliases=["ttsreroll", "voicereroll"])
    async def reroll_voice(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        if target is None:
            target = ctx.author

        if target.bot:
            await ctx.reply("Target must be a non-bot user.")
            return

        if target.id != ctx.author.id and not self._is_tts_admin(ctx):
            await ctx.reply("You don't have permission to reroll another user's TTS voice.")
            return

        voice_ids = await self._get_voice_ids()
        profile = await self._reroll_voice_profile(int(target.id), voice_ids)
        voice_id = str(profile.get("voice_id", "")).strip()
        voice_name = self._voice_name_for(voice_id)

        if target.id == ctx.author.id:
            await ctx.reply(f"Your TTS voice was rerolled to **{voice_name}**.")
        else:
            await ctx.reply(f"Rerolled TTS voice for **{target.display_name}** to **{voice_name}**.")

    @commands.command(name="setvoice", aliases=["ttsvoice", "voiceid"])
    async def set_voice(
        self,
        ctx: commands.Context,
        voice_id: str,
        stability: Optional[float] = None,
        similarity_boost: Optional[float] = None,
        style: Optional[float] = None,
        use_speaker_boost: Optional[str] = None,
        speed: Optional[float] = None,
        seed: Optional[int] = None,
    ):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        await self._get_voice_catalog()
        voice_id = str(voice_id or "").strip()
        if not voice_id:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}setvoice <voice_id> [stability] [similarity_boost] [style] [use_speaker_boost] [speed] [seed]`")
            return

        if stability is not None and not (0.0 <= float(stability) <= 1.0):
            await ctx.reply("`stability` must be between 0.0 and 1.0.")
            return
        if similarity_boost is not None and not (0.0 <= float(similarity_boost) <= 1.0):
            await ctx.reply("`similarity_boost` must be between 0.0 and 1.0.")
            return
        if style is not None and not (0.0 <= float(style) <= 1.0):
            await ctx.reply("`style` must be between 0.0 and 1.0.")
            return
        if speed is not None and float(speed) <= 0.0:
            await ctx.reply("`speed` must be > 0.")
            return
        if seed is not None and int(seed) <= 0:
            await ctx.reply("`seed` must be a positive integer.")
            return

        parsed_speaker_boost: Optional[bool] = None
        if use_speaker_boost is not None:
            parsed_speaker_boost = self._parse_bool_token(use_speaker_boost)
            if parsed_speaker_boost is None:
                await ctx.reply("`use_speaker_boost` must be one of: true/false, yes/no, on/off, 1/0.")
                return

        profile = self._build_manual_voice_profile(
            voice_id=voice_id,
            stability=stability,
            similarity_boost=similarity_boost,
            style=style,
            use_speaker_boost=parsed_speaker_boost,
            speed=speed,
            seed=seed,
        )
        normalized = self._normalize_voice_profile(profile)
        if normalized is None:
            await ctx.reply("Unable to apply that voice profile. Please verify values and try again.")
            return

        await asyncio.to_thread(set_user_tts_profile, int(ctx.author.id), normalized)
        voice_name = self._voice_name_for(voice_id)
        s = normalized["settings"]
        await ctx.reply(
            "Saved your TTS voice profile: "
            f"**{voice_name}** (`{voice_id}`) | "
            f"stability={s['stability']:.3f}, "
            f"similarity_boost={s['similarity_boost']:.3f}, "
            f"style={s['style']:.3f}, "
            f"use_speaker_boost={str(bool(s['use_speaker_boost'])).lower()}, "
            f"speed={float(normalized['speed']):.3f}, "
            f"seed={int(normalized['seed'])}."
        )

    async def _process_say(self, req: _SayRequest):
        ctx = req.ctx
        payload = req.payload
        try:
            if ctx.guild is None:
                return
            guild_id = int(ctx.guild.id)
            skip_event = self._skip_event_for(guild_id)
            playback_cog = self.bot.get_cog("PlaybackCog")

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
            playback_suspended = False
            try:
                existing_vc = ctx.voice_client if ctx.voice_client and ctx.voice_client.is_connected() else None
                if guild_id not in self._guild_disconnect_on_idle:
                    self._guild_disconnect_on_idle[guild_id] = existing_vc is None

                await ctx.send(f"{req.requester_name} says: {message} to {target.display_name}.")
                self._debug(ctx, "starting synthesis")
                voice_ids = await self._get_voice_ids()
                voice_profile = await self._get_or_create_voice_profile(int(ctx.author.id), voice_ids)
                voice_id = str(voice_profile.get("voice_id", "")).strip()
                voice_settings = dict(voice_profile.get("settings", {}))
                seed = int(voice_profile.get("seed", 1))
                speed = float(voice_profile.get("speed", 1.0))
                model_id = self._tts_model_for_guild(int(ctx.guild.id))
                self._debug(
                    ctx,
                    f"voice_id={voice_id} model_id={model_id} seed={seed} speed={speed} settings={voice_settings}",
                )
                audio_bytes = await asyncio.to_thread(
                    _synthesize_eleven_bytes,
                    message,
                    voice_id=voice_id,
                    model_id=model_id,
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

                if playback_cog is not None and hasattr(playback_cog, "suspend_for_tts"):
                    playback_suspended = bool(await playback_cog.suspend_for_tts(ctx, target_channel))

                vc = await self._ensure_voice_client(ctx, target_channel)
                self._guild_active_vc[guild_id] = vc
                if not vc.is_connected():
                    raise RuntimeError("Not connected to voice after connect/move.")
                if skip_event.is_set():
                    await ctx.reply("Current TTS request was skipped.")
                    return

                busy_deadline = time.monotonic() + 10.0
                while (vc.is_playing() or vc.is_paused()) and time.monotonic() < busy_deadline:
                    if skip_event.is_set():
                        await ctx.reply("Current TTS request was skipped.")
                        return
                    await asyncio.sleep(0.1)
                if vc.is_playing() or vc.is_paused():
                    raise RuntimeError("Voice client stayed busy too long. Please try again in a moment.")

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
                if playback_suspended and playback_cog is not None and hasattr(playback_cog, "restore_after_tts"):
                    try:
                        await playback_cog.restore_after_tts(guild_id)
                    except Exception as e:
                        self._debug(ctx, f"restore_after_tts failed={type(e).__name__}: {e}")
                self._guild_active_vc.pop(guild_id, None)
                if temp_path:
                    try:
                        os.remove(temp_path)
                        self._debug(ctx, "temp file deleted")
                    except OSError:
                        pass
        except ValueError as e:
            await ctx.reply(str(e))
