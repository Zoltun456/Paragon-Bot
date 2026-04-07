import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _as_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_data_path(env_name: str, *candidates: str) -> str:
    raw = os.getenv(env_name)
    if raw and raw.strip():
        p = Path(raw.strip())
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        return str(p)

    for candidate in candidates:
        p = Path(candidate)
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        if p.exists():
            return str(p)

    fallback = Path(candidates[0])
    if not fallback.is_absolute():
        fallback = _PROJECT_ROOT / fallback
    return str(fallback)


def _optional_resolve_path(env_name: str) -> str:
    raw = os.getenv(env_name)
    if raw is None or raw.strip() == "":
        return ""
    p = Path(raw.strip())
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return str(p)


TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
GUILD_DB_DIR = os.getenv("PARAGON_DB_DIR", "paragon_data")

# IDs / settings
AUTHOR_USER_ID = _as_int("AUTHOR_USER_ID", _as_int("OWNER_USER_ID", 0))
AFK_CHANNEL_ID = _as_int("AFK_CHANNEL_ID", 0)
_AFK_CHANNEL_CACHE: dict[int, int] = {}

# Timezone
TZ = os.getenv("PARAGON_TZ", "America/New_York")
LOCAL_TZ = ZoneInfo(TZ)

# XP model
BASE_XP_PER_MINUTE = _as_float("BASE_XP_PER_MINUTE", 1.0)
XP_REWARD_BOOST_MIN_PCT = _as_float("XP_REWARD_BOOST_MIN_PCT", 0.08)     # 8%
XP_REWARD_BOOST_MAX_PCT = _as_float("XP_REWARD_BOOST_MAX_PCT", 2.50)     # 250%
XP_REWARD_BOOST_SCALE = _as_float("XP_REWARD_BOOST_SCALE", 0.22)         # log10 scaling
XP_REWARD_BOOST_MIN_MINUTES = _as_int("XP_REWARD_BOOST_MIN_MINUTES", 10)
XP_REWARD_BOOST_MAX_MINUTES = _as_int("XP_REWARD_BOOST_MAX_MINUTES", 720)

# Playback
PLAYBACK_VOLUME = _as_float("PLAYBACK_VOLUME", 0.25)
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
YTDLP_COOKIE_FILE = _optional_resolve_path("YTDLP_COOKIE_FILE")

# Blackjack
BJ_MAX_PLAYERS = _as_int("BJ_MAX_PLAYERS", 20)
BJ_DAILY_RESET_HOUR = _as_int("BJ_DAILY_RESET_HOUR", 0)
BJ_DAILY_RESET_MINUTE = _as_int("BJ_DAILY_RESET_MINUTE", 0)
BJ_TURN_TIMEOUT_SECONDS = _as_int("BJ_TURN_TIMEOUT_SECONDS", 5 * 60)
BJ_SEAT_IDLE_TIMEOUT_SECONDS = _as_int("BJ_SEAT_IDLE_TIMEOUT_SECONDS", 5 * 60)
BJ_COOLDOWN_ENABLED = _as_bool("BJ_COOLDOWN_ENABLED", True)

# Prestige
PRESTIGE_BOARD_LIMIT = _as_int("PRESTIGE_BOARD_LIMIT", 10)

# Lottery
LOTTO_TICKET_COST = _as_int("LOTTO_TICKET_COST", 1)
LOTTO_DRAW_HOUR = _as_int("LOTTO_DRAW_HOUR", 18)
LOTTO_DRAW_MINUTE = _as_int("LOTTO_DRAW_MINUTE", 0)
LOTTO_MAX_PER_USER = _as_int("LOTTO_MAX_PER_USER", 5_000)

# Thanks
THANKS_GIFT_XP = _as_int("THANKS_GIFT_XP", 20)

# Anagram
ANAGRAM_PHRASES_PATH = _resolve_data_path(
    "ANAGRAM_PHRASES_PATH",
    "anagram_phrases.txt",
    "OLD/anagram_phrases.txt",
)
ANAGRAM_DAILY_LIMIT = _as_int("ANAGRAM_DAILY_LIMIT", 10)
ANAGRAM_WIN_XP = _as_int("ANAGRAM_WIN_XP", 5)
ANAGRAM_LOSS_XP = _as_int("ANAGRAM_LOSS_XP", 5)

# Surprise Drops
DROP_MIN_MINUTES = _as_int("DROP_MIN_MINUTES", 30)
DROP_MAX_MINUTES = _as_int("DROP_MAX_MINUTES", 240)
DROP_MIN_XP = _as_int("DROP_MIN_XP", 50)
DROP_MAX_XP = _as_int("DROP_MAX_XP", 150)

# Wordle
WORDLE_WORD_LENGTH = _as_int("WORDLE_WORD_LENGTH", 5)
WORDLE_WIN_XP = _as_int("WORDLE_WIN_XP", 50)
WORDLE_LOSS_XP = _as_int("WORDLE_LOSS_XP", 50)
WORDLE_MAX_GUESSES = _as_int("WORDLE_MAX_GUESSES", 5)
WORDLE_RESPECT_ACTIVE_HOURS = os.getenv("WORDLE_RESPECT_ACTIVE_HOURS", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
WORDLE_WORDLIST_PATH = _resolve_data_path(
    "WORDLE_WORDLIST_PATH",
    "wordlist.txt",
    "OLD/wordlist.txt",
)
WORDLE_VALID_GUESSES_PATH = _resolve_data_path(
    "WORDLE_VALID_GUESSES_PATH",
    "valid-wordle-words.txt",
    "OLD/valid-wordle-words.txt",
)
WORD_REGEX = re.compile(rf"^[A-Za-z]{{{WORDLE_WORD_LENGTH}}}$")

# Coin flip
CF_MAX_BET = _as_int("CF_MAX_BET", -1)
CF_TTL_SECONDS = _as_int("CF_TTL_SECONDS", 120)

# TTS (ElevenLabs)
ELEVEN_API = os.getenv("ELEVEN_API", "").strip()
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip()
ELEVEN_MODEL_ID = os.getenv("ELEVEN_MODEL_ID", "eleven_flash_v2_5").strip()
ELEVEN_OUTPUT_FORMAT = os.getenv("ELEVEN_OUTPUT_FORMAT", "mp3_44100_128").strip()

# Daily Spin Wheel
SPIN_RESET_HOUR = _as_int("SPIN_RESET_HOUR", 0)
SPIN_RESET_MINUTE = _as_int("SPIN_RESET_MINUTE", 0)
SPIN_DISABLED_REWARDS = {
    token.strip().lower()
    for token in os.getenv("SPIN_DISABLED_REWARDS", "").split(",")
    if token.strip()
}


def _looks_like_afk_name(name: str) -> bool:
    n = str(name or "").strip().lower()
    if not n:
        return False
    if n in {"afk", "afk-channel", "away", "away-from-keyboard", "idle"}:
        return True
    tokens = ("afk", "away", "idle", "inactive")
    return any(t in n for t in tokens)


def resolve_afk_channel_id(guild=None) -> int:
    """
    Resolve AFK channel id with fallback order:
    1) Guild AFK channel configured in Discord
    2) Voice channel with AFK-like name
    3) AFK_CHANNEL_ID from env
    4) 0
    Resolved ids are cached per guild for reuse.
    """
    env_afk = int(AFK_CHANNEL_ID or 0)
    if guild is None:
        return env_afk if env_afk > 0 else 0

    try:
        gid = int(getattr(guild, "id", 0) or 0)
    except Exception:
        gid = 0

    if gid > 0:
        cached = int(_AFK_CHANNEL_CACHE.get(gid, 0) or 0)
        if cached > 0:
            return cached

    try:
        afk_ch = getattr(guild, "afk_channel", None)
        afk_id = int(getattr(afk_ch, "id", 0) or 0)
    except Exception:
        afk_id = 0
    if afk_id > 0:
        if gid > 0:
            _AFK_CHANNEL_CACHE[gid] = afk_id
        return afk_id

    best_match_id = 0
    try:
        voice_channels = list(getattr(guild, "voice_channels", []) or [])
    except Exception:
        voice_channels = []

    for ch in voice_channels:
        name = str(getattr(ch, "name", "") or "")
        if name.strip().lower() == "afk":
            best_match_id = int(getattr(ch, "id", 0) or 0)
            break
        if best_match_id <= 0 and _looks_like_afk_name(name):
            best_match_id = int(getattr(ch, "id", 0) or 0)

    if best_match_id > 0:
        if gid > 0:
            _AFK_CHANNEL_CACHE[gid] = best_match_id
        return best_match_id

    resolved = env_afk if env_afk > 0 else 0
    if gid > 0:
        _AFK_CHANNEL_CACHE[gid] = resolved
    return resolved
