# paragon/anagram.py
from __future__ import annotations
from typing import Optional, List
import os
import random
import hashlib
import math
import re
from datetime import date
import discord
from discord.ext import commands

from .config import (
    ANAGRAM_PHRASES_PATH, ANAGRAM_DAILY_LIMIT,
    ANAGRAM_WIN_XP, ANAGRAM_LOSS_XP,
)
from .spin_support import consume_anagram_reward_multiplier
from .storage import _udict, save_data
from .stats_store import record_game_fields
from .xp import apply_xp_change, grant_prestige_scaled_fixed_boost
from .roles import enforce_level6_exclusive
from .time_windows import _today_local, _date_key          # local day keys:contentReference[oaicite:8]{index=8}

_builtin_phrases = [
    "silver sword", "ocean breeze", "golden apple", "hidden path", "vast horizon",
    "midnight sun", "silent storm", "crystal river", "ancient ruins", "shadow dancer"
]
_phrases: List[str] = []

_word_re = re.compile(r"[A-Za-z]+")
ANAGRAM_FIRST_SOLVE_BOOST_PCT = 1.00
ANAGRAM_FIRST_SOLVE_BOOST_MINUTES = 180
ANAGRAM_SOLVE_DECAY = 0.5


def _normalize_phrase(raw: str) -> str:
    body = raw.split("#", 1)[0].strip()
    if not body:
        return ""
    return " ".join(_word_re.findall(body.lower()))


def _coprime_step(seed: bytes, mod: int) -> int:
    if mod <= 1:
        return 1
    n = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
    step = (n % mod) or 1
    while math.gcd(step, mod) != 1:
        step = (step + 1) % mod
        if step == 0:
            step = 1
    return step


def _sequence_index(user_id: int, position: int, mod: int) -> int:
    if mod <= 1:
        return 0
    seed = f"anagram:{user_id}".encode("utf-8")
    start = int.from_bytes(hashlib.blake2b(seed, digest_size=8).digest(), "big") % mod
    step = _coprime_step(seed + b":step", mod)
    return (start + step * position) % mod

def _load_phrases():
    global _phrases
    if _phrases:
        return
    if os.path.exists(ANAGRAM_PHRASES_PATH):
        with open(ANAGRAM_PHRASES_PATH, "r", encoding="utf-8") as f:
            items = []
            seen = set()
            for line in f:
                s = _normalize_phrase(line)
                if not s or s in seen:
                    continue
                # Expect 2–3 word phrases, but allow any non-empty line
                seen.add(s)
                items.append(s)
        _phrases = items if items else [_normalize_phrase(p) for p in _builtin_phrases]
    else:
        _phrases = [_normalize_phrase(p) for p in _builtin_phrases]

def _canonical(s: str) -> str:
    """Lowercase; collapse all non-letters to single spaces; trim."""
    tokens = _word_re.findall(s.lower())
    return " ".join(tokens)

def _phrase_for(user_id: int, day_key: str, slot: int) -> str:
    _load_phrases()
    try:
        day_ordinal = date.fromisoformat(day_key).toordinal()
    except ValueError:
        day_ordinal = 0
    position = day_ordinal * ANAGRAM_DAILY_LIMIT + max(0, slot)
    idx = _sequence_index(user_id, position, len(_phrases))
    return _phrases[idx]

def _scramble_phrase(phrase: str) -> str:
    """Scramble letters within each word to preserve word count; avoid identity when possible."""
    out_words = []
    for w in phrase.split():
        if len(w) <= 3:
            # tiny words often look the same; try once
            letters = list(w)
            random.shuffle(letters)
            out_words.append("".join(letters))
            continue
        # scramble until different (max few tries)
        letters = list(w)
        for _ in range(6):
            random.shuffle(letters)
            s = "".join(letters)
            if s.lower() != w.lower():
                break
        out_words.append("".join(letters))
    return " ".join(out_words)

def _state(gid: int, uid: int) -> dict:
    """Per-user anagram state under user record."""
    u = _udict(gid, uid)  # ensures user record exists:contentReference[oaicite:9]{index=9}
    st = u.get("anagram")
    if st is None:
        st = {"date": "", "used": 0, "solved": 0, "idx": 0, "awaiting": False, "scrambled": "", "answer": ""}
        u["anagram"] = st
    for k, v in [("used", 0), ("solved", 0), ("idx", 0), ("awaiting", False), ("scrambled", ""), ("answer", "")]:
        if k not in st:
            st[k] = v
    if "date" not in st:
        st["date"] = ""
    return st


def _anagram_boost_profile(solve_number: int) -> tuple[float, int]:
    n = max(1, int(solve_number))
    decay = float(ANAGRAM_SOLVE_DECAY) ** float(n - 1)
    pct = max(0.0, float(ANAGRAM_FIRST_SOLVE_BOOST_PCT) * decay)
    minutes = max(1, int(round(float(ANAGRAM_FIRST_SOLVE_BOOST_MINUTES) * decay)))
    return pct, minutes

class AnagramCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="anagram", aliases=["a"])
    async def anagram(self, ctx: commands.Context, *, guess: Optional[str] = None):
        today = _date_key(_today_local())                   # local date string:contentReference[oaicite:10]{index=10}
        st = _state(ctx.guild.id, ctx.author.id)

        # New day? reset per-day counters and pending puzzle
        if st.get("date") != today:
            st["date"] = today
            st["used"] = 0
            st["solved"] = 0
            st["idx"] = 0
            st["awaiting"] = False
            st["scrambled"] = ""
            st["answer"] = ""
            await save_data()

        # Out of daily puzzles?
        used = int(st.get("used", 0))
        if used >= ANAGRAM_DAILY_LIMIT:
            await ctx.reply(f"🧩 You’ve used **{ANAGRAM_DAILY_LIMIT}/{ANAGRAM_DAILY_LIMIT}** anagrams today. Come back tomorrow!")
            return

        # If no guess provided, (re)show current scrambled or generate a new one
        if guess is None:
            p = ctx.clean_prefix
            if st.get("awaiting") and st.get("scrambled"):
                left = ANAGRAM_DAILY_LIMIT - used
                await ctx.reply(f"🧩 **Anagram:** `{st['scrambled']}`\nAttempts left today: **{used}/{ANAGRAM_DAILY_LIMIT}**\nReply with `{p}a <guess>`.")
                return
            # Generate new puzzle for this slot
            phrase = _phrase_for(ctx.author.id, today, st["idx"])
            scrambled = _scramble_phrase(phrase)
            st["answer"] = phrase
            st["scrambled"] = scrambled
            st["awaiting"] = True
            record_game_fields(ctx.guild.id, ctx.author.id, "anagram", puzzles_started=1)
            await save_data()
            await ctx.reply(f"🧩 **Anagram:** `{scrambled}`\nAttempts used today: **{used}/{ANAGRAM_DAILY_LIMIT}**\nReply with `{p}a <guess>`.")
            return

        # A guess was provided
        if not st.get("awaiting"):
            await ctx.reply(f"No active puzzle. Type `{ctx.clean_prefix}a` to get one first.")
            return

        answer = st.get("answer", "")
        norm_guess = _canonical(guess)
        norm_answer = _canonical(answer)
        st["used"] = used + 1
        st["idx"] = int(st.get("idx", 0)) + 1
        st["awaiting"] = False
        st["scrambled"] = ""
        st["answer"] = ""
        await save_data()

        # Check correctness (single attempt per puzzle)
        if norm_guess == norm_answer:
            solve_number = int(st.get("solved", 0)) + 1
            st["solved"] = solve_number
            wheel_mult = float(consume_anagram_reward_multiplier(ctx.guild.id, ctx.author.id))
            profile_pct, profile_minutes = _anagram_boost_profile(solve_number)
            reward_seed = int(max(1, round(float(ANAGRAM_WIN_XP) * float(profile_pct) * float(profile_minutes))))
            boost = await grant_prestige_scaled_fixed_boost(
                ctx.author,
                pct=profile_pct,
                minutes=profile_minutes,
                source="anagram solve",
                reward_seed_xp=reward_seed,
                flat_multiplier=wheel_mult,
            )
            record_game_fields(
                ctx.guild.id,
                ctx.author.id,
                "anagram",
                solves=1,
                boost_seed_xp_total=reward_seed,
                boost_percent_total=boost["percent"],
                boost_minutes_total=boost["minutes"],
            )
            await enforce_level6_exclusive(ctx.guild)
            wheel_line = ""
            if wheel_mult > 1.0:
                wheel_line = (
                    f" Wheel buff: x{wheel_mult:.2f} final buff scaling "
                    f"(+{boost['prestige_scaled_percent']:.1f}%/{boost['prestige_scaled_minutes']}m "
                    f"-> +{boost['percent']:.1f}%/{boost['minutes']}m)."
                )
            await ctx.reply(
                f"✅ Correct! Boost gained: **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**. "
                f"Solve **#{solve_number}** profile."
                f" "
                f"Progress: **{st['used']}/{ANAGRAM_DAILY_LIMIT}**."
                f"{wheel_line}"
            )
            return

        # Wrong → −XP
        await apply_xp_change(ctx.author, -ANAGRAM_LOSS_XP, source="anagram fail")
        record_game_fields(ctx.guild.id, ctx.author.id, "anagram", fails=1)
        await enforce_level6_exclusive(ctx.guild)
        await ctx.reply(
            f"❌ Not quite. The answer was **{answer}**. **−{ANAGRAM_LOSS_XP} XP**.\n"
            f"Progress: **{st['used']}/{ANAGRAM_DAILY_LIMIT}**."
        )
