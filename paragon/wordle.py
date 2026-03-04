from typing import Optional
import hashlib
import math
import discord
from datetime import datetime, timezone
from discord.ext import commands, tasks
from .config import (
    WORDLE_WORD_LENGTH, WORDLE_WIN_XP, WORDLE_LOSS_XP, WORDLE_MAX_GUESSES,
    WORDLE_RESPECT_ACTIVE_HOURS, WORDLE_WORDLIST_PATH, WORDLE_VALID_GUESSES_PATH,
    WORD_REGEX
)
from .guild_setup import get_log_channel
from .time_windows import _today_local, _date_key, is_active_hours
from .storage import _udict, save_data
from .stats_store import record_game_fields
from .xp import apply_xp_change, grant_reward_boost
from .roles import enforce_level6_exclusive
from .ownership import owner_only

_builtin_wordlist = [
    "apple","table","crane","track","flame","prism","glove","knock","sugar","storm",
    "river","mouse","cable","tiger","lemon","spice","crown","frame","brick","plane",
    "robot","magic","night","light","sound","earth","ocean","amber","bronze","silver",
    "vivid","cider","grape","pearl","flint","charm","shade","blush","brisk","chess",
]
_wordlist: list[str] = []
_guesslist: list[str] = []

def _load_file_words(path: str) -> list[str]:
    import os
    if not os.path.exists(path): return []
    words = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.split("#", 1)[0].strip()
            if not raw:
                continue
            w = raw.lower()
            if WORD_REGEX.match(w): words.append(w)
    return sorted(set(words))


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

def load_wordlist():
    global _wordlist
    words = _load_file_words(WORDLE_WORDLIST_PATH)
    _wordlist = words if words else list(sorted(set(_builtin_wordlist)))

def load_guesslist():
    global _guesslist
    words = _load_file_words(WORDLE_VALID_GUESSES_PATH)
    if not words:
        if not _wordlist: load_wordlist()
        words = list(_wordlist)
    if not _wordlist: load_wordlist()
    _guesslist = sorted(set(words + _wordlist))

def daily_target_for(user_id: int, dt_local: datetime) -> str:
    if not _wordlist: load_wordlist()
    mod = len(_wordlist)
    day_ordinal = dt_local.date().toordinal()
    seed = f"wordle:{user_id}".encode("utf-8")
    start = int.from_bytes(hashlib.blake2b(seed, digest_size=8).digest(), "big") % mod
    step = _coprime_step(seed + b":step", mod)
    idx = (start + step * day_ordinal) % mod
    return _wordlist[idx]

def wordle_compare(guess: str, target: str) -> tuple[str, list[str]]:
    guess = guess.lower(); target = target.lower()
    remaining = {}
    for ch in target: remaining[ch] = remaining.get(ch, 0) + 1
    marks = ['b'] * WORDLE_WORD_LENGTH
    for i, ch in enumerate(guess):
        if ch == target[i]:
            marks[i] = 'g'; remaining[ch] -= 1
    for i, ch in enumerate(guess):
        if marks[i] == 'g': continue
        if remaining.get(ch, 0) > 0:
            marks[i] = 'y'; remaining[ch] -= 1
    to_emoji = {'g':'🟩','y':'🟨','b':'⬛'}
    row = "".join(to_emoji[m] for m in marks)
    return row, marks

def _user_wordle_state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("wordle")
    if st is None:
        st = {"date":"", "guesses":[], "done":False, "win":False, "letters":[]}
        u["wordle"] = st
    if "letters" not in st: st["letters"] = []
    return st

def _reset_daily_wordle_state(st: dict, today: str):
    st["date"] = today; st["guesses"] = []; st["done"] = False; st["win"] = False; st["letters"] = []

def format_guessed_letters_line(letters: list[str], target: str) -> str:
    if not letters: return "🟩: (none)\n🟥: (none)"
    tset = set(target.lower())
    uniq = sorted(set(ch.lower() for ch in letters if ch.isalpha()))
    greens = [ch.upper() for ch in uniq if ch in tset]
    reds = [ch.upper() for ch in uniq if ch not in tset]
    green_line = " ".join(greens) if greens else "(none)"
    red_line = " ".join(reds) if reds else "(none)"
    return f"🟩: {green_line}\n🟥: {red_line}"

class WordleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        if not hasattr(self.bot, "_last_wordle_date"):
            from .time_windows import _today_local, _date_key
            self.bot._last_wordle_date = _date_key(_today_local())
        if not self.wordle_reset_notifier.is_running():
            self.wordle_reset_notifier.start()

    @tasks.loop(minutes=1)
    async def wordle_reset_notifier(self):
        now_local = _today_local(); today = _date_key(now_local)
        last_date = getattr(self.bot, "_last_wordle_date", None)
        if last_date == today: return
        self.bot._last_wordle_date = today
        for guild in self.bot.guilds:
            ch = get_log_channel(guild)
            if not ch: continue
            if not ch.permissions_for(guild.me).send_messages: continue
            try:
                await ch.send(f"📅 Wordle has reset for **{today}** — everyone has a new word!")
            except Exception:
                pass

    @commands.command(name="resetwordle")
    @owner_only()
    async def resetwordle(self, ctx: commands.Context):
        g = ctx.guild; today = _date_key(_today_local())
        from .storage import _gdict
        users = _gdict(g.id).get("users", {})
        cnt = 0
        for u in users.values():
            if "wordle" in u:
                st = u["wordle"]; _reset_daily_wordle_state(st, today); cnt += 1
        await save_data()
        await ctx.reply(f"Reset Wordle guesses + letters for {cnt} user(s) today.")

    @commands.command(name="wordle", aliases=["w","wd"])
    async def wordle(self, ctx: commands.Context, guess: Optional[str] = None):
        from .time_windows import _today_local, _date_key, is_active_hours
        if WORDLE_RESPECT_ACTIVE_HOURS and not is_active_hours(datetime.now(timezone.utc)):
            await ctx.reply("Wordle is available, but XP changes only during active hours. Try again during active hours to play for XP.")
            return
        if not _wordlist: load_wordlist()
        if not _guesslist: load_guesslist()

        st = _user_wordle_state(ctx.guild.id, ctx.author.id)
        today = _date_key(_today_local())
        if st.get("date") != today:
            _reset_daily_wordle_state(st, today)
        target = daily_target_for(ctx.author.id, _today_local())

        if guess is None:
            rows = []
            for g in st["guesses"]:
                row, _ = wordle_compare(g, target)
                rows.append(f"{row}  `{g}`")
            board = "\n".join(rows) if rows else "No guesses yet today."
            status = ("✅ You already solved today’s Wordle!" if st["win"]
                      else f"❌ You already failed today’s Wordle. The word was **{target}**.") if st["done"] \
                     else f"{WORDLE_MAX_GUESSES - len(st['guesses'])} guesses left."
            letters_line = format_guessed_letters_line(st.get("letters", []), target)
            await ctx.reply(f"{status}\n{board}\n{letters_line}")
            return

        if st["done"]:
            msg = "✅ You already solved today’s Wordle!" if st["win"] else f"❌ You already failed today’s Wordle. The word was **{target}**."
            await ctx.reply(msg); return

        guess = guess.strip().lower()
        if not WORD_REGEX.match(guess):
            await ctx.reply(f"Guesses must be exactly {WORDLE_WORD_LENGTH} letters (A–Z)."); return
        if guess not in _guesslist:
            await ctx.reply(f"That word isn't in my {WORDLE_WORD_LENGTH}-letter allowed list. Try another."); return

        st["guesses"].append(guess)
        if len(st["guesses"]) == 1:
            record_game_fields(ctx.guild.id, ctx.author.id, "wordle", puzzles_played=1)
        record_game_fields(ctx.guild.id, ctx.author.id, "wordle", guesses_submitted=1)
        for ch in guess:
            if ch not in st["letters"]:
                st["letters"].append(ch)
        st["letters"].sort()

        row, _ = wordle_compare(guess, target)

        if guess == target:
            st["done"] = True; st["win"] = True
            boost = await grant_reward_boost(ctx.author, WORDLE_WIN_XP, source="wordle clear")
            record_game_fields(
                ctx.guild.id,
                ctx.author.id,
                "wordle",
                wins=1,
                boost_seed_xp_total=WORDLE_WIN_XP,
                boost_percent_total=boost["percent"],
                boost_minutes_total=boost["minutes"],
            )
            await enforce_level6_exclusive(ctx.guild)
            await ctx.reply(
                f"{row}  `{guess}`\n**Correct!** Boost gained: "
                f"**+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
            )
            return

        remaining = WORDLE_MAX_GUESSES - len(st["guesses"])
        letters_line = format_guessed_letters_line(st["letters"], target)

        if remaining == 0:
            st["done"] = True; st["win"] = False
            await apply_xp_change(ctx.author, -WORDLE_LOSS_XP, source="wordle fail")
            record_game_fields(ctx.guild.id, ctx.author.id, "wordle", losses=1)
            await enforce_level6_exclusive(ctx.guild)
            await ctx.reply(f"{row}  `{guess}`\n**Out of guesses!** The word was **{target}**. −{WORDLE_LOSS_XP} XP.")
            return

        await save_data()
        await ctx.reply(f"{row}  `{guess}`\n{remaining} guesses left.\n{letters_line}")
