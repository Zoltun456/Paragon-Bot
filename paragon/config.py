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


def _as_float_tuple(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return tuple(default)
    values = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        values.append(float(token))
    return tuple(values) if values else tuple(default)


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
PRESTIGE_BASE_STEP_LEVELS = _as_int("PRESTIGE_BASE_STEP_LEVELS", 5)
PRESTIGE_BASE_STEP_XP_PER_MIN = _as_float("PRESTIGE_BASE_STEP_XP_PER_MIN", 1.0)
PRESTIGE_COST_C0 = _as_float("PRESTIGE_COST_C0", 120.0)
PRESTIGE_COST_A = _as_float("PRESTIGE_COST_A", 0.45)
PRESTIGE_COST_B = _as_float("PRESTIGE_COST_B", 0.065)
PRESTIGE_MAX_BASE_PROGRESS_MINUTES = _as_int("PRESTIGE_MAX_BASE_PROGRESS_MINUTES", 24 * 60 * 2)
PRESTIGE_RATE_K = _as_float("PRESTIGE_RATE_K", 0.025)
_PRESTIGE_COMPRESSION_MODE = os.getenv("PRESTIGE_COMPRESSION_MODE", "progress_only").strip().lower()
PRESTIGE_COMPRESSION_MODE = (
    _PRESTIGE_COMPRESSION_MODE
    if _PRESTIGE_COMPRESSION_MODE in {"progress_only", "global", "off"}
    else "progress_only"
)
PRESTIGE_STACK_SOFTCAP = _as_float("PRESTIGE_STACK_SOFTCAP", 6.0)
BOOST_VALUE_PREFERRED_PCTS = _as_float_tuple(
    "BOOST_VALUE_PREFERRED_PCTS",
    (0.50, 1.00, 0.25, 1.50, 2.00, 3.00, 4.00),
)
BOOST_VALUE_MAX_MINUTES = _as_int("BOOST_VALUE_MAX_MINUTES", 720)
BOOST_VALUE_PCT_ROUND_STEP = _as_float("BOOST_VALUE_PCT_ROUND_STEP", 0.25)

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
BJ_COOLDOWN_ENABLED = _as_bool("BJ_COOLDOWN_ENABLED", False)

# Prestige
PRESTIGE_BOARD_LIMIT = _as_int("PRESTIGE_BOARD_LIMIT", 10)

# Lottery
LOTTO_TICKET_COST = _as_int("LOTTO_TICKET_COST", 1)
LOTTO_DRAW_HOUR = _as_int("LOTTO_DRAW_HOUR", 18)
LOTTO_DRAW_MINUTE = _as_int("LOTTO_DRAW_MINUTE", 0)
LOTTO_MAX_PER_USER = _as_int("LOTTO_MAX_PER_USER", 5_000)
LOTTO_JACKPOT_POT_BOOST_MULTIPLIER = _as_float("LOTTO_JACKPOT_POT_BOOST_MULTIPLIER", 5.0)

# Thanks
THANKS_REWARD_SEED_XP = _as_int("THANKS_REWARD_SEED_XP", _as_int("THANKS_GIFT_XP", 20))
THANKS_BOOST_PCT = _as_float("THANKS_BOOST_PCT", 2.00)
THANKS_BOOST_MINUTES = _as_int("THANKS_BOOST_MINUTES", 60)

# Anagram
ANAGRAM_PHRASES_PATH = _resolve_data_path(
    "ANAGRAM_PHRASES_PATH",
    "anagram_phrases.txt",
    "OLD/anagram_phrases.txt",
)
ANAGRAM_DAILY_LIMIT = _as_int("ANAGRAM_DAILY_LIMIT", 10)
ANAGRAM_SOLVE_ADD_PCT = _as_float("ANAGRAM_SOLVE_ADD_PCT", 0.20)
ANAGRAM_SOLVE_ADD_MINUTES = _as_int("ANAGRAM_SOLVE_ADD_MINUTES", 60)
ANAGRAM_SOLVE_MAX_PCT = _as_float("ANAGRAM_SOLVE_MAX_PCT", 2.00)
ANAGRAM_SOLVE_MAX_MINUTES = _as_int("ANAGRAM_SOLVE_MAX_MINUTES", 600)
ANAGRAM_FAIL_ADD_PCT = _as_float("ANAGRAM_FAIL_ADD_PCT", 0.10)
ANAGRAM_FAIL_ADD_MINUTES = _as_int("ANAGRAM_FAIL_ADD_MINUTES", 60)
ANAGRAM_FAIL_MAX_PCT = _as_float("ANAGRAM_FAIL_MAX_PCT", 1.00)
ANAGRAM_FAIL_MAX_MINUTES = _as_int("ANAGRAM_FAIL_MAX_MINUTES", 600)

# Surprise Drops
DROP_MIN_MINUTES = _as_int("DROP_MIN_MINUTES", 30)
DROP_MAX_MINUTES = _as_int("DROP_MAX_MINUTES", 240)
DROP_MIN_XP = _as_int("DROP_MIN_XP", 50)
DROP_MAX_XP = _as_int("DROP_MAX_XP", 150)
SURPRISE_MIN_PCT = _as_float("SURPRISE_MIN_PCT", 0.50)
SURPRISE_MAX_PCT = _as_float("SURPRISE_MAX_PCT", 2.00)
SURPRISE_PCT_STEP = _as_float("SURPRISE_PCT_STEP", 0.10)
SURPRISE_BOOST_MINUTES = _as_int("SURPRISE_BOOST_MINUTES", 60)

# Wordle
WORDLE_WORD_LENGTH = _as_int("WORDLE_WORD_LENGTH", 5)
WORDLE_MAX_GUESSES = _as_int("WORDLE_MAX_GUESSES", 5)
WORDLE_WIN_PCTS = _as_float_tuple("WORDLE_WIN_PCTS", (5.0, 4.0, 3.0, 2.0, 1.0))
WORDLE_WIN_MINUTES = _as_int("WORDLE_WIN_MINUTES", 600)
WORDLE_FAIL_PCT = _as_float("WORDLE_FAIL_PCT", 0.50)
WORDLE_FAIL_MINUTES = _as_int("WORDLE_FAIL_MINUTES", 60)
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
CF_POT_BOOST_MULTIPLIER = _as_float("CF_POT_BOOST_MULTIPLIER", 5.0)

# Roulette
ROULETTE_COOLDOWN_SECONDS = _as_int("ROULETTE_COOLDOWN_SECONDS", 30 * 60)
ROULETTE_BASE_SUCCESS_CHANCE = _as_float("ROULETTE_BASE_SUCCESS_CHANCE", 0.20)
ROULETTE_GAP_STEP_CHANCE = _as_float("ROULETTE_GAP_STEP_CHANCE", 0.025)
ROULETTE_MIN_SUCCESS_CHANCE = _as_float("ROULETTE_MIN_SUCCESS_CHANCE", 0.025)
ROULETTE_MAX_SUCCESS_CHANCE = _as_float("ROULETTE_MAX_SUCCESS_CHANCE", 0.60)
ROULETTE_MIN_TIMEOUT_SECONDS = _as_int("ROULETTE_MIN_TIMEOUT_SECONDS", 30)
ROULETTE_MAX_TIMEOUT_SECONDS = _as_int("ROULETTE_MAX_TIMEOUT_SECONDS", 3 * 60)

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

# Contracts
CONTRACT_AUTO_ASSIGN_ON_COMMAND = _as_bool("CONTRACT_AUTO_ASSIGN_ON_COMMAND", True)
CONTRACT_REWARD_MULTIPLIER = _as_float("CONTRACT_REWARD_MULTIPLIER", 1.0)
CONTRACT_REWARD_BASE_MINUTES = _as_int("CONTRACT_REWARD_BASE_MINUTES", 540)
CONTRACT_REWARD_PER_DIFFICULTY_MINUTES = _as_int("CONTRACT_REWARD_PER_DIFFICULTY_MINUTES", 180)
CONTRACT_REWARD_STEP_SYNERGY_MINUTES = _as_int("CONTRACT_REWARD_STEP_SYNERGY_MINUTES", 120)
CONTRACT_REWARD_MIN_MINUTES = _as_int("CONTRACT_REWARD_MIN_MINUTES", 720)
CONTRACT_REWARD_MAX_MINUTES = _as_int("CONTRACT_REWARD_MAX_MINUTES", 3600)
CONTRACT_MAJOR_MIN_DIFFICULTY = _as_int("CONTRACT_MAJOR_MIN_DIFFICULTY", 4)
CONTRACT_MAJOR_MIN_STEPS = _as_int("CONTRACT_MAJOR_MIN_STEPS", 2)
CONTRACT_ELITE_MIN_DIFFICULTY = _as_int("CONTRACT_ELITE_MIN_DIFFICULTY", 6)
CONTRACT_ELITE_MIN_STEPS = _as_int("CONTRACT_ELITE_MIN_STEPS", 3)
CONTRACT_LEGENDARY_MIN_DIFFICULTY = _as_int("CONTRACT_LEGENDARY_MIN_DIFFICULTY", 9)

# Shop
SHOP_COST_ROUND_STEP = _as_int("SHOP_COST_ROUND_STEP", 10)
SHOP_WHEEL_SPIN_START_PCT = _as_int("SHOP_WHEEL_SPIN_START_PCT", 40)
SHOP_WHEEL_SPIN_STEP_PCT = _as_int("SHOP_WHEEL_SPIN_STEP_PCT", 15)
SHOP_WHEEL_SPIN_STEP_GROWTH_PCT = _as_int("SHOP_WHEEL_SPIN_STEP_GROWTH_PCT", 5)
SHOP_CLEANSE_START_PCT = _as_int("SHOP_CLEANSE_START_PCT", 50)
SHOP_CLEANSE_STEP_PCT = _as_int("SHOP_CLEANSE_STEP_PCT", 20)
SHOP_CLEANSE_STEP_GROWTH_PCT = _as_int("SHOP_CLEANSE_STEP_GROWTH_PCT", 5)
SHOP_ROULETTE_SHIELD_START_PCT = _as_int("SHOP_ROULETTE_SHIELD_START_PCT", 60)
SHOP_ROULETTE_SHIELD_STEP_PCT = _as_int("SHOP_ROULETTE_SHIELD_STEP_PCT", 25)
SHOP_ROULETTE_SHIELD_STEP_GROWTH_PCT = _as_int("SHOP_ROULETTE_SHIELD_STEP_GROWTH_PCT", 5)
SHOP_ROULETTE_ACCURACY_START_PCT = _as_int("SHOP_ROULETTE_ACCURACY_START_PCT", 70)
SHOP_ROULETTE_ACCURACY_STEP_PCT = _as_int("SHOP_ROULETTE_ACCURACY_STEP_PCT", 30)
SHOP_ROULETTE_ACCURACY_STEP_GROWTH_PCT = _as_int("SHOP_ROULETTE_ACCURACY_STEP_GROWTH_PCT", 5)
SHOP_ROULETTE_ACCURACY_BONUS_CHANCE = _as_float("SHOP_ROULETTE_ACCURACY_BONUS_CHANCE", 0.20)

# Boss Raids
BOSS_ENABLED = _as_bool("BOSS_ENABLED", True)
BOSS_SPAWN_MIN_DAYS = _as_int("BOSS_SPAWN_MIN_DAYS", 1)
BOSS_SPAWN_MAX_DAYS = _as_int("BOSS_SPAWN_MAX_DAYS", 3)
BOSS_SPAWN_START_HOUR = _as_int("BOSS_SPAWN_START_HOUR", 10)
BOSS_SPAWN_END_HOUR = _as_int("BOSS_SPAWN_END_HOUR", 20)
BOSS_IDLE_MAX_HOURS = _as_int("BOSS_IDLE_MAX_HOURS", 12)
BOSS_DURATION_MIN_MINUTES = _as_int("BOSS_DURATION_MIN_MINUTES", 120)
BOSS_DURATION_MAX_MINUTES = _as_int("BOSS_DURATION_MAX_MINUTES", 240)
BOSS_ATTACK_COOLDOWN_SECONDS = _as_int("BOSS_ATTACK_COOLDOWN_SECONDS", 60)
BOSS_RES_COOLDOWN_SECONDS = _as_int("BOSS_RES_COOLDOWN_SECONDS", 90)
BOSS_TARGET_MEMBER_DIVISOR = _as_int("BOSS_TARGET_MEMBER_DIVISOR", 4)
BOSS_AVG_PRESTIGE_OFFSET = _as_float("BOSS_AVG_PRESTIGE_OFFSET", 2.0)
BOSS_HP_BASE = _as_int("BOSS_HP_BASE", 120)
BOSS_HP_PER_TARGET_FIGHTER = _as_int("BOSS_HP_PER_TARGET_FIGHTER", 80)
BOSS_HP_PER_BOSS_PRESTIGE = _as_int("BOSS_HP_PER_BOSS_PRESTIGE", 8)
BOSS_DAMAGE_MIN = _as_int("BOSS_DAMAGE_MIN", 1)
BOSS_DAMAGE_MAX = _as_int("BOSS_DAMAGE_MAX", 3)
BOSS_DAMAGE_PRESTIGE_STEP = _as_int("BOSS_DAMAGE_PRESTIGE_STEP", 20)
BOSS_RETALIATE_DEBUFF_MIN_PCT = _as_float("BOSS_RETALIATE_DEBUFF_MIN_PCT", 0.20)
BOSS_RETALIATE_DEBUFF_MAX_PCT = _as_float("BOSS_RETALIATE_DEBUFF_MAX_PCT", 0.60)
BOSS_RETALIATE_DEBUFF_MIN_MINUTES = _as_int("BOSS_RETALIATE_DEBUFF_MIN_MINUTES", 30)
BOSS_RETALIATE_DEBUFF_MAX_MINUTES = _as_int("BOSS_RETALIATE_DEBUFF_MAX_MINUTES", 180)
BOSS_RETALIATE_TIMEOUT_MIN_SECONDS = _as_int("BOSS_RETALIATE_TIMEOUT_MIN_SECONDS", 30)
BOSS_RETALIATE_TIMEOUT_MAX_SECONDS = _as_int("BOSS_RETALIATE_TIMEOUT_MAX_SECONDS", 180)
BOSS_RETALIATE_DOWN_CHANCE = _as_float("BOSS_RETALIATE_DOWN_CHANCE", 0.08)
BOSS_VICTORY_BOOST_PCT = _as_float("BOSS_VICTORY_BOOST_PCT", 6.0)
BOSS_VICTORY_BOOST_MINUTES = _as_int("BOSS_VICTORY_BOOST_MINUTES", 360)
BOSS_FAILURE_DEBUFF_PCT = _as_float("BOSS_FAILURE_DEBUFF_PCT", 0.85)
BOSS_FAILURE_DEBUFF_MINUTES = _as_int("BOSS_FAILURE_DEBUFF_MINUTES", 360)


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
