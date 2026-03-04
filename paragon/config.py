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


TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
GUILD_DB_DIR = os.getenv("PARAGON_DB_DIR", "paragon_data")

# IDs / settings
AUTHOR_USER_ID = _as_int("AUTHOR_USER_ID", _as_int("OWNER_USER_ID", 0))
AFK_CHANNEL_ID = _as_int("AFK_CHANNEL_ID", 0)
MAX_LEVEL_ROLE = _as_int("MAX_LEVEL_ROLE", 6)

# Timezone
TZ = os.getenv("PARAGON_TZ", "America/New_York")
LOCAL_TZ = ZoneInfo(TZ)

# XP model
LEVEL_XP_REQUIREMENTS = [100, 200, 300, 400, 500, 1000]
BASE_XP_PER_MINUTE = _as_float("BASE_XP_PER_MINUTE", 1.0)
PRESTIGE_GAIN_BONUS_PER_POINT = _as_float("PRESTIGE_GAIN_BONUS_PER_POINT", 0.10)
INACTIVE_XP_PER_MINUTE_BY_LEVEL = {
    1: -1.0,
    2: -1.5,
    3: -2.0,
    4: -2.5,
    5: -3.0,
    6: -5.0,
}
XP_REWARD_BOOST_MIN_PCT = _as_float("XP_REWARD_BOOST_MIN_PCT", 0.08)     # 8%
XP_REWARD_BOOST_MAX_PCT = _as_float("XP_REWARD_BOOST_MAX_PCT", 2.50)     # 250%
XP_REWARD_BOOST_SCALE = _as_float("XP_REWARD_BOOST_SCALE", 0.22)         # log10 scaling
XP_REWARD_BOOST_MIN_MINUTES = _as_int("XP_REWARD_BOOST_MIN_MINUTES", 10)
XP_REWARD_BOOST_MAX_MINUTES = _as_int("XP_REWARD_BOOST_MAX_MINUTES", 720)

# Blackjack
BJ_MAX_PLAYERS = _as_int("BJ_MAX_PLAYERS", 20)
BJ_MIN_BET = _as_int("BJ_MIN_BET", 1)
BJ_MAX_BET = _as_int("BJ_MAX_BET", 100_000_000_000_000_000_000)

# Prestige
PRESTIGE_COST_XP = _as_int("PRESTIGE_COST_XP", 1000)
PRESTIGE_BOARD_LIMIT = _as_int("PRESTIGE_BOARD_LIMIT", 10)

# XP Shield
XP_SHIELD_ROLE_NAME = os.getenv("XP_SHIELD_ROLE_NAME", "XP Shield")
XP_SHIELD_CHECK_MINUTES = _as_int("XP_SHIELD_CHECK_MINUTES", 1)
MAX_XP_SHIELD_MINUTES = _as_int("MAX_XP_SHIELD_MINUTES", 24 * 60)

# Perm Shield
PERM_SHIELD_ROLE_NAME = os.getenv("PERM_SHIELD_ROLE_NAME", "Perm Shield")
PERM_SHIELD_CHECK_MINUTES = _as_int("PERM_SHIELD_CHECK_MINUTES", 1)
MAX_PERM_SHIELD_MINUTES = _as_int("MAX_PERM_SHIELD_MINUTES", 7 * 24 * 60)

# Lottery
LOTTO_TICKET_COST = _as_int("LOTTO_TICKET_COST", 1)
LOTTO_DRAW_HOUR = _as_int("LOTTO_DRAW_HOUR", 18)
LOTTO_DRAW_MINUTE = _as_int("LOTTO_DRAW_MINUTE", 0)
LOTTO_MIN_LEVEL = _as_int("LOTTO_MIN_LEVEL", 1)
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
DROP_MAX_MINUTES = _as_int("DROP_MAX_MINUTES", 120)
DROP_MIN_XP = _as_int("DROP_MIN_XP", 50)
DROP_MAX_XP = _as_int("DROP_MAX_XP", 150)

# Roulette
ROULETTE_COST_XP = _as_int("ROULETTE_COST_XP", 20)
ROULETTE_TIMEOUT_SECONDS = _as_int("ROULETTE_TIMEOUT_SECONDS", 60)
ROULETTE_SUCCESS_N = _as_int("ROULETTE_SUCCESS_N", 1)
ROULETTE_SUCCESS_OUTOF = _as_int("ROULETTE_SUCCESS_OUTOF", 6)

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
CF_MAX_BET = _as_int("CF_MAX_BET", 10_000_000)
CF_TTL_SECONDS = _as_int("CF_TTL_SECONDS", 120)
