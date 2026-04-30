from __future__ import annotations

import asyncio
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
from .xp import grant_bonus_xp_equivalent_boost, grant_fixed_boost, prestige_passive_rate

CONTRACT_VERSION = 3
CONTRACT_FAST_CLEAR_WINDOW_SECONDS = 60 * 60
CONTRACT_FAST_CLEAR_BONUS_PCT = 0.10
CONTRACT_FAST_CLEAR_BONUS_MINUTES = 60
CONTRACT_MIN_OBJECTIVES = 2
CONTRACT_MAX_OBJECTIVES = 5


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


def _fmt_duration_seconds(seconds: int) -> str:
    secs = max(0, int(seconds))
    rounded_minutes = max(1, (secs + 59) // 60)
    return _fmt_duration_minutes(rounded_minutes)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_iso(value) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _contract_state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("contracts")
    if not isinstance(st, dict):
        st = {}
        u["contracts"] = st
    st.setdefault("date", "")
    st.setdefault("claimed", False)
    st.setdefault("assigned_at", "")
    st.setdefault("seen_at", "")
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


def _wordle_solve_within_objective(max_attempts: int) -> dict[str, object]:
    threshold = max(1, int(max_attempts))
    return _objective(
        f"game:wordle:wins_within_{threshold}",
        1,
        f"Solve Wordle in **{threshold}** guess(es) or fewer",
        4 if threshold <= 3 else 3,
    )


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


def _is_current_contract(st: dict) -> bool:
    today = _date_key(_today_local())
    quest = _as_dict(st.get("quest"))
    return (
        str(st.get("date", "")) == today
        and int(quest.get("version", 0)) == CONTRACT_VERSION
        and bool(_as_list(quest.get("objectives")))
        and bool(_as_dict(st.get("baselines")))
    )


def _fast_clear_applies(st: dict, *, now: Optional[datetime] = None) -> bool:
    seen_at = _parse_iso(st.get("seen_at"))
    if seen_at is None:
        return False
    current = now or _utcnow()
    elapsed = (current - seen_at).total_seconds()
    return 0.0 <= elapsed <= float(CONTRACT_FAST_CLEAR_WINDOW_SECONDS)


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
        "flavor": "Shake the dust off the dictionary and crack Wordle cleanly before the dust settles again.",
        "objectives": [_wordle_solve_within_objective(target)],
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


def _chamber_choir(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "chamber_choir",
        "title": "Chamber Choir",
        "flavor": "One shot is a stunt. Three shots is a performance. Keep the cylinder singing until everyone hears it.",
        "objectives": [
            _objective("game:roulette:plays", 3, "Use roulette **3** time(s)", 5)
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
            _wordle_solve_within_objective(guess_target),
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


def _wheel_addict(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "wheel_addict",
        "title": "Wheel Addict",
        "flavor": "One spin was never going to be enough. Come back until the wheel starts to feel like a habit.",
        "objectives": [
            _objective("game:spin:spins", 3, "Spin the wheel **3** time(s)", 5)
        ],
    }


def _crowd_favorite(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "crowd_favorite",
        "title": "Crowd Favorite",
        "flavor": "The board wants proof the room likes you. Not one pity nod. Not two. Three real thank-yous.",
        "objectives": [
            _objective("game:thanks:received", 3, "Receive **3** `!thanks`", 7)
        ],
    }


def _pay_it_forward(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "pay_it_forward",
        "title": "Pay It Forward",
        "flavor": "Tiny contract, clean conscience. Make one person's day slightly better and move on.",
        "objectives": [
            _objective("game:thanks:sent", 1, "Send **1** `!thanks`", 2)
        ],
    }


def _good_company(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "good_company",
        "title": "Good Company",
        "flavor": "Be generous once, then stick around long enough for the room to send a little warmth back your way.",
        "objectives": [
            _objective("game:thanks:sent", 1, "Send **1** `!thanks`", 2),
            _objective("game:thanks:received", 2, "Receive **2** `!thanks`", 4),
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


def _split_decision(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "split_decision",
        "title": "Split Decision",
        "flavor": "A clean contract for degenerate tacticians: spot the pair, split the hand, and let chaos multiply.",
        "objectives": [
            _objective("game:blackjack:splits", 1, "Split **1** blackjack hand", 5)
        ],
    }


def _natural_talent(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "natural_talent",
        "title": "Natural Talent",
        "flavor": "No grind, no gimmicks. Sit down, let the cards speak, and catch lightning in two cards.",
        "objectives": [
            _objective("game:blackjack:naturals", 1, "Hit **1** natural blackjack", 6)
        ],
    }


def _high_roller_blackjack(rng: random.Random, prestige_level: int) -> dict[str, object]:
    target = rng.choice([300, 500, 750]) + min(500, (max(0, prestige_level) // 10) * 50)
    return {
        "key": "high_roller_blackjack",
        "title": "High Roller: Blackjack",
        "flavor": "The pit boss wants to hear chips moving. Keep feeding the table until the felt remembers your name.",
        "objectives": [
            _objective(
                "game:blackjack:xp_wagered_total",
                target,
                f"Wager **{target} XP** total in blackjack",
                4 if target <= 500 else 5,
            )
        ],
    }


def _high_roller_coinflip(rng: random.Random, prestige_level: int) -> dict[str, object]:
    target = rng.choice([200, 350, 500]) + min(400, (max(0, prestige_level) // 10) * 50)
    return {
        "key": "high_roller_coinflip",
        "title": "High Roller: Coinflip",
        "flavor": "Find enough believers to keep the bets flowing. The board is only impressed when the room feels the risk.",
        "objectives": [
            _objective(
                "game:coinflip:xp_wagered_total",
                target,
                f"Wager **{target} XP** total in coinflip matches",
                4 if target <= 350 else 5,
            )
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


def _friendly_fire(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "friendly_fire",
        "title": "Friendly Fire",
        "flavor": "Some contracts are issued by comedians. Eat exactly one roulette timeout, whether it comes from bad luck or bad company.",
        "objectives": [
            _objective("game:roulette:got_timed_out", 1, "Get timed out by roulette **1** time", 5)
        ],
    }


def _backfire_ballet(rng: random.Random, prestige_level: int) -> dict[str, object]:
    roll = rng.random()
    if roll < 0.35:
        target = 1
    elif roll < 0.60:
        target = 2
    elif roll < 0.80:
        target = 3
    elif roll < 0.93:
        target = 4
    else:
        target = 5
    return {
        "key": "backfire_ballet",
        "title": "Backfire Ballet",
        "flavor": "A graceful disaster, repeated on purpose. Keep eating roulette backfires until the contract board is satisfied.",
        "objectives": [
            _objective(
                "game:roulette:backfires",
                target,
                f"Backfire on roulette **{target}** time(s)",
                5 if target <= 2 else (6 if target <= 4 else 7),
            )
        ],
    }


def _bad_influence(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "bad_influence",
        "title": "Bad Influence",
        "flavor": "The contract wants a full character arc: blow yourself up on roulette, shake it off, then land one clean shot later that same day.",
        "objectives": [
            _objective(
                "game:roulette:successes_after_backfire",
                1,
                "Backfire on roulette, then land **1** successful shot later that day",
                7,
            )
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


def _lucky_circuit(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "lucky_circuit",
        "title": "Lucky Circuit",
        "flavor": "Catch a falling prize, feed it into the machine, and let the rest ride straight into the lottery pool.",
        "objectives": [
            _objective("game:surprise:claims", 1, "Claim **1** surprise drop", 3),
            _objective("game:spin:spins", 1, "Spin the wheel **1** time", 2),
            _objective("game:lotto:tickets_bought", 10, "Buy **10** lotto ticket(s)", 3),
        ],
    }


def _scramble_marathon(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "scramble_marathon",
        "title": "Scramble Marathon",
        "flavor": "This one is for people who enjoy making their brain overheat. Keep the anagram queue rolling until the day starts to blur.",
        "objectives": [
            _objective("game:anagram:puzzles_started", 10, "Start **10** anagram puzzle(s)", 3),
            _objective("game:anagram:solves", 5, "Solve **5** anagram puzzle(s)", 5),
        ],
    }


def _letter_grinder(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "letter_grinder",
        "title": "Letter Grinder",
        "flavor": "Start with a clean Wordle solve, then keep chewing through enough scrambled letters that the alphabet begs for mercy.",
        "objectives": [
            _objective("game:wordle:wins", 1, "Solve Wordle **1** time", 3),
            _objective("game:anagram:solves", 3, "Solve **3** anagram puzzle(s)", 4),
        ],
    }


def _word_surgeon(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "word_surgeon",
        "title": "Word Surgeon",
        "flavor": "No rummaging through the alphabet. Walk in, cut straight to the answer, and leave the grid in stitches.",
        "objectives": [
            _objective(
                "game:wordle:wins_within_2",
                1,
                "Solve Wordle in **2** guess(es) or fewer",
                7,
            )
        ],
    }


def _practiced_sniper(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "practiced_sniper",
        "title": "Practiced Sniper",
        "flavor": "Raw talent is cute. The contract wants discipline: line up the wheel assist and actually convert the shot.",
        "objectives": [
            _objective(
                "game:roulette:successes_with_aim_bonus",
                1,
                "Land **1** roulette hit while using a wheel aim bonus",
                7,
            )
        ],
    }


def _jackpot_hunter(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "jackpot_hunter",
        "title": "Jackpot Hunter",
        "flavor": "A ridiculous ask from a ridiculous patron: buy in, wait out the draw, and walk away with the whole damn surge.",
        "objectives": [
            _objective("game:lotto:jackpots_won", 1, "Win **1** lotto jackpot", 10)
        ],
    }


def _call_of_duty(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "call_of_duty",
        "title": "Call of Duty",
        "flavor": "Clock an hour in voice and prove you can commit to the bit longer than a loading screen.",
        "objectives": [
            _objective("game:voice:minutes_in_call", 60, "Spend **60** minute(s) in voice", 4)
        ],
    }


def _after_hours(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "after_hours",
        "title": "After Hours",
        "flavor": "This contract pays for real loitering. Keep the call alive long enough that it starts to feel like a second shift.",
        "objectives": [
            _objective("game:voice:minutes_in_call", 120, "Spend **120** minute(s) in voice", 6)
        ],
    }


def _wingman(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "wingman",
        "title": "Wingman",
        "flavor": "Pick a partner, stay in the pocket, and rack up enough shared call time that the board starts assuming you came as a set.",
        "objectives": [
            _objective(
                "game:voice:wingman_targets",
                1,
                "Share a voice call with the same user for **45** minute(s) total",
                6,
            )
        ],
    }


def _party_bus(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "party_bus",
        "title": "Party Bus",
        "flavor": "A lonely VC does not count. This one wants a proper crowd and enough time for the chaos to settle into a vibe.",
        "objectives": [
            _objective(
                "game:voice:party_bus_minutes",
                30,
                "Spend **30** minute(s) in voice with **3+** people total",
                6,
            )
        ],
    }


def _mic_check(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "mic_check",
        "title": "Mic Check",
        "flavor": "The contract issuer wants a public service announcement. Queue a `!say` and make the room hear it.",
        "objectives": [
            _objective("game:tts:say_commands", 1, "Use `!say` **1** time", 4)
        ],
    }


def _ping_pong(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "ping_pong",
        "title": "Ping Pong",
        "flavor": "Lock onto one unfortunate soul and keep the pings coming until the contract board decides the joke has landed.",
        "objectives": [
            _objective(
                "game:social:ping_pong_targets",
                1,
                "Mention the same user in **10** messages",
                6,
            )
        ],
    }


def _neighborhood_watch(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "neighborhood_watch",
        "title": "Neighborhood Watch",
        "flavor": "Make the rounds, check in on the whole block, and make sure at least five people know you were here.",
        "objectives": [
            _objective(
                "game:social:neighborhood_watch_days",
                1,
                "Mention **5** different users in one day",
                5,
            )
        ],
    }


def _essayist(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "essayist",
        "title": "Essayist",
        "flavor": "The board wants volume. Keep typing until your presence in chat becomes statistically impossible to ignore.",
        "objectives": [
            _objective("game:social:messages_sent", 20, "Send **20** non-command messages", 4)
        ],
    }


def _dj_set(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "dj_set",
        "title": "DJ Set",
        "flavor": "Take over the aux like you mean it. Queue enough tracks that nobody can pretend it was an accident.",
        "objectives": [
            _objective("game:playback:tracks_queued", 3, "Queue **3** track(s) with `!play`", 4)
        ],
    }


def _regular_customer(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "regular_customer",
        "title": "Regular Customer",
        "flavor": "Tour the menu. The contract only pays if you use enough different commands to look like a repeat customer.",
        "objectives": [
            _objective(
                "game:social:regular_customer_days",
                1,
                "Use **5** different bot commands in one day",
                5,
            )
        ],
    }


def _shopaholic(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "shopaholic",
        "title": "Shopaholic",
        "flavor": "A quick sweep through the store is not enough. Buy a few things and let the receipt speak for itself.",
        "objectives": [
            _objective("game:shop:purchases", 3, "Buy **3** shop item(s)", 4)
        ],
    }


def _touch_grass(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "touch_grass",
        "title": "Touch Grass",
        "flavor": "View the contract, vanish for a while, and only get paid once you actually come back like a reformed person.",
        "objectives": [
            _objective(
                "game:social:touch_grass_returns",
                1,
                "After checking this contract, be away for **30+** minutes and then come back",
                5,
            )
        ],
    }


def _full_circuit(rng: random.Random, prestige_level: int) -> dict[str, object]:
    return {
        "key": "full_circuit",
        "title": "Full Circuit",
        "flavor": "Make it social, make it technical, make it audible. The board wants proof you touched every part of the server in one run.",
        "objectives": [
            _objective("game:social:mention_messages", 1, "Mention **1** user in chat", 2),
            _objective("game:voice:minutes_in_call", 30, "Spend **30** minute(s) in voice", 4),
            _objective(
                "game:social:non_quest_commands_used",
                1,
                "Use **1** bot command that is not `!q`",
                2,
            ),
        ],
    }


def _brain_burn(rng: random.Random, prestige_level: int) -> dict[str, object]:
    start_target = 3 if rng.random() < 0.6 else 4
    return {
        "key": "brain_burn",
        "title": "Brain Burn",
        "flavor": "A proper contract for overachievers: work both puzzle queues until your skull starts humming.",
        "objectives": [
            _wordle_solve_within_objective(4),
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
    (3, _chamber_choir),
    (8, _call_it),
    (6, _open_challenge),
    (7, _puzzle_circuit),
    (5, _community_circuit),
    (2, _wheel_addict),
    (1, _crowd_favorite),
    (4, _pay_it_forward),
    (2, _good_company),
    (4, _house_circuit),
    (4, _double_or_nothing),
    (3, _split_decision),
    (2, _natural_talent),
    (3, _high_roller_blackjack),
    (3, _high_roller_coinflip),
    (3, _sharpshooter),
    (2, _friendly_fire),
    (2, _backfire_ballet),
    (1, _bad_influence),
    (3, _winner_take_all),
    (2, _lucky_break),
    (2, _lucky_circuit),
    (1, _scramble_marathon),
    (2, _letter_grinder),
    (1, _word_surgeon),
    (1, _practiced_sniper),
    (1, _jackpot_hunter),
    (2, _call_of_duty),
    (1, _after_hours),
    (1, _wingman),
    (1, _party_bus),
    (2, _mic_check),
    (1, _ping_pong),
    (2, _neighborhood_watch),
    (2, _essayist),
    (2, _dj_set),
    (2, _regular_customer),
    (2, _shopaholic),
    (1, _touch_grass),
    (1, _full_circuit),
    (2, _brain_burn),
]


def _objective_metrics(objectives: list[dict[str, object]]) -> set[str]:
    metrics: set[str] = set()
    for obj in objectives:
        metric = str(_as_dict(obj).get("metric", "")).strip()
        if metric:
            metrics.add(metric)
    return metrics


def _contract_objective_target(rng: random.Random) -> int:
    return int(rng.choices([2, 3, 4, 5], weights=[35, 40, 18, 7], k=1)[0])


def _compose_contract_bundle(contracts: list[dict[str, object]]) -> dict[str, object]:
    picked = [dict(contract) for contract in contracts if isinstance(contract, dict)]
    if not picked:
        return {
            "key": "contract_bundle",
            "title": "Contract Bundle",
            "flavor": "Clear every listed objective for one payout.",
            "objectives": [],
        }

    if len(picked) == 1:
        return picked[0]

    titles = [str(contract.get("title", "")).strip() for contract in picked if str(contract.get("title", "")).strip()]
    keys = [str(contract.get("key", "")).strip() for contract in picked if str(contract.get("key", "")).strip()]
    objectives = [
        _as_dict(obj)
        for contract in picked
        for obj in _as_list(contract.get("objectives"))
        if _as_dict(obj)
    ]

    if len(titles) <= 2:
        title = " + ".join(titles)
    else:
        title = f"{titles[0]} + {len(titles) - 1} More"

    bundle_count = len(picked)
    return {
        "key": "+".join(keys) or "contract_bundle",
        "title": title or "Contract Bundle",
        "flavor": (
            f"The board stacked **{bundle_count}** assignments today. "
            "Clear every listed objective for one payout."
        ),
        "objectives": objectives,
        "bundle_count": bundle_count,
    }


def _generate_contract(gid: int, uid: int, date_key: str) -> dict[str, object]:
    prestige_level = _user_prestige(gid, uid)
    rng = _rng_for_contract(gid, uid, date_key)
    available = [
        {"weight": int(weight), "builder": builder}
        for weight, builder in CONTRACT_BUILDERS
    ]
    if not available:
        return _finalize_contract(_compose_contract_bundle([]), prestige_level)

    first_idx = rng.choices(
        range(len(available)),
        weights=[row["weight"] for row in available],
        k=1,
    )[0]
    first = available.pop(first_idx)
    selected = [first["builder"](rng, prestige_level)]
    objectives = [_as_dict(obj) for obj in _as_list(selected[0].get("objectives")) if _as_dict(obj)]
    objective_total = len(objectives)
    used_metrics = _objective_metrics(objectives)
    desired_total = max(CONTRACT_MIN_OBJECTIVES, objective_total, _contract_objective_target(rng))

    while objective_total < desired_total and available:
        next_idx = rng.choices(
            range(len(available)),
            weights=[row["weight"] for row in available],
            k=1,
        )[0]
        row = available.pop(next_idx)
        candidate = row["builder"](rng, prestige_level)
        candidate_objectives = [
            _as_dict(obj) for obj in _as_list(candidate.get("objectives")) if _as_dict(obj)
        ]
        if not candidate_objectives:
            continue
        if objective_total + len(candidate_objectives) > desired_total:
            continue
        candidate_metrics = _objective_metrics(candidate_objectives)
        if used_metrics & candidate_metrics:
            continue
        selected.append(candidate)
        objective_total += len(candidate_objectives)
        used_metrics.update(candidate_metrics)

    while objective_total < CONTRACT_MIN_OBJECTIVES and available:
        next_idx = rng.choices(
            range(len(available)),
            weights=[row["weight"] for row in available],
            k=1,
        )[0]
        row = available.pop(next_idx)
        candidate = row["builder"](rng, prestige_level)
        candidate_objectives = [
            _as_dict(obj) for obj in _as_list(candidate.get("objectives")) if _as_dict(obj)
        ]
        if not candidate_objectives:
            continue
        if objective_total + len(candidate_objectives) > CONTRACT_MAX_OBJECTIVES:
            continue
        selected.append(candidate)
        objective_total += len(candidate_objectives)

    return _finalize_contract(_compose_contract_bundle(selected), prestige_level)


class ContractsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._claim_locks: dict[tuple[int, int], asyncio.Lock] = {}

    def _claim_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(user_id))
        lock = self._claim_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._claim_locks[key] = lock
        return lock

    def _claimed_reward_payload(self, st: dict) -> dict[str, object]:
        quest = _as_dict(st.get("quest"))
        last_reward = _as_dict(st.get("last_reward"))
        equivalent_bonus_xp = _as_float(last_reward.get("equivalent_bonus_xp", quest.get("reward_bonus_xp", 1)))
        fast_clear_percent = _as_float(last_reward.get("fast_clear_percent", 0.0))
        fast_clear_minutes = _as_int(last_reward.get("fast_clear_minutes", 0))
        fast_clear_equivalent_bonus_xp = _as_float(last_reward.get("fast_clear_equivalent_bonus_xp", 0.0))
        fast_clear_reward: dict[str, object] = {}
        if fast_clear_percent > 0.0 and fast_clear_minutes > 0:
            fast_clear_reward = {
                "percent": float(fast_clear_percent),
                "minutes": int(fast_clear_minutes),
            }
        return {
            "percent": _as_float(last_reward.get("percent", 0.0)),
            "minutes": _as_int(last_reward.get("minutes", 0)),
            "equivalent_bonus_xp": equivalent_bonus_xp,
            "fast_clear_reward": fast_clear_reward,
            "fast_clear_equivalent_bonus_xp": fast_clear_equivalent_bonus_xp,
            "total_equivalent_bonus_xp": equivalent_bonus_xp + fast_clear_equivalent_bonus_xp,
            "already_claimed": True,
        }

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
        st["seen_at"] = ""
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
        async with self._claim_lock(guild.id, member.id):
            live_state = _contract_state(guild.id, member.id)
            if bool(live_state.get("claimed", False)):
                return self._claimed_reward_payload(live_state)

            quest = _as_dict(live_state.get("quest"))
            reward_bonus_xp = max(1, int(quest.get("reward_bonus_xp", 1)))
            reward = await grant_bonus_xp_equivalent_boost(
                member,
                reward_bonus_xp,
                source="contract complete",
                reward_seed_xp=reward_bonus_xp,
            )
            fast_clear_reward: dict[str, object] | None = None
            fast_clear_equivalent_bonus_xp = 0.0
            if _fast_clear_applies(live_state):
                fast_clear_equivalent_bonus_xp = (
                    max(0.01, float(reward.get("rate_basis_per_min", 0.0)))
                    * float(CONTRACT_FAST_CLEAR_BONUS_PCT)
                    * float(CONTRACT_FAST_CLEAR_BONUS_MINUTES)
                )
                fast_clear_reward = await grant_fixed_boost(
                    member,
                    pct=CONTRACT_FAST_CLEAR_BONUS_PCT,
                    minutes=CONTRACT_FAST_CLEAR_BONUS_MINUTES,
                    source="contract complete",
                    reward_seed_xp=fast_clear_equivalent_bonus_xp,
                )

            step_count = len(progress_rows)
            tier = str(quest.get("tier", "")).strip()
            total_equivalent_bonus_xp = float(reward.get("equivalent_bonus_xp", reward_bonus_xp))
            total_equivalent_bonus_xp += float(fast_clear_equivalent_bonus_xp)
            total_percent = float(reward.get("percent", 0.0))
            total_minutes = int(reward.get("minutes", 0))
            if fast_clear_reward is not None:
                total_percent += float(fast_clear_reward.get("percent", 0.0))
                total_minutes += int(fast_clear_reward.get("minutes", 0))
            live_state["claimed"] = True
            live_state["completed_at"] = _iso(_utcnow())
            live_state["last_reward"] = {
                "percent": float(reward.get("percent", 0.0)),
                "minutes": int(reward.get("minutes", 0)),
                "equivalent_bonus_xp": float(reward.get("equivalent_bonus_xp", reward_bonus_xp)),
                "fast_clear_percent": (
                    float(fast_clear_reward.get("percent", 0.0)) if fast_clear_reward is not None else 0.0
                ),
                "fast_clear_minutes": (
                    int(fast_clear_reward.get("minutes", 0)) if fast_clear_reward is not None else 0
                ),
                "fast_clear_equivalent_bonus_xp": float(fast_clear_equivalent_bonus_xp),
            }

            record_game_fields(
                guild.id,
                member.id,
                "contracts",
                completed=1,
                multi_step_completed=1 if step_count > 1 else 0,
                legendary_completed=1 if tier == "Legendary" else 0,
                fast_clear_completed=1 if fast_clear_reward is not None else 0,
                objectives_completed_total=step_count,
                boost_seed_xp_total=total_equivalent_bonus_xp,
                boost_percent_total=total_percent,
                boost_minutes_total=total_minutes,
                reward_minutes_equivalent_total=int(quest.get("reward_minutes_equivalent", 0)),
                fast_clear_percent_total=(
                    float(fast_clear_reward.get("percent", 0.0)) if fast_clear_reward is not None else 0.0
                ),
                fast_clear_minutes_total=(
                    int(fast_clear_reward.get("minutes", 0)) if fast_clear_reward is not None else 0
                ),
            )
            await save_data()
            reward["fast_clear_reward"] = fast_clear_reward or {}
            reward["fast_clear_equivalent_bonus_xp"] = float(fast_clear_equivalent_bonus_xp)
            reward["total_equivalent_bonus_xp"] = float(total_equivalent_bonus_xp)
            reward["already_claimed"] = False
            return reward

    async def _send_contract_completion_message(self, channel, member: discord.Member, st: dict, reward: dict) -> None:
        if channel is None:
            return
        quest = _as_dict(st.get("quest"))
        fast_clear_reward = _as_dict(reward.get("fast_clear_reward"))
        lines = [
            (
                f"{member.mention} **Contract complete:** "
                f"**{quest.get('title', 'Unknown Contract')}** - **{quest.get('tier', 'Standard')}**."
            ),
            (
                f"Reward paid: **+{float(reward.get('percent', 0.0)):.1f}% XP/min** "
                f"for **{int(reward.get('minutes', 0))}m**"
            ),
        ]
        if fast_clear_reward:
            lines[-1] += (
                f" plus fast-clear bonus **+{float(fast_clear_reward.get('percent', 0.0)):.1f}% XP/min** "
                f"for **{int(fast_clear_reward.get('minutes', 0))}m**"
            )
        lines[-1] += f" (worth about **{_fmt_num(reward.get('total_equivalent_bonus_xp', 0.0))} XP**)."
        if fast_clear_reward:
            lines.append("Fast clear bonus applied for finishing within **1 hour** of first checking the contract.")
        try:
            await channel.send("\n".join(lines))
        except Exception:
            return

    async def maybe_auto_complete_contract_for_member(self, guild: discord.Guild, member: discord.Member, *, channel=None) -> bool:
        if guild is None or member.bot:
            return False
        st = _contract_state(guild.id, member.id)
        if bool(st.get("claimed", False)) or not _is_current_contract(st):
            return False
        quest = _as_dict(st.get("quest"))
        progress_rows = _progress_rows(guild.id, member.id, quest, _as_dict(st.get("baselines")))
        if not _is_complete(progress_rows):
            return False
        reward = await self._claim_contract_reward(guild, member, st, progress_rows)
        if bool(reward.get("already_claimed", False)):
            return False
        await self._send_contract_completion_message(channel, member, st, reward)
        return True

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
        if complete:
            return "Status: **Complete. Reward will be paid automatically.**"
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
        fast_clear_percent = _as_float(last_reward.get("fast_clear_percent", 0.0))
        fast_clear_minutes = _as_int(last_reward.get("fast_clear_minutes", 0))
        fast_clear_equivalent = _as_float(last_reward.get("fast_clear_equivalent_bonus_xp", 0.0))
        if fast_clear_percent > 0.0 and fast_clear_minutes > 0:
            total_equivalent = _as_float(last_reward.get("equivalent_bonus_xp", reward_bonus_xp)) + fast_clear_equivalent
            return (
                f"Reward claimed: **+{percent:.1f}% XP/min** for **{minutes}m** plus "
                f"fast-clear bonus **+{fast_clear_percent:.1f}% XP/min** for "
                f"**{fast_clear_minutes}m** (worth about **{_fmt_num(total_equivalent)} XP** total)."
            )
        return (
            f"Reward claimed: **+{percent:.1f}% XP/min** for **{minutes}m** "
            f"(worth about **{_fmt_num(last_reward.get('equivalent_bonus_xp', reward_bonus_xp))} XP**)."
        )

    def _fast_clear_line(self, st: dict, *, viewer_is_holder: bool) -> str:
        if not viewer_is_holder:
            return ""
        if bool(st.get("claimed", False)):
            last_reward = _as_dict(st.get("last_reward"))
            fast_clear_percent = _as_float(last_reward.get("fast_clear_percent", 0.0))
            fast_clear_minutes = _as_int(last_reward.get("fast_clear_minutes", 0))
            if fast_clear_percent > 0.0 and fast_clear_minutes > 0:
                return (
                    f"Fast clear: earned **+{fast_clear_percent:.1f}% XP/min** and "
                    f"**{fast_clear_minutes}m** for finishing within **1h** of first check."
                )
            return ""

        seen_at = _parse_iso(st.get("seen_at"))
        if seen_at is None:
            return ""
        remaining_seconds = int(
            float(CONTRACT_FAST_CLEAR_WINDOW_SECONDS)
            - max(0.0, (_utcnow() - seen_at).total_seconds())
        )
        if remaining_seconds > 0:
            return (
                f"Fast clear bonus active: finish within **{_fmt_duration_seconds(remaining_seconds)}** "
                f"for an extra **+{CONTRACT_FAST_CLEAR_BONUS_PCT * 100.0:.1f}% XP/min** "
                f"and **+{CONTRACT_FAST_CLEAR_BONUS_MINUTES}m**."
            )
        return "Fast clear bonus expired."

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
        if target.id == ctx.author.id and not str(st.get("seen_at", "")).strip():
            st["seen_at"] = _iso(_utcnow())
            await save_data()

        # Re-read current state so the display reflects any auto-claim that may have happened elsewhere.
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
        fast_clear_line = self._fast_clear_line(st, viewer_is_holder=(target.id == ctx.author.id))
        if fast_clear_line:
            lines.append(fast_clear_line)

        lines.append("Objectives:")
        lines.extend(self._objective_lines(progress_rows))
        await ctx.reply("\n".join(lines))
