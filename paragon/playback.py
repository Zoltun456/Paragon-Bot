from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import discord
from discord.ext import commands

from .config import PLAYBACK_VOLUME, YTDLP_COOKIE_FILE, YTDLP_COOKIES_FROM_BROWSER
from .emojis import EMOJI_BLACK_RIGHT_POINTING_DOUBLE_TRIANGLE
from .ownership import is_control_user_id
from .stats_store import record_game_fields
from .storage import save_data
from .voice_runtime import cleanup_voice_client, ensure_voice_client
from .voice_support import dave_support_status

try:
    from yt_dlp import YoutubeDL
except Exception:
    YoutubeDL = None


FAST_FORWARD_EMOJI = EMOJI_BLACK_RIGHT_POINTING_DOUBLE_TRIANGLE
MAX_PLAY_DURATION_SECONDS = 20 * 60
MAX_PLAY_FILE_BYTES = 128 * 1024 * 1024
MIN_PLAYBACK_SPEED = 0.5
MAX_PLAYBACK_SPEED = 2.0
PLAY_DEBUG = os.getenv("PLAY_DEBUG", "true").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class _PlayRequest:
    ctx: commands.Context
    source_url: str
    title: str
    duration_seconds: float
    duration_known: bool
    playback_speed: float
    webpage_url: str
    uploader: str
    mode: str
    requester_id: int
    requester_name: str
    target_channel_id: int
    target_channel_name: str
    text_channel_id: int
    enqueued_at: float


@dataclass(slots=True)
class _PreparedTrack:
    request: _PlayRequest
    temp_dir: str
    temp_path: str
    duration_seconds: float
    started_at: float = 0.0
    started_offset: float = 0.0
    skip_message_id: int = 0


def _probe_audio_seconds(path: str) -> float:
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


def _format_duration(seconds: float) -> str:
    total = int(round(max(0.0, float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _clean_title(title: str, fallback: str) -> str:
    raw = str(title or "").strip()
    if raw:
        return raw
    parsed = urllib.parse.urlparse(fallback)
    name = os.path.basename(parsed.path or "").strip()
    return urllib.parse.unquote(name) or fallback


def _looks_like_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _clamp_playback_speed(value: float) -> float:
    return max(MIN_PLAYBACK_SPEED, min(MAX_PLAYBACK_SPEED, float(value)))


def _speed_tag(speed: float) -> str:
    return "" if abs(float(speed) - 1.0) < 1e-6 else f" at **{float(speed):.2f}x**"


def _effective_playback_duration(duration_seconds: float, speed: float) -> float:
    duration = max(0.0, float(duration_seconds))
    return duration / _clamp_playback_speed(speed) if duration > 0.0 else 0.0


def _split_play_input(raw_input: str) -> tuple[str, float]:
    text = str(raw_input or "").strip()
    if not text:
        raise RuntimeError("Usage: `!play <link or search terms> [speed 0.5-2.0]`")

    parts = text.rsplit(maxsplit=1)
    if len(parts) < 2:
        return text, 1.0

    query = parts[0].strip()
    token = parts[1].strip().lower()
    if not query:
        raise RuntimeError("Usage: `!play <link or search terms> [speed 0.5-2.0]`")

    if token.endswith("x"):
        token = token[:-1].strip()
    try:
        parsed_speed = float(token)
    except ValueError:
        return text, 1.0

    if _looks_like_url(query) or (MIN_PLAYBACK_SPEED <= parsed_speed <= MAX_PLAYBACK_SPEED):
        return query, _clamp_playback_speed(parsed_speed)
    return text, 1.0


def _is_youtube_source(value: str) -> bool:
    src = str(value or "").strip()
    if not src:
        return False
    if src.startswith("ytsearch"):
        return True
    parsed = urllib.parse.urlparse(src)
    host = (parsed.netloc or "").strip().lower()
    return host.endswith("youtube.com") or host.endswith("youtu.be") or host.endswith("music.youtube.com")


def _cookies_from_browser_opt() -> Optional[tuple[str, ...]]:
    raw = str(YTDLP_COOKIES_FROM_BROWSER or "").strip()
    if not raw:
        return None
    browser, sep, profile = raw.partition(":")
    browser = browser.strip().lower()
    if not browser:
        return None
    if not sep:
        return (browser,)
    profile = profile.strip()
    return (browser, profile) if profile else (browser,)


def _coerce_single_entry(info: dict) -> dict:
    if not isinstance(info, dict):
        raise RuntimeError("That link did not return playable metadata.")
    entries = info.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                return entry
        raise RuntimeError("That link did not contain a playable entry.")
    return info


def _build_direct_request_info(url: str) -> tuple[str, float, bool, str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("Play links must start with http:// or https://.")

    title = _clean_title("", url)
    content_length = 0
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            header = resp.headers.get("Content-Length")
            if header:
                content_length = max(0, int(header))
    except Exception:
        content_length = 0

    if content_length > MAX_PLAY_FILE_BYTES:
        raise RuntimeError(
            f"That file is too large to queue ({content_length / (1024 * 1024):.1f} MB > {MAX_PLAY_FILE_BYTES / (1024 * 1024):.0f} MB)."
        )

    return title, 0.0, False, url, ""


class PlaybackCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._guild_queues: dict[int, asyncio.Queue[_PlayRequest]] = {}
        self._guild_workers: dict[int, asyncio.Task] = {}
        self._guild_current: dict[int, _PreparedTrack] = {}
        self._guild_active_vc: dict[int, discord.VoiceClient] = {}
        self._guild_skip_events: dict[int, asyncio.Event] = {}
        self._guild_play_allowed: dict[int, asyncio.Event] = {}
        self._guild_processing: set[int] = set()
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def cog_unload(self):
        for task in self._guild_workers.values():
            task.cancel()
        self._guild_workers.clear()
        self._guild_current.clear()
        self._guild_active_vc.clear()
        self._guild_skip_events.clear()
        self._guild_play_allowed.clear()
        self._guild_processing.clear()
        self._guild_locks.clear()

    def _debug(self, ctx: commands.Context, msg: str):
        if not PLAY_DEBUG:
            return
        gid = int(getattr(ctx.guild, "id", 0) or 0)
        uid = int(getattr(ctx.author, "id", 0) or 0)
        print(f"[PLAY][g={gid}][u={uid}] {msg}")

    def _yt_dlp_auth_available(self) -> bool:
        return bool(str(YTDLP_COOKIE_FILE or "").strip() or _cookies_from_browser_opt())

    def _apply_youtube_auth_tuning(self, source: str, opts: dict) -> dict:
        out = dict(opts)
        if not _is_youtube_source(source):
            return out
        js_runtimes = dict(out.get("js_runtimes") or {})
        js_runtimes.setdefault("node", {})
        out["js_runtimes"] = js_runtimes

        remote_components = out.get("remote_components")
        if isinstance(remote_components, dict):
            enabled_components = list(remote_components.keys())
        elif isinstance(remote_components, str):
            enabled_components = [remote_components]
        else:
            enabled_components = list(remote_components or [])
        if "ejs:github" not in enabled_components:
            enabled_components.append("ejs:github")
        out["remote_components"] = enabled_components
        return out

    def _apply_yt_dlp_auth(self, opts: dict) -> dict:
        out = dict(opts)
        cookie_file = str(YTDLP_COOKIE_FILE or "").strip()
        if cookie_file:
            out["cookiefile"] = cookie_file
            return out
        cookies_from_browser = _cookies_from_browser_opt()
        if cookies_from_browser:
            out["cookiesfrombrowser"] = cookies_from_browser
        return out

    def _should_retry_with_auth(self, source: str) -> bool:
        return _is_youtube_source(source) and self._yt_dlp_auth_available()

    def _auth_failure_hint(self, source: str) -> str:
        if self._should_retry_with_auth(source):
            return ""
        if not _is_youtube_source(source):
            return ""
        return (
            " This may be age-restricted or gated. Configure `YTDLP_COOKIES_FROM_BROWSER` "
            "or `YTDLP_COOKIE_FILE` to let yt-dlp retry with an authenticated YouTube session."
        )

    def _format_ytdlp_error(self, source: str, error: Exception, *, auth_attempted: bool) -> str:
        err_text = str(error)
        if auth_attempted and _is_youtube_source(source) and "Sign in to confirm your age" in err_text:
            return (
                "yt-dlp could not inspect that link even with the configured YouTube cookies. "
                "YouTube still reported that the session is not age-authorized for this video. "
                "Re-export cookies from a signed-in 18+ account that can open this exact link on youtube.com."
            )
        return f"yt-dlp could not inspect that link: {err_text}{self._auth_failure_hint(source)}"

    def _extract_info_with_ytdlp(self, source: str, *, opts: dict) -> dict:
        with YoutubeDL(opts) as ydl:
            return _coerce_single_entry(ydl.extract_info(source, download=False))

    def _download_with_ytdlp(
        self,
        source: str,
        *,
        opts: dict,
        temp_dir: str,
    ) -> tuple[dict, str]:
        with YoutubeDL(opts) as ydl:
            info = _coerce_single_entry(ydl.extract_info(source, download=True))
            path = ""
            requested = info.get("requested_downloads")
            if isinstance(requested, list) and requested:
                maybe_path = requested[0].get("filepath")
                if maybe_path:
                    path = str(maybe_path)
            if not path:
                path = str(ydl.prepare_filename(info))
        if not os.path.exists(path):
            files = [
                os.path.join(temp_dir, name)
                for name in os.listdir(temp_dir)
                if os.path.isfile(os.path.join(temp_dir, name))
            ]
            if not files:
                raise RuntimeError("yt-dlp did not produce a playable audio file.")
            path = max(files, key=os.path.getsize)
        return info, path

    def _clear_temp_dir_files(self, temp_dir: str) -> None:
        try:
            for name in os.listdir(temp_dir):
                maybe = os.path.join(temp_dir, name)
                if os.path.isfile(maybe):
                    os.remove(maybe)
        except OSError:
            pass

    def _queue_for(self, guild_id: int) -> asyncio.Queue[_PlayRequest]:
        queue = self._guild_queues.get(guild_id)
        if queue is None:
            queue = asyncio.Queue()
            self._guild_queues[guild_id] = queue
        return queue

    def _skip_event_for(self, guild_id: int) -> asyncio.Event:
        ev = self._guild_skip_events.get(guild_id)
        if ev is None:
            ev = asyncio.Event()
            self._guild_skip_events[guild_id] = ev
        return ev

    def _play_allowed_for(self, guild_id: int) -> asyncio.Event:
        ev = self._guild_play_allowed.get(guild_id)
        if ev is None:
            ev = asyncio.Event()
            ev.set()
            self._guild_play_allowed[guild_id] = ev
        return ev

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    def _ensure_worker(self, guild_id: int):
        task = self._guild_workers.get(guild_id)
        if task and not task.done():
            return
        queue = self._queue_for(guild_id)
        self._guild_workers[guild_id] = asyncio.create_task(self._worker_loop(guild_id, queue))

    def _is_play_admin(self, ctx: commands.Context) -> bool:
        perms = getattr(ctx.author, "guild_permissions", None)
        is_admin = bool(perms and perms.administrator)
        return bool(is_admin or is_control_user_id(ctx.guild, ctx.author.id))

    def _queue_snapshot(self, guild_id: int) -> list[_PlayRequest]:
        queue = self._guild_queues.get(guild_id)
        if queue is None:
            return []
        return list(getattr(queue, "_queue", []))

    def _active_play_channel_id(self, guild_id: int) -> int:
        current = self._guild_current.get(guild_id)
        if current is not None:
            return int(current.request.target_channel_id)
        snapshot = self._queue_snapshot(guild_id)
        if snapshot:
            return int(snapshot[0].target_channel_id)
        return 0

    def _channel_has_humans(self, channel: discord.VoiceChannel) -> bool:
        return any(not getattr(member, "bot", False) for member in getattr(channel, "members", []))

    def _eligible_skip_voter_ids(self, channel: discord.VoiceChannel) -> set[int]:
        return {
            int(member.id)
            for member in getattr(channel, "members", [])
            if not getattr(member, "bot", False)
        }

    def has_pending_audio(self, guild_id: int) -> bool:
        queue = self._guild_queues.get(guild_id)
        pending = queue.qsize() if queue is not None else 0
        return bool(self._guild_current.get(guild_id) is not None or guild_id in self._guild_processing or pending > 0)

    def should_keep_voice_connected(self, guild_id: int) -> bool:
        return self.has_pending_audio(guild_id)

    def note_voice_disconnected(self, guild_id: int) -> None:
        self._guild_active_vc.pop(guild_id, None)

    async def suspend_for_tts(self, ctx: commands.Context, target_channel: discord.VoiceChannel) -> bool:
        if ctx.guild is None:
            return False
        guild_id = int(ctx.guild.id)
        if not self.has_pending_audio(guild_id):
            return False

        bound_channel_id = self._active_play_channel_id(guild_id)
        if bound_channel_id and bound_channel_id != target_channel.id:
            bound_channel = ctx.guild.get_channel(bound_channel_id)
            bound_name = getattr(bound_channel, "name", "another voice channel")
            raise RuntimeError(
                f"I'm already playing queued audio in **{bound_name}**. "
                "TTS can only interrupt playback in that same voice channel."
            )

        async with self._lock_for(guild_id):
            self._play_allowed_for(guild_id).clear()
            current = self._guild_current.get(guild_id)
            vc = self._guild_active_vc.get(guild_id) or ctx.voice_client
            if current is not None and vc is not None and (vc.is_playing() or vc.is_paused()):
                current.started_offset = self._current_offset(current)
                try:
                    vc.stop()
                except Exception:
                    pass
            return True

    async def restore_after_tts(self, guild_id: int) -> None:
        self._play_allowed_for(guild_id).set()

    async def shutdown_guild(self, guild_id: int, *, clear_queue: bool = True, disconnect: bool = False) -> None:
        if clear_queue:
            self._clear_pending_queue(guild_id)
        self._skip_event_for(guild_id).set()
        self._play_allowed_for(guild_id).set()

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

    async def _worker_loop(self, guild_id: int, queue: asyncio.Queue[_PlayRequest]):
        while True:
            req = await queue.get()
            try:
                self._guild_processing.add(guild_id)
                self._skip_event_for(guild_id).clear()
                await self._process_play(req)
            except Exception as e:
                self._debug(req.ctx, f"worker unhandled exception={type(e).__name__}: {e}")
            finally:
                self._guild_processing.discard(guild_id)
                queue.task_done()

    def _extract_track_request(self, raw_input: str) -> tuple[str, str, float, bool, str, str, str]:
        src, _ = _split_play_input(raw_input)

        is_url = _looks_like_url(src)
        if (not is_url) and YoutubeDL is None:
            raise RuntimeError("Search playback requires `yt-dlp`. Add it with `pip install -r requirements.txt`.")

        lookup_value = src if is_url else f"ytsearch1:{src}"

        if YoutubeDL is not None:
            opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "extract_flat": False,
                "skip_download": True,
                "socket_timeout": 20,
            }
            tuned_auth_opts = None
            if self._should_retry_with_auth(lookup_value):
                tuned_auth_opts = self._apply_yt_dlp_auth(
                    self._apply_youtube_auth_tuning(lookup_value, opts),
                )
            try:
                info = self._extract_info_with_ytdlp(lookup_value, opts=tuned_auth_opts or opts)
            except Exception as e:
                if tuned_auth_opts is not None:
                    try:
                        info = self._extract_info_with_ytdlp(lookup_value, opts=opts)
                    except Exception as plain_error:
                        if not is_url:
                            raise RuntimeError(f'No YouTube results found for "{src}".')
                        raise RuntimeError(
                            self._format_ytdlp_error(
                                lookup_value,
                                plain_error,
                                auth_attempted=True,
                            )
                        ) from plain_error
                else:
                    if not is_url:
                        raise RuntimeError(f'No YouTube results found for "{src}".')
                    raise RuntimeError(self._format_ytdlp_error(lookup_value, e, auth_attempted=False)) from e
            try:
                duration = float(info.get("duration") or 0.0)
                filesize = int(info.get("filesize") or info.get("filesize_approx") or 0)
                if duration > MAX_PLAY_DURATION_SECONDS:
                    raise RuntimeError(
                        f"That track is too long to queue ({_format_duration(duration)} > {_format_duration(MAX_PLAY_DURATION_SECONDS)})."
                    )
                if filesize > MAX_PLAY_FILE_BYTES:
                    raise RuntimeError(
                        f"That track looks too large to queue ({filesize / (1024 * 1024):.1f} MB > {MAX_PLAY_FILE_BYTES / (1024 * 1024):.0f} MB)."
                    )

                title = _clean_title(str(info.get("title") or info.get("fulltitle") or ""), src)
                webpage_url = str(info.get("webpage_url") or info.get("original_url") or src).strip() or src
                source_url = str(info.get("original_url") or info.get("webpage_url") or webpage_url).strip() or webpage_url
                uploader = str(info.get("uploader") or info.get("channel") or "").strip()
                return source_url, title, duration, duration > 0.0, webpage_url, uploader, "ytdlp"
            except RuntimeError:
                raise
            except Exception:
                pass

        if not is_url:
            raise RuntimeError("Search playback requires `yt-dlp`. Add it with `pip install -r requirements.txt`.")
        title, duration, duration_known, webpage_url, uploader = _build_direct_request_info(src)
        return src, title, duration, duration_known, webpage_url, uploader, "direct"

    def _download_via_ytdlp(self, req: _PlayRequest) -> _PreparedTrack:
        if YoutubeDL is None:
            raise RuntimeError("`yt-dlp` is not installed. Add it with `pip install -r requirements.txt`.")

        temp_dir = tempfile.mkdtemp(prefix="paragon_play_")
        outtmpl = os.path.join(temp_dir, "track.%(ext)s")
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "windowsfilenames": True,
            "nopart": True,
            "socket_timeout": 30,
        }
        tuned_auth_opts = None
        if self._should_retry_with_auth(req.source_url):
            tuned_auth_opts = self._apply_yt_dlp_auth(
                self._apply_youtube_auth_tuning(req.source_url, opts),
            )
        try:
            info = None
            path = ""
            try:
                info, path = self._download_with_ytdlp(
                    req.source_url,
                    opts=tuned_auth_opts or opts,
                    temp_dir=temp_dir,
                )
            except Exception:
                if tuned_auth_opts is None:
                    raise
                self._clear_temp_dir_files(temp_dir)
                info, path = self._download_with_ytdlp(
                    req.source_url,
                    opts=opts,
                    temp_dir=temp_dir,
                )
        except Exception as e:
            try:
                self._clear_temp_dir_files(temp_dir)
                os.rmdir(temp_dir)
            except OSError:
                pass
            raise RuntimeError(f"yt-dlp could not fetch audio from that link: {e}{self._auth_failure_hint(req.source_url)}") from e

        duration = float(req.duration_seconds or 0.0)
        if duration <= 0.0:
            duration = _probe_audio_seconds(path)
        size = os.path.getsize(path)
        if duration > MAX_PLAY_DURATION_SECONDS:
            try:
                os.remove(path)
                os.rmdir(temp_dir)
            except OSError:
                pass
            raise RuntimeError(
                f"That track is too long to queue ({_format_duration(duration)} > {_format_duration(MAX_PLAY_DURATION_SECONDS)})."
            )
        if size > MAX_PLAY_FILE_BYTES:
            try:
                os.remove(path)
                os.rmdir(temp_dir)
            except OSError:
                pass
            raise RuntimeError(
                f"That downloaded audio is too large to queue ({size / (1024 * 1024):.1f} MB > {MAX_PLAY_FILE_BYTES / (1024 * 1024):.0f} MB)."
            )

        return _PreparedTrack(
            request=req,
            temp_dir=temp_dir,
            temp_path=path,
            duration_seconds=duration,
        )

    def _download_direct(self, req: _PlayRequest) -> _PreparedTrack:
        temp_dir = tempfile.mkdtemp(prefix="paragon_play_")
        parsed = urllib.parse.urlparse(req.source_url)
        _, ext = os.path.splitext(parsed.path or "")
        suffix = ext if ext else ".bin"
        temp_path = os.path.join(temp_dir, f"track{suffix}")

        request = urllib.request.Request(
            req.source_url,
            method="GET",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        downloaded = 0
        try:
            with urllib.request.urlopen(request, timeout=45) as resp, open(temp_path, "wb") as handle:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_PLAY_FILE_BYTES:
                        raise RuntimeError(
                            f"That file is too large to queue ({downloaded / (1024 * 1024):.1f} MB > {MAX_PLAY_FILE_BYTES / (1024 * 1024):.0f} MB)."
                        )
                    handle.write(chunk)
        except (RuntimeError, urllib.error.URLError) as e:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                os.rmdir(temp_dir)
            except OSError:
                pass
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(f"Could not download audio from that link: {e}") from e

        duration = _probe_audio_seconds(temp_path)
        if duration <= 0.0:
            try:
                os.remove(temp_path)
                os.rmdir(temp_dir)
            except OSError:
                pass
            raise RuntimeError("I downloaded that link, but ffmpeg could not read a playable audio stream from it.")
        if duration > MAX_PLAY_DURATION_SECONDS:
            try:
                os.remove(temp_path)
                os.rmdir(temp_dir)
            except OSError:
                pass
            raise RuntimeError(
                f"That track is too long to queue ({_format_duration(duration)} > {_format_duration(MAX_PLAY_DURATION_SECONDS)})."
            )

        return _PreparedTrack(
            request=req,
            temp_dir=temp_dir,
            temp_path=temp_path,
            duration_seconds=duration,
        )

    def _prepare_track_download(self, req: _PlayRequest) -> _PreparedTrack:
        if req.mode == "direct":
            return self._download_direct(req)
        return self._download_via_ytdlp(req)

    def _current_offset(self, track: _PreparedTrack) -> float:
        elapsed = max(0.0, time.monotonic() - float(track.started_at or 0.0))
        if track.started_at <= 0.0:
            return max(0.0, float(track.started_offset or 0.0))
        consumed = elapsed * _clamp_playback_speed(track.request.playback_speed)
        return min(float(track.duration_seconds or 0.0), max(0.0, float(track.started_offset or 0.0) + consumed))

    async def _cleanup_track(self, track: _PreparedTrack) -> None:
        try:
            if os.path.exists(track.temp_path):
                os.remove(track.temp_path)
        except OSError:
            pass
        try:
            os.rmdir(track.temp_dir)
        except OSError:
            pass

    async def _post_now_playing(self, track: _PreparedTrack) -> None:
        ctx = track.request.ctx
        effective_duration = _effective_playback_duration(track.duration_seconds, track.request.playback_speed)
        duration_text = _format_duration(effective_duration) if effective_duration > 0 else "unknown length"
        line = (
            f"Now playing **{track.request.title}** ({duration_text}){_speed_tag(track.request.playback_speed)}"
            f" requested by **{track.request.requester_name}**."
        )
        if track.request.uploader:
            line += f" Source: **{track.request.uploader}**."
        line += f"\nReact with {FAST_FORWARD_EMOJI} to vote-skip (50% of non-bot users in voice)."
        msg = await ctx.send(line)
        track.skip_message_id = int(msg.id)
        try:
            await msg.add_reaction(FAST_FORWARD_EMOJI)
        except Exception:
            pass

    async def _wait_for_track_end(
        self,
        track: _PreparedTrack,
        vc: discord.VoiceClient,
        *,
        play_err: list[str],
    ) -> str:
        guild_id = int(track.request.ctx.guild.id)
        skip_event = self._skip_event_for(guild_id)
        play_allowed = self._play_allowed_for(guild_id)
        effective_duration = _effective_playback_duration(track.duration_seconds, track.request.playback_speed)
        max_wait = max(8.0, min(60.0 * 60.0, effective_duration + 20.0))
        play_started = time.monotonic()
        while True:
            if play_err:
                return "error"
            if skip_event.is_set():
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                return "skipped"
            if not play_allowed.is_set():
                return "interrupted"
            if not vc.is_playing() and not vc.is_paused():
                return "finished"
            if (time.monotonic() - play_started) > max_wait:
                try:
                    vc.stop()
                except Exception:
                    pass
                return "timeout"
            await asyncio.sleep(0.2)

    async def _start_track_playback(self, track: _PreparedTrack, vc: discord.VoiceClient) -> tuple[str, list[str]]:
        play_err: list[str] = []

        def _after(err: Optional[Exception]):
            if err:
                play_err.append(str(err))

        before_options = None
        if float(track.started_offset or 0.0) > 0.0:
            before_options = f"-ss {track.started_offset:.3f}"
        ffmpeg_options = "-vn"
        speed = _clamp_playback_speed(track.request.playback_speed)
        if abs(speed - 1.0) >= 1e-6:
            ffmpeg_options = f"-vn -filter:a atempo={speed:.3f}"

        base_src = discord.FFmpegPCMAudio(
            track.temp_path,
            before_options=before_options,
            options=ffmpeg_options,
        )
        src = discord.PCMVolumeTransformer(base_src, volume=max(0.0, float(PLAYBACK_VOLUME)))
        track.started_at = time.monotonic()
        try:
            vc.play(src, after=_after)
        except Exception as e:
            return f"error:{e}", play_err

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
            return "not_started", play_err

        state = await self._wait_for_track_end(track, vc, play_err=play_err)
        return state, play_err

    async def _process_play(self, req: _PlayRequest) -> None:
        ctx = req.ctx
        if ctx.guild is None:
            return

        guild_id = int(ctx.guild.id)
        target_channel = ctx.guild.get_channel(req.target_channel_id)
        if not isinstance(target_channel, discord.VoiceChannel):
            await ctx.reply(f"Voice channel for **{req.title}** is no longer available.")
            return
        if not self._channel_has_humans(target_channel):
            await ctx.reply(f"Skipping **{req.title}** because no one is in **{target_channel.name}** anymore.")
            return

        prepared: Optional[_PreparedTrack] = None
        try:
            self._debug(ctx, f"preparing track title={req.title!r} mode={req.mode}")
            prepared = await asyncio.to_thread(self._prepare_track_download, req)
            prepared.duration_seconds = float(prepared.duration_seconds or req.duration_seconds or 0.0)
            self._guild_current[guild_id] = prepared

            announced = False
            while True:
                if self._skip_event_for(guild_id).is_set():
                    await ctx.reply(f"Skipped **{prepared.request.title}** before playback started.")
                    return

                await self._play_allowed_for(guild_id).wait()
                vc = await ensure_voice_client(ctx, target_channel, debug=lambda msg: self._debug(ctx, msg))
                self._guild_active_vc[guild_id] = vc
                if vc.is_playing() or vc.is_paused():
                    await asyncio.sleep(0.2)
                    continue

                if not announced:
                    await self._post_now_playing(prepared)
                    announced = True

                state, play_err = await self._start_track_playback(prepared, vc)
                if state.startswith("error:"):
                    await ctx.reply(f"Playback error: {state.split(':', 1)[1][:160]}")
                    return
                if state == "not_started":
                    if play_err:
                        await ctx.reply(f"Playback error: {play_err[0][:160]}")
                    else:
                        await ctx.reply("Playback did not start.")
                    return
                if state == "error":
                    await ctx.reply(f"Playback error: {play_err[0][:160]}")
                    return
                if state == "timeout":
                    await ctx.reply("Playback timed out.")
                    return
                if state == "skipped":
                    await ctx.reply(f"Skipped **{prepared.request.title}**.")
                    return
                if state == "interrupted":
                    if prepared.duration_seconds > 0 and prepared.started_offset >= max(0.0, prepared.duration_seconds - 0.5):
                        return
                    continue
                return
        except RuntimeError as e:
            await ctx.reply(str(e))
        except discord.ClientException as e:
            await ctx.reply(f"Playback failed: {e}")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.reply("Couldn't join or play audio in that channel.")
        except Exception as e:
            await ctx.reply(f"Playback failed: {type(e).__name__}: {e}")
            self._debug(ctx, f"Unhandled exception={type(e).__name__}: {e}")
        finally:
            self._guild_current.pop(guild_id, None)
            if prepared is not None:
                await self._cleanup_track(prepared)

    @commands.command(name="play")
    async def play(self, ctx: commands.Context, *, link: Optional[str] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        dave_ok, dave_reason = dave_support_status()
        if not dave_ok:
            await ctx.reply(f"Voice unavailable: {dave_reason}")
            return

        author_voice = getattr(ctx.author, "voice", None)
        target_channel = author_voice.channel if author_voice and author_voice.channel else None
        if not isinstance(target_channel, discord.VoiceChannel):
            await ctx.reply("Join a standard voice channel first.")
            return
        if not link or not str(link).strip():
            await ctx.reply(f"Usage: `{ctx.clean_prefix}play <link or search terms> [speed 0.5-2.0]`")
            return

        try:
            play_target, playback_speed = _split_play_input(str(link))
        except RuntimeError as e:
            await ctx.reply(str(e))
            return

        bound_channel_id = self._active_play_channel_id(ctx.guild.id)
        if bound_channel_id and bound_channel_id != target_channel.id:
            bound_channel = ctx.guild.get_channel(bound_channel_id)
            bound_name = getattr(bound_channel, "name", "another voice channel")
            await ctx.reply(f"The current play queue is bound to **{bound_name}**. Join that channel to add more tracks.")
            return

        me = ctx.guild.me
        if me is None:
            await ctx.reply("Bot voice state unavailable in this guild.")
            return
        perms = target_channel.permissions_for(me)
        if not perms.view_channel or not perms.connect:
            await ctx.reply("I don't have permission to join that channel.")
            return

        try:
            source_url, title, duration, duration_known, webpage_url, uploader, mode = await asyncio.to_thread(
                self._extract_track_request,
                play_target,
            )
        except RuntimeError as e:
            await ctx.reply(str(e))
            return
        except Exception as e:
            await ctx.reply(f"Couldn't inspect that link: {e}")
            return

        req = _PlayRequest(
            ctx=ctx,
            source_url=source_url,
            title=title,
            duration_seconds=duration,
            duration_known=duration_known,
            playback_speed=playback_speed,
            webpage_url=webpage_url,
            uploader=uploader,
            mode=mode,
            requester_id=int(ctx.author.id),
            requester_name=str(ctx.author.display_name),
            target_channel_id=int(target_channel.id),
            target_channel_name=str(target_channel.name),
            text_channel_id=int(ctx.channel.id),
            enqueued_at=time.monotonic(),
        )

        queue = self._queue_for(ctx.guild.id)
        await queue.put(req)
        record_game_fields(ctx.guild.id, ctx.author.id, "playback", tracks_queued=1)
        await save_data()
        self._ensure_worker(ctx.guild.id)
        position = queue.qsize() + (1 if ctx.guild.id in self._guild_processing else 0)
        effective_duration = _effective_playback_duration(duration, playback_speed)
        duration_text = _format_duration(effective_duration) if duration_known and effective_duration > 0 else "duration pending validation"
        if position <= 1:
            await ctx.reply(f"Queued **{title}** ({duration_text}){_speed_tag(playback_speed)}. Starting shortly.")
        else:
            await ctx.reply(f"Queued **{title}** ({duration_text}){_speed_tag(playback_speed)} at position **{position}**.")

    @commands.command(name="playskip")
    async def play_skip(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if not self._is_play_admin(ctx):
            await ctx.reply("You don't have permission to skip queued audio.")
            return
        if not self.has_pending_audio(ctx.guild.id):
            await ctx.reply("No queued audio is currently active.")
            return

        self._skip_event_for(ctx.guild.id).set()
        vc = self._guild_active_vc.get(ctx.guild.id) or ctx.voice_client
        if vc is not None:
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                pass
        await ctx.reply("Skip requested for the current track.")

    @commands.command(name="playclear")
    async def play_clear(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if not self._is_play_admin(ctx):
            await ctx.reply("You don't have permission to clear the play queue.")
            return

        cleared = self._clear_pending_queue(ctx.guild.id)
        current = self._guild_current.get(ctx.guild.id)
        if current is not None:
            await ctx.reply(f"Cleared **{cleared}** queued track(s). One track is still active.")
        else:
            await ctx.reply(f"Cleared **{cleared}** queued track(s).")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.user_id == getattr(self.bot.user, "id", 0):
            return
        if str(payload.emoji) != FAST_FORWARD_EMOJI:
            return

        current = self._guild_current.get(int(payload.guild_id))
        if current is None or int(current.skip_message_id or 0) != int(payload.message_id):
            return
        if current.request.ctx.guild is None:
            return

        guild = current.request.ctx.guild
        voice_channel = guild.get_channel(current.request.target_channel_id)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return

        eligible_ids = self._eligible_skip_voter_ids(voice_channel)
        if payload.user_id not in eligible_ids:
            return

        text_channel = self.bot.get_channel(payload.channel_id)
        if text_channel is None:
            try:
                text_channel = await self.bot.fetch_channel(payload.channel_id)
            except Exception:
                return
        if not hasattr(text_channel, "fetch_message"):
            return

        try:
            msg = await text_channel.fetch_message(payload.message_id)
        except Exception:
            return

        vote_count = 0
        for reaction in msg.reactions:
            if str(reaction.emoji) != FAST_FORWARD_EMOJI:
                continue
            try:
                users = [user async for user in reaction.users()]
            except Exception:
                users = []
            vote_count = sum(1 for user in users if int(getattr(user, "id", 0)) in eligible_ids)
            break

        required = max(1, math.ceil(len(eligible_ids) * 0.5))
        if vote_count < required:
            return
        if self._skip_event_for(payload.guild_id).is_set():
            return

        self._skip_event_for(payload.guild_id).set()
        vc = self._guild_active_vc.get(payload.guild_id)
        if vc is not None:
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                pass

        try:
            await current.request.ctx.send(f"Skip vote passed for **{current.request.title}** ({vote_count}/{required}).")
        except Exception:
            pass
