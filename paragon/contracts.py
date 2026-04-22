from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import random
from typing import Callable, Optional

import discord
from discord.ext import commands

from .config import (
    CONTRACT_AUTO_ASSIGN_ON_COMMAND,
    CONTRACT_ELITE_MIN_DIFFICULTY,
    CONTRACT_ELITE_MIN_STEPS,
    CONTRACT_LEGENDARY_MIN_DIFFICULTY,
    CONTRACT_MAJOR_MIN_DIFFICULTY,
    CONTRACT_MAJOR_MIN_STEPS,
    CONTRACT_REWARD_BASE_MINUTES,
    CONTRACT_REWARD_MAX_MINUTES,
    CONTRACT_REWARD_MIN_MINUTES,
    CONTRACT_REWARD_MULTIPLIER,
    CONTRACT_REWARD_PER_DIFFICULTY_MINUTES,
    CONTRACT_REWARD_STEP_SYNERGY_MINUTES,
)
from .stats_store import get_user_stats, record_game_fields
from .storage import _udict, save_data
from .time_windows import _date_key, _today_local
from .xp import grant_bonus_xp_equivalent_boost, prestige_passive_rate

CONTRACT_VERSION = 1


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _fmt_num(value: int | float) -> str:
    f = _as_float(value, 0.0)
    if abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}"
    return f"{f:,.2f}"


def _fmt_duration_minutes(minutes: int) -> str:
    total = max(0, int(minutes))
    hours, mins = divmod(total, 60)
    if hours <= 0:
        return f"{mins}m"
    if mins <= 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _contract_state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("contracts")
    if not isinstance(st, dict):
        st = {}
        u["contracts"] = st
    st.setdefault("date", "")
    st.setdefault("claimed", False)
    st.setdefault("assigned_at", "")
    st.setdefault("completed_at", "")
    st.setdefault("quest", {})
    st.setdefault("baselines", {})
    st.setdefault("last_reward", {})
    st["quest"] = _as_dict(st.get("quest"))
    st["baselines"] = _as_dict(st.get("baselines"))
    st["last_reward"] = _as_dict(st.get("last_reward"))
    return st


def _objective(metric: str, target: int, label: str, points: int) -> dict[str, object]:
    return {
        "metric": str(metric).strip(),
        "target": max(1, int(target)),
        "label": str(label).strip(),
        "points": max(1, int(points)),
    }


def _user_prestige(gid: int, uid: int) -> int:
    u = _udict(gid, uid)
    return max(0, int(u.get("prestige", 0)))


def _rng_for_contract(gid: int, uid: int, date_key: str) -> random.Random:
    seed_input = f"contracts:v{CONTRACT_VERSION}:{gid}:{uid}:{date_key}".encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_input).digest()[:8], "big")
    return random.Random(seed)


def _metric_value_from_stats(stats: dict, metric: str) -> float:
    parts = str(metric or "").strip().split(":")
    if len(parts) != 3:
        return 0.0

    kind, section, field = parts
    if kind == "game":
        games = _as_dict(_as_dict(stats).get("games"))
        return _as_float(_as_dict(games.get(section)).get(field, 0.0))
    if kind == "xp":
        xp = _as_dict(_as_dict(stats).get("xp"))
        return _as_float(xp.get(field, 0.0))
    return 0.0


def _metric_value(gid: int, uid: int, metric: str) -> float:
    stats = get_user_stats(gid, uid)
    return _metric_value_from_stats(stats, metric)


def _progress_rows(gid: int, uid: int, quest: dict, baselines: dict) -> list[dict[str, object]]:
    stats = get_user_stats(gid, uid)
    rows: list[dict[str, object]] = []
    for obj in _as_list(quest.get("objectives")):
        data = _as_dict(obj)
        metric = str(data.get("metric", "")).strip()
        target = max(1, int(data.get("target", 1)))
        base = _as_float(_as_dict(baselines).get(metric, 0.0))
        cur = max(0.0, _metric_value_from_stats(stats, metric) - base)
        rows.append(
            {
                "metric": metric,
                "label": str(data.get("label", "Objective")).strip() or "Objective",
                "target": target,
                "current": cur,
                "done": bool(cur >= float(target)),
                "points": max(1, int(data.get("points", 1))),
            }
        )
    return rows


def _is_complete(rows: list[dict[str, object]]) -> bool:
    return bool(rows) and all(bool(row.get("done", False)) for row in rows)


def _tier_name(difficulty: int, step_count: int) -> str:
    diff = max(1, int(difficulty))
    steps = max(1, int(step_count))
    major_diff = max(1, int(CONTRACT_MAJOR_MIN_DIFFICULTY))
    major_steps = max(1, int(CONTRACT_MAJOR_MIN_STEPS))
    elite_diff = max(major_diff, int(CONTRACT_ELITE_MIN_DIFFICULTY))
    elite_steps = max(major_steps, int(CONTRACT_ELITE_MIN_STEPS))
    legendary_diff = max(elite_diff, int(CONTRACT_LEGENDARY_MIN_DIFFICULTY))

    if diff >= legendary_diff:
        return "Legendary"
    if diff >= elite_diff or steps >= elite_steps:
        return "Elite"
    if diff >= major_diff or steps >= major_steps:
        return "Major"
    return "Standard"


def _finalize_contract(base: dict[str, object], prestige_level: int) -> dict[str, object]:
    contract = dict(base)
    objectives = [_as_dict(obj) for obj in _as_list(contract.get("objectives"))]
    contract["objectives"] = objectives

    difficulty = sum(max(1, int(obj.get("points", 1))) for obj in objectives)
    step_count = len(objectives)
    tier = _tier_name(difficulty, step_count)

    reward_minutes_equivalent = (
        int(CONTRACT_REWARD_BASE_MINUTES)
        + (int(CONTRACT_REWARD_PER_DIFFICULTY_MINUTES) * difficulty)
        + (int(CONTRACT_REWARD_STEP_SYNERGY_MINUTES) * max(0, step_count - 1) * step_count)
    )
    reward_minutes_equivalent = int(
        round(float(reward_minutes_equivalent) * max(0.0, float(CONTRACT_REWARD_MULTIPLIER)))
    )
    reward_min = max(1, int(CONTRACT_REWARD_MIN_MINUTES))
    reward_max = max(reward_min, int(CONTRACT_REWARD_MAX_MINUTES))
    reward_minutes_equivalent = max(reward_min, min(reward_max, int(reward_minutes_equivalent)))

    rate_basis = max(0.01, float(prestige_passive_rate(prestige_level)))
    reward_bonus_xp = max(1, int(round(rate_basis * float(reward_minutes_equivalent))))

    contract["version"] = int(CONTRACT_VERSION)
    contract["difficulty"] = int(difficulty)
    contract["step_count"] = int(step_count)
    contract["tier"] = str(tier)
    contract["reward_minutes_equivalent"] = int(reward_minutes_equivalent)
    contract["reward_bonus_xp"] = int(reward_bonus_xp)
    contract["reward_rate_basis_per_min"] = float(rate_basis)
    return contract


def _archive_sweep(rng: random.Random, prestige_level: int) -> dict[str, object]:
    target = 3 if rng.random() < 0.6 else 4
    return {
        "key": "archive_sweep",
        "title": "Archive Sweep",
        "flavor": "Shake the dust off the dictionary and put a few solid Wordle guesses on the board.",
        "objectives": [
            _objective(
                "game:wordle:guesses_submitted",
                target,
                f"Submit **{target}** valid Wordle guess(es)",
                2 if target <= 3 else 3,
            )
        ],
    }


def _anagram_sprint(rng: random.Random, prestige_level: int) -> dict[str, object]:
    target = 2 if rng.random() < 0.7 else 3
    return {
        "key": "anagram_sprint",
        "title": "Anagram Sprint",
        "flavor": "The contract board wants quick thinking, not perfect thinking. Keep solving until the stack bends.",
        "objectives": [
            _objective(
                "game:anagram:solves",
                target,
                f"Solve **{target}** anagram puzzle(s)",
                4 if target <= 2 else 5,
            )
        ],
    }


def _wheel_and_numbers(rng: random.Random, prestige_level: int) -> dict[str, object]:
    target = rng.choice([10, 15, 20]) + min(10, (max(0, prestige_level) // 15) * 5)
    return {
        "key": "wheel_and_numbers",
        "title": "Wheel And Numbers",
        "flavor": "The bookie wants motion. Spin the wheel, then throw some weight into the lotto pool.",
        "objectives": [
            _objective("game:spin:spins", 1, "Spin the wheel **1** time", 2),
            _objective(
                "game:lotto:tickets_bought",
                target,
                f"Buy **{target}** lotto ticket(s)",
                2 if target <= 10 else (3 if target <= 20 else 4),
            ),
        ],
    }


def _open_table(rng: random.Random, prestige_level: int) -> dict[str, object]:
    target = 1 if rng.random() < 0.7 else 2
    return {
        "key": "open_table",
        "title": "Open Table",
        "flavor": "Seat yourself, put chips on the felt, and survive a few rounds of blackjack chaos.",
        "objectives": [
            _objective(
                "game:blackjack:rounds_played",
                target,
                f"Play **{target}** blackjack round(s)",
                3 if target <= 1 else 4,
            )
        ],
    }


def _loaded_chamber(rng: random.Random, prestige_level: int) -> dict[str, object]:
    target = 1 if rng.random() < 0.7 else 2
    return {
        "key": "loaded_chamber",
        "title": "Loaded Chamber",
        "flavor": "The contract is simple: step up, call your shot, and let roulette sort out the nerves.",
        "objectives": [
            _objective(
                "game:roulette:plays",
                target,
                f"Use roulette **{target}** time(s)",
                3 if target <= 1 else 4,
            )
        ],
    }


def _call_it(rng: random.Random, prestige_level: int) -> dict[str, object]:
    target = 1 if rng.random() < 0.65 else 2
    return {
        "key": "call_it",
        "title": "Call It",
        "flavor": "Find someone willing to gamble with you and let the coin decide who gets the glory.",
        "objectives": [
            _objective(
                "game:coinflip:matches_played",
                target,
                f"Play **{target}** coinflip match(es)",
                3 if target <= 1 else 4,
            )
        ],
    }


def _open_challenge(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "open_challenge",
        "title": "Open Challenge",
        "flavor": "Post a line, bait the room, and make sure someone actually accepts it.",
        "objectives": [
            _objective("game:coinflip:challenges_created", 1, "Create **1** coinflip challenge", 2),
            _objective("game:coinflip:matches_played", 1, "Complete **1** coinflip match", 2),
        ],
    }


def _puzzle_circuit(rng: random.Random, prestige_level: int) -> dict[str, object]:
    guess_target = 3 if rng.random() < 0.5 else 4
    solve_target = 1 if rng.random() < 0.55 else 2
    return {
        "key": "puzzle_circuit",
        "title": "Puzzle Circuit",
        "flavor": "Bounce between the word grids and the scramble pile until both sides of your brain are warmed up.",
        "objectives": [
            _objective(
                "game:wordle:guesses_submitted",
                guess_target,
                f"Submit **{guess_target}** valid Wordle guess(es)",
                2 if guess_target <= 3 else 3,
            ),
            _objective(
                "game:anagram:solves",
                solve_target,
                f"Solve **{solve_target}** anagram puzzle(s)",
                2 if solve_target <= 1 else 4,
            ),
        ],
    }


def _community_circuit(rng: random.Random, prestige_level: int) -> dict[str, object]:
    ticket_target = 10 if rng.random() < 0.5 else 15
    return {
        "key": "community_circuit",
        "title": "Community Circuit",
        "flavor": "Spread a little goodwill, feed the lotto pool, and don’t forget to touch the wheel.",
        "objectives": [
            _objective("game:thanks:sent", 1, "Send **1** `!thanks`", 2),
            _objective(
                "game:lotto:tickets_bought",
                ticket_target,
                f"Buy **{ticket_target}** lotto ticket(s)",
                2 if ticket_target <= 10 else 3,
            ),
            _objective("game:spin:spins", 1, "Spin the wheel **1** time", 2),
        ],
    }


def _house_circuit(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "house_circuit",
        "title": "House Circuit",
        "flavor": "The casino floor wants a full lap: one risky shot, one card round, and one wheel spin for luck.",
        "objectives": [
            _objective("game:roulette:plays", 1, "Use roulette **1** time", 3),
            _objective("game:blackjack:rounds_played", 1, "Play **1** blackjack round", 3),
            _objective("game:spin:spins", 1, "Spin the wheel **1** time", 2),
        ],
    }


def _double_or_nothing(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "double_or_nothing",
        "title": "Double Or Nothing",
        "flavor": "The contract issuer wants nerve, not caution. Sit down for blackjack and commit to the double.",
        "objectives": [
            _objective("game:blackjack:rounds_played", 1, "Play **1** blackjack round", 3),
            _objective("game:blackjack:doubles", 1, "Double down **1** time", 3),
        ],
    }


def _sharpshooter(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "sharpshooter",
        "title": "Sharpshooter",
        "flavor": "Anyone can pull the trigger. This one pays only if you actually land the shot.",
        "objectives": [
            _objective("game:roulette:successes", 1, "Land **1** successful roulette hit", 6)
        ],
    }


def _winner_take_all(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "winner_take_all",
        "title": "Winner Take All",
        "flavor": "Post a coinflip, take the risk, and make sure you walk away with the win.",
        "objectives": [
            _objective("game:coinflip:matches_played", 1, "Complete **1** coinflip match", 2),
            _objective("game:coinflip:wins", 1, "Win **1** coinflip match", 5),
        ],
    }


def _lucky_break(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "lucky_break",
        "title": "Lucky Break",
        "flavor": "Snag a surprise drop while the server sleeps, then cash a wheel spin before the luck fades.",
        "objectives": [
            _objective("game:surprise:claims", 1, "Claim **1** surprise drop", 4),
            _objective("game:spin:spins", 1, "Spin the wheel **1** time", 2),
        ],
    }


def _brain_burn(rng: random.Random, prestige_level: int) -> dict[str, object]:
    start_target = 3 if rng.random() < 0.6 else 4
    return {
        "key": "brain_burn",
        "title": "Brain Burn",
        "flavor": "A proper contract for overachievers: work both puzzle queues until your skull starts humming.",
        "objectives": [
            _objective("game:wordle:guesses_submitted", 4, "Submit **4** valid Wordle guess(es)", 3),
            _objective(
                "game:anagram:puzzles_started",
                start_target,
                f"Start **{start_target}** anagram puzzle(s)",
                2 if start_target <= 3 else 3,
            ),
            _objective("game:anagram:solves", 2, "Solve **2** anagram puzzle(s)", 4),
        ],
    }


CONTRACT_BUILDERS: list[tuple[int, Callable[[random.Random, int], dict[str, object]]]] = [
    (12, _archive_sweep),
    (10, _anagram_sprint),
    (9, _wheel_and_numbers),
    (8, _open_table),
    (8, _loaded_chamber),
    (8, _call_it),
    (6, _open_challenge),
    (7, _puzzle_circuit),
    (5, _community_circuit),
    (4, _house_circuit),
    (4, _double_or_nothing),
    (3, _sharpshooter),
    (3, _winner_take_all),
    (2, _lucky_break),
    (2, _brain_burn),
]


def _generate_contract(gid: int, uid: int, date_key: str) -> dict[str, object]:
    prestige_level = _user_prestige(gid, uid)
    rng = _rng_for_contract(gid, uid, date_key)
    builders = [builder for _, builder in CONTRACT_BUILDERS]
    weights = [weight for weight, _ in CONTRACT_BUILDERS]
    picked = rng.choices(builders, weights=weights, k=1)[0]
    return _finalize_contract(picked(rng, prestige_level), prestige_level)


class ContractsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _ensure_daily_contract(self, guild: discord.Guild, member: discord.Member) -> dict:
        st = _contract_state(guild.id, member.id)
        today = _date_key(_today_local())
        quest = _as_dict(st.get("quest"))
        baselines = _as_dict(st.get("baselines"))

        if (
            str(st.get("date", "")) == today
            and int(quest.get("version", 0)) == CONTRACT_VERSION
            and bool(_as_list(quest.get("objectives")))
            and bool(baselines)
        ):
            return st

        quest = _generate_contract(guild.id, member.id, today)
        metrics = {
            str(_as_dict(obj).get("metric", "")).strip()
            for obj in _as_list(quest.get("objectives"))
            if str(_as_dict(obj).get("metric", "")).strip()
        }
        baselines = {metric: _metric_value(guild.id, member.id, metric) for metric in metrics}

        st["date"] = today
        st["claimed"] = False
        st["assigned_at"] = _iso(_utcnow())
        st["completed_at"] = ""
        st["quest"] = quest
        st["baselines"] = baselines
        st["last_reward"] = {}

        record_game_fields(guild.id, member.id, "contracts", assigned=1)
        await save_data()
        return st

    async def _claim_contract_reward(
        self,
        guild: discord.Guild,
        member: discord.Member,
        st: dict,
        progress_rows: list[dict[str, object]],
    ) -> dict:
        quest = _as_dict(st.get("quest"))
        reward_bonus_xp = max(1, int(quest.get("reward_bonus_xp", 1)))
        reward = await grant_bonus_xp_equivalent_boost(
            member,
            reward_bonus_xp,
            source="contract complete",
            reward_seed_xp=reward_bonus_xp,
        )

        step_count = len(progress_rows)
        tier = str(quest.get("tier", "")).strip()
        st["claimed"] = True
        st["completed_at"] = _iso(_utcnow())
        st["last_reward"] = {
            "percent": float(reward.get("percent", 0.0)),
            "minutes": int(reward.get("minutes", 0)),
            "equivalent_bonus_xp": float(reward.get("equivalent_bonus_xp", reward_bonus_xp)),
        }

        record_game_fields(
            guild.id,
            member.id,
            "contracts",
            completed=1,
            multi_step_completed=1 if step_count > 1 else 0,
            legendary_completed=1 if tier == "Legendary" else 0,
            objectives_completed_total=step_count,
            boost_seed_xp_total=reward_bonus_xp,
            boost_percent_total=reward["percent"],
            boost_minutes_total=reward["minutes"],
            reward_minutes_equivalent_total=int(quest.get("reward_minutes_equivalent", 0)),
        )
        await save_data()
        return reward

    def _status_line(
        self,
        ctx: commands.Context,
        target: discord.Member,
        st: dict,
        progress_rows: list[dict[str, object]],
    ) -> str:
        claimed = bool(st.get("claimed", False))
        complete = _is_complete(progress_rows)
        done_count = sum(1 for row in progress_rows if bool(row.get("done", False)))
        total = len(progress_rows)

        if claimed:
            return "Status: **Completed and claimed today.**"
        if complete and target.id == ctx.author.id:
            return "Status: **Completed. Reward paid out now.**"
        if complete:
            return f"Status: **Complete.** Reward is ready when **{target.display_name}** uses `{ctx.clean_prefix}quest`."
        return f"Status: **In progress** (**{done_count}/{total}** objective(s) done)."

    def _reward_line(self, st: dict) -> str:
        quest = _as_dict(st.get("quest"))
        claimed = bool(st.get("claimed", False))
        reward_bonus_xp = max(1, int(quest.get("reward_bonus_xp", 1)))
        reward_minutes = max(1, int(quest.get("reward_minutes_equivalent", 1)))

        if not claimed:
            return (
                f"Reward: contract completion boost worth about **{_fmt_num(reward_bonus_xp)} XP** "
                f"at the holder's passive rate (~**{_fmt_duration_minutes(reward_minutes)}** of passive gain)."
            )

        last_reward = _as_dict(st.get("last_reward"))
        percent = _as_float(last_reward.get("percent", 0.0))
        minutes = _as_int(last_reward.get("minutes", 0))
        if percent <= 0.0 or minutes <= 0:
            return (
                f"Reward claimed: worth about **{_fmt_num(reward_bonus_xp)} XP** "
                f"(~**{_fmt_duration_minutes(reward_minutes)}** of passive gain)."
            )
        return (
            f"Reward claimed: **+{percent:.1f}% XP/min** for **{minutes}m** "
            f"(worth about **{_fmt_num(last_reward.get('equivalent_bonus_xp', reward_bonus_xp))} XP**)."
        )

    def _objective_lines(self, progress_rows: list[dict[str, object]]) -> list[str]:
        lines: list[str] = []
        for row in progress_rows:
            target = max(1, int(row.get("target", 1)))
            current = min(float(target), max(0.0, float(row.get("current", 0.0))))
            current_label = str(int(round(current))) if abs(current - round(current)) < 1e-9 else _fmt_num(current)
            prefix = "[x]" if bool(row.get("done", False)) else "[ ]"
            lines.append(f"- {prefix} {row.get('label', 'Objective')} (**{current_label}/{target}**)")
        return lines

    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context):
        if ctx.guild is None or ctx.author.bot or not CONTRACT_AUTO_ASSIGN_ON_COMMAND:
            return
        await self._ensure_daily_contract(ctx.guild, ctx.author)

    @commands.command(name="quest", aliases=["q"])
    async def quest(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        target = target or ctx.author
        if target.bot:
            await ctx.reply("Bots do not take contracts.")
            return

        st = await self._ensure_daily_contract(ctx.guild, target)
        progress_rows = _progress_rows(ctx.guild.id, target.id, _as_dict(st.get("quest")), _as_dict(st.get("baselines")))

        claimed_now = False
        if target.id == ctx.author.id and _is_complete(progress_rows) and not bool(st.get("claimed", False)):
            await self._claim_contract_reward(ctx.guild, target, st, progress_rows)
            claimed_now = True

        # Re-read current state after a possible payout so the display stays honest.
        st = _contract_state(ctx.guild.id, target.id)
        quest = _as_dict(st.get("quest"))
        progress_rows = _progress_rows(ctx.guild.id, target.id, quest, _as_dict(st.get("baselines")))

        lines = [
            f"**Daily Contract for {target.display_name}**",
            f"**{quest.get('title', 'Unknown Contract')}** - **{quest.get('tier', 'Standard')}**",
            str(quest.get("flavor", "")).strip(),
            self._status_line(ctx, target, st, progress_rows),
            self._reward_line(st),
        ]

        if claimed_now:
            lines.append("Completion payout has been delivered automatically.")

        lines.append("Objectives:")
        lines.extend(self._objective_lines(progress_rows))
        await ctx.reply("\n".join(lines))
