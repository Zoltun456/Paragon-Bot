from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import random
import re
from typing import Optional

import discord
from discord.ext import commands, tasks

from .config import (
    BOSS_ATTACK_COOLDOWN_SECONDS,
    BOSS_AVG_PRESTIGE_OFFSET,
    BOSS_DAMAGE_CRIT_BONUS,
    BOSS_DAMAGE_MAX,
    BOSS_DAMAGE_MIN,
    BOSS_DAMAGE_PRESTIGE_BONUS,
    BOSS_DAMAGE_PRESTIGE_STEP,
    BOSS_DURATION_MAX_MINUTES,
    BOSS_DURATION_MIN_MINUTES,
    BOSS_ENABLED,
    BOSS_FAILURE_DEBUFF_MINUTES,
    BOSS_FAILURE_DEBUFF_PCT,
    BOSS_HP_BASE,
    BOSS_HP_PER_BOSS_PRESTIGE,
    BOSS_HP_PER_TARGET_FIGHTER,
    BOSS_HEAL_MAX,
    BOSS_HEAL_MIN,
    BOSS_IDLE_MAX_HOURS,
    BOSS_RES_COOLDOWN_SECONDS,
    BOSS_RETALIATE_DEBUFF_MAX_MINUTES,
    BOSS_RETALIATE_DEBUFF_MAX_PCT,
    BOSS_RETALIATE_DEBUFF_MIN_MINUTES,
    BOSS_RETALIATE_DEBUFF_MIN_PCT,
    BOSS_RETALIATE_DOWN_CHANCE,
    BOSS_RETALIATE_TIMEOUT_MAX_SECONDS,
    BOSS_RETALIATE_TIMEOUT_MIN_SECONDS,
    BOSS_SPAWN_END_HOUR,
    BOSS_SPAWN_MAX_DAYS,
    BOSS_SPAWN_MIN_DAYS,
    BOSS_SPAWN_START_HOUR,
    BOSS_TARGET_MEMBER_DIVISOR,
    BOSS_VICTORY_BOOST_MINUTES,
    BOSS_VICTORY_BOOST_PCT,
    COMMAND_PREFIX,
    LOCAL_TZ,
)
from .emojis import EMOJI_CROSSED_SWORDS, EMOJI_HEART
from .guild_setup import ensure_guild_setup, get_log_channel
from .ownership import owner_only
from .stats_store import record_game_fields
from .storage import _gdict, _udict, save_data
from .xp import grant_fixed_boost, grant_fixed_debuff

BOSS_STATE_KEY = "boss"
BOSS_ATTACKER_LIMIT = 5
BOSS_CHANNEL_NAME = "active-boss"
BOSS_CONTROL_MESSAGE_LIMIT = 5
BOSS_REACTION_ATTACK = EMOJI_CROSSED_SWORDS
BOSS_REACTION_RES = EMOJI_HEART
BOSS_REACTION_ATTACK_NAMES = {
    BOSS_REACTION_ATTACK.replace("\ufe0f", ""),
    "crossed_swords",
}
BOSS_REACTION_RES_NAMES = {
    BOSS_REACTION_RES.replace("\ufe0f", ""),
    "heart",
}
BOSS_ACTIVE_DURATION_MINUTES = 60
BOSS_TARGET_CLEAR_MINUTES = max(15, BOSS_ACTIVE_DURATION_MINUTES // 4)
BOSS_ATTACK_COOLDOWN_ACTIVE_SECONDS = 75
BOSS_RES_COOLDOWN_ACTIVE_SECONDS = 45
BOSS_SUPPORT_COOLDOWN_SECONDS = 45
BOSS_SUPPORT_WINDOW_SECONDS = 90
BOSS_GUARD_DURATION_SECONDS = 45
BOSS_FOCUS_DURATION_SECONDS = 90
BOSS_FOCUS_DAMAGE_BONUS_PCT = 0.30
BOSS_FOCUS_HIT_BONUS_PCT = 0.15
BOSS_EXPOSE_DURATION_SECONDS = 35
BOSS_EXPOSE_DAMAGE_BONUS_PCT = 0.20
BOSS_STUN_DURATION_SECONDS = 25
BOSS_MARK_DURATION_SECONDS = 150
BOSS_MARK_HIT_PENALTY_PCT = 0.15
BOSS_MARK_DAMAGE_PENALTY_PCT = 0.20
BOSS_MECHANIC_INTERVAL_MIN_SECONDS = 65
BOSS_MECHANIC_INTERVAL_MAX_SECONDS = 100
BOSS_MECHANIC_WARNING_MIN_SECONDS = 20
BOSS_MECHANIC_WARNING_MAX_SECONDS = 30
BOSS_PHASE_TRIGGER_DELAY_SECONDS = 25
BOSS_PHASE_THRESHOLDS = (75, 50, 25)
BOSS_BASE_REWARD_PCT = min(0.08, max(0.03, float(BOSS_VICTORY_BOOST_PCT)))
BOSS_BASE_REWARD_MINUTES = min(240, max(90, int(BOSS_VICTORY_BOOST_MINUTES)))
BOSS_BONUS_REWARD_PCT = 0.02
BOSS_BONUS_REWARD_MINUTES = 90
BOSS_SURVIVOR_REWARD_PCT = 0.01
BOSS_SURVIVOR_REWARD_MINUTES = 60
BOSS_FAILURE_PENALTY_PCT = min(0.35, max(0.10, float(BOSS_FAILURE_DEBUFF_PCT)))
BOSS_FAILURE_PENALTY_MINUTES = min(120, max(30, int(BOSS_FAILURE_DEBUFF_MINUTES)))

NAME_PREFIXES = (
    "Ael",
    "Ar",
    "Bael",
    "Cal",
    "Dra",
    "Eld",
    "Fael",
    "Ghor",
    "Ith",
    "Kael",
    "Kor",
    "Luth",
    "Mor",
    "Nhal",
    "Or",
    "Rha",
    "Sael",
    "Thal",
    "Vael",
    "Vel",
    "Vor",
    "Xal",
    "Yor",
    "Zev",
)
NAME_MIDDLES = (
    "a",
    "ae",
    "an",
    "ar",
    "e",
    "el",
    "en",
    "eth",
    "ia",
    "ir",
    "or",
    "ul",
    "ur",
    "yr",
)
NAME_SUFFIXES = (
    "bane",
    "dris",
    "garde",
    "khar",
    "lith",
    "morn",
    "rahn",
    "rune",
    "thas",
    "vane",
    "vek",
    "vyr",
    "zhar",
    "zoren",
)
TITLE_ROLES = (
    "Blade",
    "Crown",
    "Harbinger",
    "Herald",
    "Keeper",
    "Marshal",
    "Saint",
    "Scourge",
    "Seer",
    "Tyrant",
    "Warden",
    "Watcher",
)
TITLE_PLACES = (
    "Ashen Choir",
    "Black Orchard",
    "Bleak Ember",
    "Broken Oaths",
    "Cinder Gate",
    "Drowned Bell",
    "Hollow Dawn",
    "Iron Eclipse",
    "Last Reliquary",
    "Pale Furnace",
    "Sable March",
    "Shattered Sun",
    "Thorned Mercy",
    "Withered Vale",
)
TITLE_ADJECTIVES = (
    "Ashen",
    "Black",
    "Bleak",
    "Cinder",
    "Fell",
    "Gloam",
    "Hollow",
    "Iron",
    "Pale",
    "Sable",
    "Shattered",
    "Withered",
)
TITLE_NOUNS = (
    "Apostle",
    "Bell",
    "Censer",
    "Judge",
    "King",
    "Monarch",
    "Prophet",
    "Revenant",
    "Sovereign",
    "Throne",
    "Usurper",
    "Vow",
)
RETALIATION_NAMES = {
    "ashen_claw": "Ashen Claw",
    "grave_brand": "Grave Brand",
    "iron_sentence": "Iron Sentence",
    "sundering_roar": "Sundering Roar",
    "black_tithe": "Black Tithe",
    "choir_of_ruin": "Choir Of Ruin",
    "hollow_judgment": "Hollow Judgment",
    "sable_chain": "Sable Chain",
    "grave_fall": "Gravefall",
    "void_glare": "Void Glare",
}
PHASE_NAMES = {
    1: "Opening",
    2: "Cracked Aegis",
    3: "Blooded Fury",
    4: "Last Stand",
}
AFFIXES = {
    "bulwarked": {
        "name": "Bulwarked",
        "desc": "Extra HP and guard-heavy mechanics, but clean interruptions crack its armor.",
        "hp_mult": 1.05,
        "mechanic_bias": "guard",
        "heal_mult": 1.10,
        "expose_bonus_pct": 0.05,
        "interval_mult": 1.00,
        "stun_bonus_seconds": 0,
        "focus_bonus_pct": 0.00,
        "mark_duration_mult": 1.00,
    },
    "frenzied": {
        "name": "Frenzied",
        "desc": "Lower HP, faster mechanics, and more cooldown pressure once it wakes up.",
        "hp_mult": 0.95,
        "mechanic_bias": "interrupt",
        "heal_mult": 0.90,
        "expose_bonus_pct": 0.02,
        "interval_mult": 0.85,
        "stun_bonus_seconds": 0,
        "focus_bonus_pct": 0.00,
        "mark_duration_mult": 1.00,
    },
    "venomous": {
        "name": "Venomous",
        "desc": "Blight marks linger longer, so timely cleanses matter a lot.",
        "hp_mult": 1.00,
        "mechanic_bias": "cleanse",
        "heal_mult": 1.00,
        "expose_bonus_pct": 0.00,
        "interval_mult": 0.95,
        "stun_bonus_seconds": 0,
        "focus_bonus_pct": 0.00,
        "mark_duration_mult": 1.30,
    },
    "siphoning": {
        "name": "Siphoning",
        "desc": "Failed mechanics heal it harder, but focused burst windows hit much harder.",
        "hp_mult": 1.02,
        "mechanic_bias": "cleanse",
        "heal_mult": 1.30,
        "expose_bonus_pct": 0.00,
        "interval_mult": 1.00,
        "stun_bonus_seconds": 0,
        "focus_bonus_pct": 0.10,
        "mark_duration_mult": 1.00,
    },
    "stormbound": {
        "name": "Stormbound",
        "desc": "Interrupt windows come faster, and successful counters stun it for longer.",
        "hp_mult": 0.97,
        "mechanic_bias": "interrupt",
        "heal_mult": 1.00,
        "expose_bonus_pct": 0.03,
        "interval_mult": 0.90,
        "stun_bonus_seconds": 10,
        "focus_bonus_pct": 0.00,
        "mark_duration_mult": 1.00,
    },
}
MECHANIC_DEFS = {
    "crushing_slam": {
        "name": "Crushing Slam",
        "counter": "guard",
        "verb": "brace",
        "warning": "Brace with `!guard` before the impact lands.",
    },
    "soul_scream": {
        "name": "Soul Scream",
        "counter": "interrupt",
        "verb": "interrupt",
        "warning": "Shut it down with `!interrupt` before the scream breaks over the raid.",
    },
    "blight_bloom": {
        "name": "Blight Bloom",
        "counter": "purge",
        "verb": "purge",
        "warning": "Purge the bloom with `!purge` before the rot spreads through the chamber.",
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_iso(value: object) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
    num = float(value)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.2f}"


def _fmt_duration_minutes(minutes: int) -> str:
    total = max(0, int(minutes))
    hours, mins = divmod(total, 60)
    if hours <= 0:
        return f"{mins}m"
    if mins <= 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


def _fmt_remaining(seconds: int) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes <= 0:
        return f"{secs}s"
    hours, mins = divmod(minutes, 60)
    if hours <= 0:
        return f"{mins}m {secs:02d}s"
    return f"{hours}h {mins:02d}m"


def _fmt_pct(value: int | float) -> str:
    return f"{float(value) * 100.0:.0f}%"


def _boss_history(st: dict) -> dict:
    hist = st.get("history")
    if not isinstance(hist, dict):
        hist = {}
        st["history"] = hist
    hist.setdefault("spawns", 0)
    hist.setdefault("kills", 0)
    hist.setdefault("failures", 0)
    hist.setdefault("fades", 0)
    hist.setdefault("mechanics_countered", 0)
    hist.setdefault("support_actions", 0)
    hist.setdefault("fastest_kill_seconds", 0)
    hist.setdefault("largest_hit", 0)
    hist.setdefault("largest_hit_by", "")
    hist.setdefault("largest_hit_boss", "")
    return hist


def _phase_for_ratio(hp: int, max_hp: int) -> int:
    denom = max(1, int(max_hp))
    ratio = max(0.0, min(1.0, float(hp) / float(denom)))
    if ratio <= 0.25:
        return 4
    if ratio <= 0.50:
        return 3
    if ratio <= 0.75:
        return 2
    return 1


def _phase_name(phase: int) -> str:
    return PHASE_NAMES.get(int(phase), "Unknown Phase")


def _affix_data(boss: dict) -> dict:
    key = str(boss.get("affix_key", "")).strip().lower()
    return AFFIXES.get(key, AFFIXES["bulwarked"])


def _attack_cooldown_seconds(boss: dict) -> int:
    return int(BOSS_ATTACK_COOLDOWN_ACTIVE_SECONDS)


def _res_cooldown_seconds(boss: dict) -> int:
    return int(BOSS_RES_COOLDOWN_ACTIVE_SECONDS)


def _support_cooldown_seconds(boss: dict) -> int:
    return int(BOSS_SUPPORT_COOLDOWN_SECONDS)


def _is_contributor_row(row: dict) -> bool:
    return (
        _as_int(row.get("attacks", 0), 0) > 0
        or _as_int(row.get("resurrections", 0), 0) > 0
        or _as_int(row.get("support_actions", 0), 0) > 0
    )


def _support_score(row: dict) -> int:
    return (
        (_as_int(row.get("resurrections", 0), 0) * 3)
        + (_as_int(row.get("guards", 0), 0) * 2)
        + (_as_int(row.get("interrupts", 0), 0) * 3)
        + (_as_int(row.get("cleanses", 0), 0) * 2)
        + _as_int(row.get("focuses", 0), 0)
        + (_as_int(row.get("mechanics_countered", 0), 0) * 2)
    )


def _phase_triggers(boss: dict) -> list[int]:
    rows = [int(v) for v in _as_list(boss.get("phase_triggers")) if _as_int(v, 0) > 0]
    boss["phase_triggers"] = rows
    return rows


def _pending_mechanic(boss: dict) -> dict:
    mech = _as_dict(boss.get("pending_mechanic"))
    mech.setdefault("responses", [])
    mech["responses"] = [int(uid) for uid in _as_list(mech.get("responses")) if _as_int(uid, 0) > 0]
    boss["pending_mechanic"] = mech
    return mech


def _player_marks(boss: dict) -> dict:
    marks = _as_dict(boss.get("marks"))
    boss["marks"] = marks
    return marks


def _mark_row(boss: dict, uid: int) -> dict:
    marks = _player_marks(boss)
    row = _as_dict(marks.get(str(uid)))
    marks[str(uid)] = row
    return row


def _clear_mark(boss: dict, uid: int) -> bool:
    marks = _player_marks(boss)
    if str(uid) not in marks:
        return False
    marks.pop(str(uid), None)
    boss["marks"] = marks
    return True


def _set_mark(
    boss: dict,
    uid: int,
    *,
    kind: str,
    name: str,
    source: str,
    now: datetime,
    duration_seconds: int,
) -> None:
    row = _mark_row(boss, uid)
    row["kind"] = str(kind or "mark").strip() or "mark"
    row["name"] = str(name or "Mark").strip() or "Mark"
    row["source"] = str(source or "boss").strip() or "boss"
    row["expires_at"] = _iso(now + timedelta(seconds=max(10, int(duration_seconds))))


def _clear_expired_marks(boss: dict, now: datetime) -> bool:
    changed = False
    marks = _player_marks(boss)
    for uid_s, raw in list(marks.items()):
        row = _as_dict(raw)
        expires_at = _parse_iso(row.get("expires_at"))
        if expires_at is None or expires_at > now:
            continue
        marks.pop(uid_s, None)
        changed = True
    if changed:
        boss["marks"] = marks
    return changed


def _has_ward(row: dict, now: datetime) -> bool:
    expires_at = _parse_iso(row.get("ward_expires_at"))
    return expires_at is not None and expires_at > now


def _grant_ward(row: dict, now: datetime) -> None:
    row["ward_expires_at"] = _iso(now + timedelta(seconds=int(BOSS_GUARD_DURATION_SECONDS)))


def _consume_ward(row: dict, now: datetime) -> bool:
    if not _has_ward(row, now):
        return False
    row["ward_expires_at"] = ""
    row["wards_consumed"] = _as_int(row.get("wards_consumed", 0), 0) + 1
    return True


def _grant_focus(
    row: dict,
    now: datetime,
    *,
    damage_bonus_pct: float,
    hit_bonus_pct: float,
) -> None:
    row["focus_damage_bonus_pct"] = max(float(row.get("focus_damage_bonus_pct", 0.0) or 0.0), float(damage_bonus_pct))
    row["focus_hit_bonus_pct"] = max(float(row.get("focus_hit_bonus_pct", 0.0) or 0.0), float(hit_bonus_pct))
    row["focus_expires_at"] = _iso(now + timedelta(seconds=int(BOSS_FOCUS_DURATION_SECONDS)))


def _focus_bonus(row: dict, now: datetime) -> tuple[float, float]:
    expires_at = _parse_iso(row.get("focus_expires_at"))
    if expires_at is None or expires_at <= now:
        return 0.0, 0.0
    return (
        max(0.0, float(row.get("focus_damage_bonus_pct", 0.0) or 0.0)),
        max(0.0, float(row.get("focus_hit_bonus_pct", 0.0) or 0.0)),
    )


def _consume_focus(row: dict) -> tuple[float, float]:
    damage_bonus = max(0.0, float(row.get("focus_damage_bonus_pct", 0.0) or 0.0))
    hit_bonus = max(0.0, float(row.get("focus_hit_bonus_pct", 0.0) or 0.0))
    row["focus_damage_bonus_pct"] = 0.0
    row["focus_hit_bonus_pct"] = 0.0
    row["focus_expires_at"] = ""
    return damage_bonus, hit_bonus


def _clear_expired_player_effects(boss: dict, now: datetime) -> bool:
    changed = False
    for raw in _as_dict(boss.get("attackers")).values():
        row = _as_dict(raw)
        if _parse_iso(row.get("ward_expires_at")) and not _has_ward(row, now):
            row["ward_expires_at"] = ""
            changed = True
        focus_expires_at = _parse_iso(row.get("focus_expires_at"))
        if focus_expires_at is not None and focus_expires_at <= now:
            if max(0.0, float(row.get("focus_damage_bonus_pct", 0.0) or 0.0)) > 0.0:
                changed = True
            if max(0.0, float(row.get("focus_hit_bonus_pct", 0.0) or 0.0)) > 0.0:
                changed = True
            row["focus_damage_bonus_pct"] = 0.0
            row["focus_hit_bonus_pct"] = 0.0
            row["focus_expires_at"] = ""
    return changed


def _boss_exposed_bonus(boss: dict, now: datetime) -> float:
    expires_at = _parse_iso(boss.get("exposed_until"))
    if expires_at is None or expires_at <= now:
        return 0.0
    return max(0.0, float(boss.get("exposed_bonus_pct", 0.0) or 0.0))


def _boss_is_stunned(boss: dict, now: datetime) -> bool:
    stunned_until = _parse_iso(boss.get("stunned_until"))
    return stunned_until is not None and stunned_until > now


def _pending_response_count(boss: dict) -> int:
    return len(_pending_mechanic(boss).get("responses", []))

def _root_state(gid: int) -> dict:
    g = _gdict(gid)
    st = g.get(BOSS_STATE_KEY)
    if not isinstance(st, dict):
        st = {}
        g[BOSS_STATE_KEY] = st
    st.setdefault("last_spawn_date", "")
    st.setdefault("next_spawn_at", "")
    cur = st.get("current")
    if not isinstance(cur, dict):
        cur = {}
        st["current"] = cur
    _boss_history(st)
    return st


def _current_boss(st: dict) -> dict:
    cur = _as_dict(st.get("current"))
    if _as_int(cur.get("event_id", 0), 0) <= 0:
        st["current"] = {}
        return {}
    cur.setdefault("attackers", {})
    cur.setdefault("recent_attackers", [])
    cur.setdefault("control_message_ids", [])
    cur.setdefault("last_message_id", 0)
    cur["attackers"] = _as_dict(cur.get("attackers"))
    cur["recent_attackers"] = [int(uid) for uid in _as_list(cur.get("recent_attackers")) if _as_int(uid, 0) > 0]
    cur["control_message_ids"] = [
        int(mid)
        for mid in _as_list(cur.get("control_message_ids"))
        if _as_int(mid, 0) > 0
    ][:BOSS_CONTROL_MESSAGE_LIMIT]
    cur["last_message_id"] = _as_int(cur.get("last_message_id", 0), 0)
    cur.setdefault("affix_key", "bulwarked")
    cur.setdefault("affix_name", AFFIXES["bulwarked"]["name"])
    cur.setdefault("affix_desc", AFFIXES["bulwarked"]["desc"])
    cur.setdefault("total_member_count", _as_int(cur.get("member_count", 1), 1))
    cur.setdefault("prestiged_member_count", _as_int(cur.get("member_count", 1), 1))
    cur.setdefault("phase", 1)
    cur.setdefault("phase_triggers", [])
    cur.setdefault("mechanic_count", 0)
    cur.setdefault("mechanics_countered", 0)
    cur.setdefault("mechanics_failed", 0)
    cur.setdefault("pending_mechanic", {})
    cur.setdefault("next_mechanic_at", "")
    cur.setdefault("stunned_until", "")
    cur.setdefault("exposed_until", "")
    cur.setdefault("exposed_bonus_pct", 0.0)
    cur.setdefault("marks", {})
    cur.setdefault("best_hit", 0)
    cur.setdefault("best_hit_by", "")
    cur["phase"] = max(1, min(4, _as_int(cur.get("phase", 1), 1)))
    cur["phase_triggers"] = _phase_triggers(cur)
    cur["pending_mechanic"] = _pending_mechanic(cur)
    cur["marks"] = _player_marks(cur)
    return cur


def _clear_current_boss(st: dict) -> None:
    st["current"] = {}


def _member_prestige(gid: int, uid: int) -> int:
    g = _gdict(gid)
    users = _as_dict(g.get("users"))
    u = _as_dict(users.get(str(uid)))
    return max(0, _as_int(u.get("prestige", 0), 0))


def _human_members(guild: discord.Guild) -> list[discord.Member]:
    return [member for member in guild.members if not member.bot]


def _guild_snapshot(guild: discord.Guild) -> dict[str, object]:
    members = _human_members(guild)
    total_member_count = len(members)
    prestiged_members: list[tuple[discord.Member, int]] = []
    for member in members:
        prestige = _member_prestige(guild.id, member.id)
        if prestige > 0:
            prestiged_members.append((member, prestige))

    scaling_members = prestiged_members or [(member, _member_prestige(guild.id, member.id)) for member in members]
    member_count = max(1, len(scaling_members))
    divisor = max(1, int(BOSS_TARGET_MEMBER_DIVISOR))
    target_fighters = max(1, (member_count + divisor - 1) // divisor)

    prestiges = [prestige for _, prestige in scaling_members]
    if not prestiges:
        prestiges = [0]
    min_prestige = min(prestiges)
    max_prestige = max(prestiges)
    avg_prestige = sum(prestiges) / float(len(prestiges))
    if max_prestige <= min_prestige:
        boss_prestige = max_prestige
    else:
        offset_target = int(round(avg_prestige + float(BOSS_AVG_PRESTIGE_OFFSET)))
        boss_prestige = max(min_prestige, min(max_prestige, offset_target))

    expected_hit = _hit_chance(
        {
            "min_prestige": int(min_prestige),
            "boss_prestige": int(boss_prestige),
        },
        int(round(avg_prestige)),
    )
    dmg_min = max(1, int(BOSS_DAMAGE_MIN))
    dmg_max = max(dmg_min, int(BOSS_DAMAGE_MAX))
    avg_roll = (dmg_min + dmg_max) / 2.0
    step = max(1, int(BOSS_DAMAGE_PRESTIGE_STEP))
    avg_bonus = (max(0, int(round(avg_prestige))) // step) * max(0, int(BOSS_DAMAGE_PRESTIGE_BONUS))
    crit_chance = 0.08
    if int(round(avg_prestige)) >= int(boss_prestige):
        crit_chance = 0.18
    elif int(round(avg_prestige)) >= max(1, int(boss_prestige) // 2):
        crit_chance = 0.12
    expected_damage_per_attack = expected_hit * (avg_roll + avg_bonus + (crit_chance * max(1, int(BOSS_DAMAGE_CRIT_BONUS))))
    attacks_per_window = max(1, int((BOSS_TARGET_CLEAR_MINUTES * 60) // max(10, int(BOSS_ATTACK_COOLDOWN_ACTIVE_SECONDS))))
    hp = max(40_000, int(round(target_fighters * attacks_per_window * expected_damage_per_attack * 0.80)))
    return {
        "member_count": int(member_count),
        "total_member_count": int(max(1, total_member_count)),
        "prestiged_member_count": int(len(prestiged_members)),
        "target_fighters": int(target_fighters),
        "avg_prestige": float(avg_prestige),
        "min_prestige": int(min_prestige),
        "max_prestige": int(max_prestige),
        "boss_prestige": int(boss_prestige),
        "hp": max(1, int(hp)),
        "expected_damage_per_attack": float(expected_damage_per_attack),
        "target_clear_minutes": int(BOSS_TARGET_CLEAR_MINUTES),
    }


def _spawn_at_local(guild_id: int, base_local: datetime, *, salt: str = "") -> datetime:
    start_hour = max(0, min(23, int(BOSS_SPAWN_START_HOUR)))
    end_hour = max(start_hour + 1, min(24, int(BOSS_SPAWN_END_HOUR)))
    candidate = base_local.astimezone(LOCAL_TZ).replace(second=0, microsecond=0)
    if start_hour <= candidate.hour < end_hour:
        return candidate

    if candidate.hour < start_hour:
        spawn_date = candidate.date()
    else:
        spawn_date = (candidate + timedelta(days=1)).date()

    total_minutes = max(1, ((end_hour - start_hour) * 60))
    seed = hashlib.sha256(f"boss:{guild_id}:{candidate.isoformat()}:{salt}".encode("utf-8")).digest()
    offset = int.from_bytes(seed[:4], "big") % total_minutes
    hour = start_hour + (offset // 60)
    minute = offset % 60
    return datetime(
        spawn_date.year,
        spawn_date.month,
        spawn_date.day,
        hour,
        minute,
        tzinfo=LOCAL_TZ,
    )


def _schedule_next_spawn(st: dict, guild_id: int, *, base_local: Optional[datetime] = None) -> datetime:
    now_local = base_local or datetime.now(LOCAL_TZ)
    min_hours = max(1, int(BOSS_SPAWN_MIN_DAYS))
    max_hours = max(min_hours, int(BOSS_SPAWN_MAX_DAYS))
    rng = random.SystemRandom()
    minute_offset = rng.randint(min_hours * 60, max_hours * 60)
    candidate_local = now_local + timedelta(minutes=minute_offset)
    salt = f"{now_local.isoformat()}:{rng.randint(0, 1_000_000)}"
    spawn_local = _spawn_at_local(guild_id, candidate_local, salt=salt)
    spawn_utc = spawn_local.astimezone(timezone.utc)
    st["next_spawn_at"] = _iso(spawn_utc)
    return spawn_utc


def _next_spawn_at(st: dict) -> Optional[datetime]:
    return _parse_iso(st.get("next_spawn_at"))


def _fmt_local_spawn(dt: datetime) -> str:
    local_dt = dt.astimezone(LOCAL_TZ)
    return local_dt.strftime("%B %d at %I:%M %p %Z").replace(" 0", " ")


def _seeded_rng(guild_id: int, seed_value: str) -> random.Random:
    digest = hashlib.sha256(f"{guild_id}:{seed_value}".encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")
    return random.Random(seed)


def _build_boss_name(rng: random.Random) -> tuple[str, str, str]:
    first = rng.choice(NAME_PREFIXES)
    if rng.random() < 0.75:
        first += rng.choice(NAME_MIDDLES)
    first += rng.choice(NAME_SUFFIXES)
    name = first[0].upper() + first[1:]

    if rng.random() < 0.6:
        title = f"{rng.choice(TITLE_ROLES)} of the {rng.choice(TITLE_PLACES)}"
    else:
        title = f"the {rng.choice(TITLE_ADJECTIVES)} {rng.choice(TITLE_NOUNS)}"

    return name, title, f"{name}, {title}"


def _slugify(text: str, *, max_length: int = 72) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    slug = slug or "boss"
    if len(slug) <= max_length:
        return slug
    return slug[:max_length].rstrip("-") or "boss"


def _channel_name_for_boss(boss: dict) -> str:
    return BOSS_CHANNEL_NAME


def _channel_topic_for_boss(boss: dict) -> str:
    return f"Raid Boss: {boss.get('display_name', 'Unknown Boss')}"


def _participant_row(boss: dict, member: discord.Member) -> dict:
    attackers = _as_dict(boss.get("attackers"))
    boss["attackers"] = attackers
    key = str(member.id)
    row = attackers.get(key)
    if not isinstance(row, dict):
        row = {}
        attackers[key] = row
    row.setdefault("display_name", member.display_name)
    row.setdefault("attacks", 0)
    row.setdefault("hits", 0)
    row.setdefault("misses", 0)
    row.setdefault("damage", 0)
    row.setdefault("resurrections", 0)
    row.setdefault("downs_taken", 0)
    row.setdefault("debuffs_taken", 0)
    row.setdefault("timeouts_taken", 0)
    row.setdefault("cooldown_extensions", 0)
    row.setdefault("next_attack_ts", 0.0)
    row.setdefault("next_res_ts", 0.0)
    row.setdefault("next_support_ts", 0.0)
    row.setdefault("first_attack_at", "")
    row.setdefault("last_attack_at", "")
    row.setdefault("guards", 0)
    row.setdefault("interrupts", 0)
    row.setdefault("cleanses", 0)
    row.setdefault("focuses", 0)
    row.setdefault("support_actions", 0)
    row.setdefault("mechanics_countered", 0)
    row.setdefault("marks_cleansed", 0)
    row.setdefault("focus_damage_bonus_pct", 0.0)
    row.setdefault("focus_hit_bonus_pct", 0.0)
    row.setdefault("focus_expires_at", "")
    row.setdefault("ward_expires_at", "")
    row.setdefault("wards_consumed", 0)
    return row


def _is_downed(boss: dict, uid: int) -> bool:
    return str(uid) in _as_dict(boss.get("downed"))


def _down_member(boss: dict, member: discord.Member, *, reason: str, now: datetime) -> None:
    downed = _as_dict(boss.get("downed"))
    boss["downed"] = downed
    downed[str(member.id)] = {
        "display_name": member.display_name,
        "reason": str(reason or "").strip() or "unknown",
        "at": _iso(now),
    }


def _revive_member(boss: dict, uid: int) -> bool:
    downed = _as_dict(boss.get("downed"))
    if str(uid) not in downed:
        return False
    downed.pop(str(uid), None)
    boss["downed"] = downed
    return True


def _recent_attackers(boss: dict) -> list[int]:
    return [int(uid) for uid in _as_list(boss.get("recent_attackers")) if _as_int(uid, 0) > 0]


def _push_recent_attacker(boss: dict, uid: int) -> None:
    rows = [int(v) for v in _recent_attackers(boss) if int(v) != int(uid)]
    rows.insert(0, int(uid))
    boss["recent_attackers"] = rows[:BOSS_ATTACKER_LIMIT]


def _control_message_ids(boss: dict) -> list[int]:
    return [int(mid) for mid in _as_list(boss.get("control_message_ids")) if _as_int(mid, 0) > 0]


def _register_control_message(boss: dict, message_id: int) -> None:
    ids = [mid for mid in _control_message_ids(boss) if mid != int(message_id)]
    ids.insert(0, int(message_id))
    boss["last_message_id"] = int(message_id)
    boss["control_message_ids"] = ids[:BOSS_CONTROL_MESSAGE_LIMIT]


def _is_active_control_message(boss: dict, message_id: int) -> bool:
    return int(message_id) in _control_message_ids(boss)


def _sorted_downed_ids(boss: dict) -> list[int]:
    rows: list[tuple[datetime, int]] = []
    for uid_s, raw in _as_dict(boss.get("downed")).items():
        uid = _as_int(uid_s, 0)
        if uid <= 0:
            continue
        row = _as_dict(raw)
        at = _parse_iso(row.get("at")) or datetime.fromtimestamp(0, tz=timezone.utc)
        rows.append((at, uid))
    rows.sort(key=lambda item: (item[0], item[1]))
    return [uid for _, uid in rows]


def _pick_downed_target(guild: discord.Guild, boss: dict) -> Optional[discord.Member]:
    for uid in _sorted_downed_ids(boss):
        member = guild.get_member(uid)
        if member is not None and not member.bot:
            return member
    return None


def _pick_other_recent_attacker(guild: discord.Guild, boss: dict, attacker_id: int) -> Optional[discord.Member]:
    for uid in _recent_attackers(boss):
        if int(uid) == int(attacker_id):
            continue
        member = guild.get_member(int(uid))
        if member is not None and not member.bot:
            return member
    return None


def _hit_chance(boss: dict, prestige: int) -> float:
    min_prestige = max(0, _as_int(boss.get("min_prestige", 0), 0))
    boss_prestige = max(min_prestige, _as_int(boss.get("boss_prestige", 0), min_prestige))
    if boss_prestige <= min_prestige:
        return 0.75
    ratio = (max(min_prestige, int(prestige)) - min_prestige) / float(max(1, boss_prestige - min_prestige))
    return max(0.5, min(1.0, 0.5 + (0.5 * ratio)))


def _roll_damage(rng: random.Random, prestige: int, boss: dict) -> tuple[int, bool]:
    dmg_min = max(1, int(BOSS_DAMAGE_MIN))
    dmg_max = max(dmg_min, int(BOSS_DAMAGE_MAX))
    damage = rng.randint(dmg_min, dmg_max)
    step = max(1, int(BOSS_DAMAGE_PRESTIGE_STEP))
    damage += max(0, int(prestige)) // step * max(0, int(BOSS_DAMAGE_PRESTIGE_BONUS))

    crit_bonus = False
    boss_prestige = max(0, _as_int(boss.get("boss_prestige", 0), 0))
    crit_chance = 0.08
    if prestige >= boss_prestige:
        crit_chance = 0.18
    elif prestige >= max(1, boss_prestige // 2):
        crit_chance = 0.12
    if rng.random() < crit_chance:
        damage += max(1, int(BOSS_DAMAGE_CRIT_BONUS))
        crit_bonus = True
    return max(1, int(damage)), crit_bonus


class BossCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not self.boss_loop.is_running():
            self.boss_loop.start()

    def cog_unload(self):
        if self.boss_loop.is_running():
            self.boss_loop.cancel()

    async def _ensure_boss_channel(self, guild: discord.Guild, boss: dict) -> Optional[discord.TextChannel]:
        channel_id = _as_int(boss.get("channel_id", 0), 0)
        existing = guild.get_channel(channel_id)
        if isinstance(existing, discord.TextChannel):
            await self._sync_channel_name(guild, boss)
            return existing

        named = discord.utils.get(guild.text_channels, name=_channel_name_for_boss(boss))
        if isinstance(named, discord.TextChannel):
            boss["channel_id"] = int(named.id)
            await self._sync_channel_name(guild, boss)
            boss["last_channel_name"] = str(named.name)
            return named

        log_channel = get_log_channel(guild)
        category = log_channel.category if log_channel is not None else None
        name = _channel_name_for_boss(boss)
        try:
            channel = await guild.create_text_channel(
                name=name,
                category=category,
                topic=_channel_topic_for_boss(boss),
                reason="Paragon raid boss spawn",
            )
        except (discord.Forbidden, discord.HTTPException):
            return None

        boss["channel_id"] = int(channel.id)
        boss["last_channel_name"] = str(channel.name)
        return channel

    async def _sync_channel_name(self, guild: discord.Guild, boss: dict) -> None:
        channel = guild.get_channel(_as_int(boss.get("channel_id", 0), 0))
        if not isinstance(channel, discord.TextChannel):
            return
        desired = _channel_name_for_boss(boss)
        desired_topic = _channel_topic_for_boss(boss)
        if str(channel.name) == desired and str(channel.topic or "") == desired_topic:
            boss["last_channel_name"] = str(channel.name)
            return
        try:
            await channel.edit(
                name=desired,
                topic=desired_topic,
                reason="Paragon boss channel sync",
            )
            boss["last_channel_name"] = str(channel.name)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _track_control_message(self, boss: dict, message: discord.Message) -> None:
        _register_control_message(boss, message.id)
        await save_data()
        for emoji in (BOSS_REACTION_ATTACK, BOSS_REACTION_RES):
            try:
                await message.add_reaction(emoji)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def _send_boss_message(
        self,
        channel: discord.TextChannel,
        boss: dict,
        content: str,
        *,
        reference: Optional[discord.Message] = None,
        ping_here: bool = False,
    ) -> Optional[discord.Message]:
        kwargs: dict[str, object] = {}
        text = str(content or "")
        if ping_here and channel.permissions_for(channel.guild.default_role).view_channel:
            text = f"@here {text}"
            kwargs["allowed_mentions"] = discord.AllowedMentions(everyone=True)
        if reference is not None:
            kwargs["reference"] = reference
            kwargs["mention_author"] = False
        try:
            message = await channel.send(text, **kwargs)
        except TypeError:
            message = await channel.send(text)
        except (discord.Forbidden, discord.HTTPException):
            return None
        await self._track_control_message(boss, message)
        return message

    async def _remove_user_reaction(
        self,
        channel: discord.TextChannel,
        message_id: int,
        emoji: discord.PartialEmoji,
        member: discord.Member,
    ) -> None:
        try:
            await channel.get_partial_message(int(message_id)).remove_reaction(emoji, member)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

    def _mechanic_interval_seconds(self, boss: dict) -> int:
        affix = _affix_data(boss)
        phase = max(1, _as_int(boss.get("phase", 1), 1))
        mult = max(0.70, float(affix.get("interval_mult", 1.0)) - ((phase - 1) * 0.04))
        low = max(20, int(round(BOSS_MECHANIC_INTERVAL_MIN_SECONDS * mult)))
        high = max(low, int(round(BOSS_MECHANIC_INTERVAL_MAX_SECONDS * mult)))
        return random.randint(low, high)

    def _schedule_next_mechanic(
        self,
        boss: dict,
        now: datetime,
        *,
        immediate: bool = False,
        delay_seconds: Optional[int] = None,
    ) -> None:
        if immediate:
            delay = int(BOSS_PHASE_TRIGGER_DELAY_SECONDS)
        elif delay_seconds is not None:
            delay = max(10, int(delay_seconds))
        else:
            delay = self._mechanic_interval_seconds(boss)
        boss["next_mechanic_at"] = _iso(now + timedelta(seconds=delay))

    def _mechanic_requirement(self, boss: dict, counter: str) -> int:
        size = max(1, _as_int(boss.get("target_fighters", 1), 1))
        phase = max(1, _as_int(boss.get("phase", 1), 1))
        if counter == "guard":
            need = max(1, min(3, (size + 1) // 2))
        elif counter == "interrupt":
            need = 1 if size <= 2 else 2 if size <= 6 else 3
        else:
            need = 1 if size <= 3 else 2 if size <= 7 else 3
        if phase >= 4 and counter in {"interrupt", "purge"}:
            need = min(3, need + 1)
        return need

    def _pick_mechanic_key(self, boss: dict) -> str:
        weights = {
            "crushing_slam": 3,
            "soul_scream": 3,
            "blight_bloom": 2,
        }
        bias = str(_affix_data(boss).get("mechanic_bias", "")).strip().lower()
        if bias == "guard":
            weights["crushing_slam"] += 2
        elif bias == "interrupt":
            weights["soul_scream"] += 2
        elif bias == "cleanse":
            weights["blight_bloom"] += 2
        if len(_player_marks(boss)) >= 2:
            weights["blight_bloom"] += 2
        if _as_int(boss.get("phase", 1), 1) >= 3:
            weights["soul_scream"] += 1
        keys = list(weights.keys())
        return random.choices(keys, weights=[weights[key] for key in keys], k=1)[0]

    def _pending_mechanic_line(self, boss: dict, now: datetime) -> str:
        pending = _pending_mechanic(boss)
        key = str(pending.get("key", "")).strip().lower()
        if not key:
            return ""
        due_at = _parse_iso(pending.get("due_at"))
        if due_at is None:
            return ""
        mechanic = MECHANIC_DEFS.get(key)
        if mechanic is None:
            return ""
        remaining = max(0, int((due_at - now).total_seconds()))
        return (
            f"Incoming mechanic: **{mechanic['name']}** in **{_fmt_remaining(remaining)}**. "
            f"Use `{COMMAND_PREFIX}{mechanic['counter']}` "
            f"({max(0, _pending_response_count(boss))}/{_as_int(pending.get('required', 1), 1)})."
        )

    def _mechanic_targets(self, guild: discord.Guild, boss: dict, *, limit: int) -> list[discord.Member]:
        picked: list[discord.Member] = []
        seen: set[int] = set()
        for uid in _recent_attackers(boss):
            member = guild.get_member(int(uid))
            if member is None or member.bot or member.id in seen:
                continue
            picked.append(member)
            seen.add(member.id)
            if len(picked) >= limit:
                return picked
        rows: list[tuple[int, dict]] = []
        for uid_s, raw in _as_dict(boss.get("attackers")).items():
            uid = _as_int(uid_s, 0)
            row = _as_dict(raw)
            if uid <= 0 or not _is_contributor_row(row) or uid in seen:
                continue
            rows.append((uid, row))
        rows.sort(
            key=lambda item: (
                -_as_int(item[1].get("attacks", 0), 0),
                -_support_score(item[1]),
                item[0],
            )
        )
        for uid, row in rows:
            member = guild.get_member(uid)
            if member is None or member.bot:
                continue
            picked.append(member)
            if len(picked) >= limit:
                break
        return picked

    def _register_mechanic_response(self, boss: dict, member: discord.Member, action: str) -> bool:
        pending = _pending_mechanic(boss)
        if str(pending.get("counter", "")).strip().lower() != str(action or "").strip().lower():
            return False
        uid = int(member.id)
        rows = [int(v) for v in _as_list(pending.get("responses")) if _as_int(v, 0) > 0]
        if uid in rows:
            return False
        rows.append(uid)
        pending["responses"] = rows
        boss["pending_mechanic"] = pending
        return True

    async def _apply_phase_transition(
        self,
        guild: discord.Guild,
        boss: dict,
        channel: discord.TextChannel,
        *,
        phase: int,
        now: datetime,
    ) -> None:
        boss["phase"] = int(phase)
        triggers = [v for v in _phase_triggers(boss) if v != int(phase)]
        triggers.append(int(phase))
        boss["phase_triggers"] = sorted(set(triggers))
        lines = [
            f"**Phase Shift: {_phase_name(phase)}**",
        ]
        if phase == 2:
            boss["exposed_until"] = _iso(now + timedelta(seconds=20))
            boss["exposed_bonus_pct"] = 0.12 + float(_affix_data(boss).get("expose_bonus_pct", 0.0))
            lines.append("Its shell cracks for a moment. Push damage while the opening is there.")
        elif phase == 3:
            targets = self._mechanic_targets(guild, boss, limit=max(1, min(2, _as_int(boss.get("target_fighters", 1), 1))))
            duration = int(round(BOSS_MARK_DURATION_SECONDS * float(_affix_data(boss).get("mark_duration_mult", 1.0))))
            for member in targets:
                _set_mark(
                    boss,
                    member.id,
                    kind="blight",
                    name="Blight",
                    source="phase shift",
                    now=now,
                    duration_seconds=duration,
                )
            if targets:
                lines.append(
                    "Blight spills across the floor: "
                    + ", ".join(member.mention for member in targets)
                    + " should be cleansed soon."
                )
            else:
                lines.append("The chamber poisons itself. Expect heavier cleanse pressure.")
        else:
            boss["stunned_until"] = ""
            boss["exposed_until"] = _iso(now + timedelta(seconds=25))
            boss["exposed_bonus_pct"] = 0.18 + float(_affix_data(boss).get("expose_bonus_pct", 0.0))
            lines.append("The boss is desperate and unstable. Mechanics will come faster, but every opening matters.")
        self._schedule_next_mechanic(boss, now, immediate=True)
        await self._send_boss_message(channel, boss, "\n".join(lines))

    async def _maybe_handle_phase_transition(
        self,
        guild: discord.Guild,
        boss: dict,
        channel: discord.TextChannel,
        *,
        now: datetime,
    ) -> bool:
        changed = False
        target_phase = _phase_for_ratio(_as_int(boss.get("hp", 0), 0), _as_int(boss.get("max_hp", 1), 1))
        current_phase = max(1, _as_int(boss.get("phase", 1), 1))
        while current_phase < target_phase:
            current_phase += 1
            await self._apply_phase_transition(guild, boss, channel, phase=current_phase, now=now)
            changed = True
        return changed

    async def _maybe_start_pending_mechanic(
        self,
        guild: discord.Guild,
        boss: dict,
        channel: discord.TextChannel,
        *,
        now: datetime,
    ) -> bool:
        pending = _pending_mechanic(boss)
        if str(pending.get("key", "")).strip():
            return False
        next_mechanic_at = _parse_iso(boss.get("next_mechanic_at"))
        if next_mechanic_at is None or now < next_mechanic_at:
            return False
        key = self._pick_mechanic_key(boss)
        mechanic = MECHANIC_DEFS[key]
        warning_seconds = random.randint(BOSS_MECHANIC_WARNING_MIN_SECONDS, BOSS_MECHANIC_WARNING_MAX_SECONDS)
        due_at = now + timedelta(seconds=warning_seconds)
        pending = {
            "key": key,
            "counter": mechanic["counter"],
            "name": mechanic["name"],
            "required": self._mechanic_requirement(boss, mechanic["counter"]),
            "due_at": _iso(due_at),
            "responses": [],
            "created_at": _iso(now),
        }
        boss["pending_mechanic"] = pending
        boss["mechanic_count"] = _as_int(boss.get("mechanic_count", 0), 0) + 1
        lines = [
            f"**Boss Telegraph: {mechanic['name']}**",
            mechanic["warning"],
            f"Need **{_as_int(pending.get('required', 1), 1)}** raider(s) to `{COMMAND_PREFIX}{mechanic['counter']}` in **{_fmt_remaining(warning_seconds)}**.",
        ]
        await self._send_boss_message(channel, boss, "\n".join(lines), ping_here=True)
        return True

    async def _maybe_resolve_due_mechanic(
        self,
        guild: discord.Guild,
        boss: dict,
        channel: discord.TextChannel,
        *,
        now: datetime,
    ) -> bool:
        pending = _pending_mechanic(boss)
        key = str(pending.get("key", "")).strip().lower()
        if not key:
            return False
        due_at = _parse_iso(pending.get("due_at"))
        if due_at is None or now < due_at:
            return False

        responses = [int(uid) for uid in _as_list(pending.get("responses")) if _as_int(uid, 0) > 0]
        required = max(1, _as_int(pending.get("required", 1), 1))
        success = len(responses) >= required
        affix = _affix_data(boss)
        lines: list[str] = [f"**{pending.get('name', 'Boss Mechanic')}** resolves."]

        if success:
            boss["mechanics_countered"] = _as_int(boss.get("mechanics_countered", 0), 0) + 1
            st = _root_state(guild.id)
            hist = _boss_history(st)
            hist["mechanics_countered"] = _as_int(hist.get("mechanics_countered", 0), 0) + 1
            for uid in responses:
                member = guild.get_member(uid)
                if member is None or member.bot:
                    continue
                row = _participant_row(boss, member)
                row["mechanics_countered"] = _as_int(row.get("mechanics_countered", 0), 0) + 1
                record_game_fields(guild.id, uid, "boss", mechanics_countered=1)
            if key == "crushing_slam":
                boss["exposed_until"] = _iso(now + timedelta(seconds=BOSS_EXPOSE_DURATION_SECONDS))
                boss["exposed_bonus_pct"] = BOSS_EXPOSE_DAMAGE_BONUS_PCT + float(affix.get("expose_bonus_pct", 0.0))
                lines.append(
                    f"The raid braces together and cracks the boss open. Damage is boosted by **{_fmt_pct(boss['exposed_bonus_pct'])}** for **{_fmt_remaining(BOSS_EXPOSE_DURATION_SECONDS)}**."
                )
            elif key == "soul_scream":
                stun_seconds = int(BOSS_STUN_DURATION_SECONDS + _as_int(affix.get("stun_bonus_seconds", 0), 0))
                boss["stunned_until"] = _iso(now + timedelta(seconds=stun_seconds))
                lines.append(f"The scream is cut off. The boss is staggered for **{_fmt_remaining(stun_seconds)}**.")
            elif key == "blight_bloom":
                cleared = len(_player_marks(boss))
                boss["marks"] = {}
                boss["exposed_until"] = _iso(now + timedelta(seconds=20))
                boss["exposed_bonus_pct"] = 0.12
                lines.append(
                    f"The raid purges the bloom and clears **{cleared}** mark(s). Damage is boosted by **12%** for **20s**."
                )
        else:
            boss["mechanics_failed"] = _as_int(boss.get("mechanics_failed", 0), 0) + 1
            max_hp = max(1, _as_int(boss.get("max_hp", 1), 1))
            if key == "crushing_slam":
                targets = self._mechanic_targets(guild, boss, limit=max(1, min(2, _as_int(boss.get("target_fighters", 1), 1))))
                downed: list[str] = []
                for member in targets:
                    row = _participant_row(boss, member)
                    if _consume_ward(row, now):
                        continue
                    _down_member(boss, member, reason="Crushing Slam", now=now)
                    row["downs_taken"] = _as_int(row.get("downs_taken", 0), 0) + 1
                    boss["down_count"] = _as_int(boss.get("down_count", 0), 0) + 1
                    downed.append(member.mention)
                heal = int(round(max_hp * 0.04 * float(affix.get("heal_mult", 1.0))))
                before = _as_int(boss.get("hp", 0), 0)
                boss["hp"] = min(max_hp, before + heal)
                healed = max(0, _as_int(boss.get("hp", 0), 0) - before)
                boss["heal_total"] = _as_int(boss.get("heal_total", 0), 0) + healed
                if downed:
                    lines.append("The chamber caves in. Downed: " + ", ".join(downed) + ".")
                else:
                    lines.append("The raid barely avoids the full impact, but the boss still slips away from the pressure.")
                lines.append(f"The boss siphons **{_fmt_num(healed)} HP** back from the shockwave.")
            elif key == "soul_scream":
                extra = 20 + (_as_int(boss.get("phase", 1), 1) * 5)
                targets = self._mechanic_targets(guild, boss, limit=max(1, min(4, _as_int(boss.get("target_fighters", 1), 1) + 1)))
                caught: list[str] = []
                for member in targets:
                    row = _participant_row(boss, member)
                    if _consume_ward(row, now):
                        continue
                    row["next_attack_ts"] = max(float(row.get("next_attack_ts", 0.0)), now.timestamp()) + extra
                    row["cooldown_extensions"] = _as_int(row.get("cooldown_extensions", 0), 0) + 1
                    caught.append(member.mention)
                heal = int(round(max_hp * 0.03 * float(affix.get("heal_mult", 1.0))))
                before = _as_int(boss.get("hp", 0), 0)
                boss["hp"] = min(max_hp, before + heal)
                healed = max(0, _as_int(boss.get("hp", 0), 0) - before)
                boss["heal_total"] = _as_int(boss.get("heal_total", 0), 0) + healed
                if caught:
                    lines.append(
                        "The scream lands. "
                        + ", ".join(caught)
                        + f" are delayed by **{_fmt_remaining(extra)}**."
                    )
                lines.append(f"The boss recovers **{_fmt_num(healed)} HP** during the chaos.")
            elif key == "blight_bloom":
                duration = int(round(BOSS_MARK_DURATION_SECONDS * float(affix.get("mark_duration_mult", 1.0))))
                targets = self._mechanic_targets(guild, boss, limit=max(1, min(4, _as_int(boss.get("target_fighters", 1), 1) + 1)))
                marked: list[str] = []
                for member in targets:
                    row = _participant_row(boss, member)
                    if _consume_ward(row, now):
                        continue
                    _set_mark(
                        boss,
                        member.id,
                        kind="blight",
                        name="Blight",
                        source="Blight Bloom",
                        now=now,
                        duration_seconds=duration,
                    )
                    row["debuffs_taken"] = _as_int(row.get("debuffs_taken", 0), 0) + 1
                    marked.append(member.mention)
                heal = int(round(max_hp * 0.025 * float(affix.get("heal_mult", 1.0))))
                before = _as_int(boss.get("hp", 0), 0)
                boss["hp"] = min(max_hp, before + heal)
                healed = max(0, _as_int(boss.get("hp", 0), 0) - before)
                boss["heal_total"] = _as_int(boss.get("heal_total", 0), 0) + healed
                if marked:
                    lines.append(
                        "Rot spreads across "
                        + ", ".join(marked)
                        + f" for **{_fmt_remaining(duration)}** unless cleansed."
                    )
                lines.append(f"The boss drinks **{_fmt_num(healed)} HP** from the bloom.")

        boss["pending_mechanic"] = {}
        self._schedule_next_mechanic(boss, now)
        await self._send_boss_message(channel, boss, "\n".join(lines))
        return True

    async def _refresh_boss_runtime(
        self,
        guild: discord.Guild,
        boss: dict,
        channel: Optional[discord.TextChannel],
    ) -> bool:
        if channel is None:
            return False
        now = _utcnow()
        changed = _clear_expired_marks(boss, now)
        changed = _clear_expired_player_effects(boss, now) or changed
        if str(boss.get("status", "idle")).strip().lower() != "active":
            return changed
        if await self._maybe_resolve_due_mechanic(guild, boss, channel, now=now):
            changed = True
        if await self._maybe_start_pending_mechanic(guild, boss, channel, now=now):
            changed = True
        return changed

    def _make_new_boss(self, guild: discord.Guild, now: datetime, *, seed_value: str) -> dict[str, object]:
        snapshot = _guild_snapshot(guild)
        rng = _seeded_rng(guild.id, seed_value)
        _, _, display_name = _build_boss_name(rng)
        affix_key = rng.choice(tuple(AFFIXES.keys()))
        affix = AFFIXES[affix_key]
        duration_minutes = int(BOSS_ACTIVE_DURATION_MINUTES)
        max_hp = max(1, int(round(float(snapshot["hp"]) * float(affix.get("hp_mult", 1.0)))))
        return {
            "event_id": int(now.timestamp()),
            "display_name": display_name,
            "slug": _slugify(display_name),
            "status": "idle",
            "created_at": _iso(now),
            "engaged_at": "",
            "expires_at": "",
            "idle_expires_at": _iso(now + timedelta(hours=max(1, int(BOSS_IDLE_MAX_HOURS)))),
            "duration_minutes": int(duration_minutes),
            "max_hp": int(max_hp),
            "hp": int(max_hp),
            "member_count": int(snapshot["member_count"]),
            "total_member_count": int(snapshot.get("total_member_count", snapshot["member_count"])),
            "prestiged_member_count": int(snapshot.get("prestiged_member_count", snapshot["member_count"])),
            "target_fighters": int(snapshot["target_fighters"]),
            "avg_prestige": float(snapshot["avg_prestige"]),
            "min_prestige": int(snapshot["min_prestige"]),
            "max_prestige": int(snapshot["max_prestige"]),
            "boss_prestige": int(snapshot["boss_prestige"]),
            "expected_damage_per_attack": float(snapshot.get("expected_damage_per_attack", 0.0)),
            "target_clear_minutes": int(snapshot.get("target_clear_minutes", BOSS_TARGET_CLEAR_MINUTES)),
            "affix_key": affix_key,
            "affix_name": str(affix.get("name", "Unknown Affix")),
            "affix_desc": str(affix.get("desc", "")),
            "phase": 1,
            "phase_triggers": [],
            "channel_id": 0,
            "last_channel_name": "",
            "attack_count": 0,
            "hit_count": 0,
            "total_damage": 0,
            "retaliations": 0,
            "heal_total": 0,
            "down_count": 0,
            "timeouts_inflicted": 0,
            "mechanic_count": 0,
            "mechanics_countered": 0,
            "mechanics_failed": 0,
            "pending_mechanic": {},
            "next_mechanic_at": "",
            "stunned_until": "",
            "exposed_until": "",
            "exposed_bonus_pct": 0.0,
            "marks": {},
            "best_hit": 0,
            "best_hit_by": "",
            "attackers": {},
            "downed": {},
            "recent_attackers": [],
            "control_message_ids": [],
            "last_message_id": 0,
        }

    async def _spawn_boss(self, guild: discord.Guild, *, forced: bool = False) -> bool:
        if not BOSS_ENABLED and not forced:
            return False

        if not _human_members(guild):
            return False

        st = _root_state(guild.id)
        if _current_boss(st):
            return False

        now = _utcnow()
        await ensure_guild_setup(guild)
        seed_value = f"boss:{now.date().isoformat()}:{'forced' if forced else 'daily'}:{now.hour}:{now.minute}"
        boss = self._make_new_boss(guild, now, seed_value=seed_value)
        channel = await self._ensure_boss_channel(guild, boss)
        if channel is None:
            return False

        st["current"] = boss
        st["last_spawn_date"] = datetime.now(LOCAL_TZ).date().isoformat()
        hist = _boss_history(st)
        hist["spawns"] = _as_int(hist.get("spawns", 0), 0) + 1
        if not forced:
            _schedule_next_spawn(st, guild.id)
        elif _next_spawn_at(st) is None:
            _schedule_next_spawn(st, guild.id)
        await save_data()
        await self._announce_boss_spawn(guild, boss, channel, forced=forced)
        return True

    async def _announce_boss_spawn(
        self,
        guild: discord.Guild,
        boss: dict,
        channel: discord.TextChannel,
        *,
        forced: bool = False,
    ) -> None:
        prefix = "@everyone " if channel.permissions_for(guild.default_role).view_channel else ""
        timer_line = (
            f"This boss is dormant until someone uses `{COMMAND_PREFIX}attack`. "
            f"Once engaged, the kill timer starts for **{_fmt_duration_minutes(_as_int(boss.get('duration_minutes', 0), 0))}**."
        )
        affix = _affix_data(boss)
        lines = [
            f"{prefix}**Raid Boss Appeared**",
            f"**{boss.get('display_name', 'Unknown Boss')}**",
            f"Affix: **{boss.get('affix_name', 'Unknown')}** - {boss.get('affix_desc', '')}",
            f"HP: **{_fmt_num(boss.get('hp', 0))} / {_fmt_num(boss.get('max_hp', 0))}**",
            (
                f"Tuned for about **{_as_int(boss.get('target_fighters', 1), 1)}** fighters out of "
                f"**{_as_int(boss.get('member_count', 1), 1)}** eligible prestige member(s)."
            ),
            (
                f"Target clear pace: about **{_fmt_duration_minutes(_as_int(boss.get('target_clear_minutes', BOSS_TARGET_CLEAR_MINUTES), BOSS_TARGET_CLEAR_MINUTES))}** "
                "for a raid of that size, leaving solo or duo clears possible if the run goes well."
            ),
            (
                f"Boss prestige: **{_as_int(boss.get('boss_prestige', 0), 0)}** "
                f"(guild average **{_as_float(boss.get('avg_prestige', 0.0), 0.0):.1f}**). "
                "Hit chance scales from **50%** at the low end to **100%** at the top end."
            ),
            timer_line,
            f"`{COMMAND_PREFIX}attack` once every **{_fmt_remaining(_attack_cooldown_seconds(boss))}**",
            f"`{COMMAND_PREFIX}res @user` or `{COMMAND_PREFIX}resurrect @user` to revive a downed raider",
            f"`{COMMAND_PREFIX}guard`, `{COMMAND_PREFIX}interrupt`, `{COMMAND_PREFIX}purge [@user]`, `{COMMAND_PREFIX}focus [@user]` for raid support and mechanic counters",
            f"React **{BOSS_REACTION_ATTACK}** to attack or **{BOSS_REACTION_RES}** to revive the oldest downed raider on the newest boss message",
            f"`{COMMAND_PREFIX}boss` from anywhere to check status",
            (
                f"Victory reward: contributors receive **+{_fmt_pct(BOSS_BASE_REWARD_PCT)} XP/min** "
                f"for **{_fmt_duration_minutes(int(BOSS_BASE_REWARD_MINUTES))}**."
            ),
            (
                f"Failure penalty: contributors take **-{_fmt_pct(BOSS_FAILURE_PENALTY_PCT)} XP/min** "
                f"for **{_fmt_duration_minutes(int(BOSS_FAILURE_PENALTY_MINUTES))}**."
            ),
        ]
        ignored_count = max(
            0,
            _as_int(boss.get("total_member_count", 0), 0) - _as_int(boss.get("member_count", 0), 0),
        )
        if ignored_count > 0:
            lines.append(f"*Ignored **{ignored_count}** prestige 0 member(s) for boss tuning.*")
        if forced:
            lines.append("*Forced spawn for testing/admin use.*")
        try:
            message = await channel.send(
                "\n".join(lines),
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
        except Exception:
            return
        await self._track_control_message(boss, message)

    async def _announce_log(self, guild: discord.Guild, text: str) -> None:
        log_channel = get_log_channel(guild)
        if log_channel is None:
            return
        try:
            await log_channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    async def _delete_boss_channel(self, guild: discord.Guild, boss: dict) -> None:
        channel = guild.get_channel(_as_int(boss.get("channel_id", 0), 0))
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.delete(reason="Paragon raid boss cleanup")
        except (discord.Forbidden, discord.HTTPException):
            pass

    def _rollback_boss_stats(self, guild_id: int, boss: dict) -> None:
        for uid_s, raw in _as_dict(boss.get("attackers")).items():
            uid = _as_int(uid_s, 0)
            if uid <= 0:
                continue
            row = _as_dict(raw)
            deltas: dict[str, int] = {}
            attacks = _as_int(row.get("attacks", 0), 0)
            hits = _as_int(row.get("hits", 0), 0)
            misses = _as_int(row.get("misses", 0), 0)
            damage = _as_int(row.get("damage", 0), 0)
            resurrections = _as_int(row.get("resurrections", 0), 0)
            support_actions = _as_int(row.get("support_actions", 0), 0)
            guards = _as_int(row.get("guards", 0), 0)
            interrupts = _as_int(row.get("interrupts", 0), 0)
            cleanses = _as_int(row.get("cleanses", 0), 0)
            focuses = _as_int(row.get("focuses", 0), 0)
            mechanics_countered = _as_int(row.get("mechanics_countered", 0), 0)
            marks_cleansed = _as_int(row.get("marks_cleansed", 0), 0)
            if attacks > 0:
                deltas["attacks"] = -attacks
            if hits > 0:
                deltas["hits"] = -hits
            if misses > 0:
                deltas["misses"] = -misses
            if damage > 0:
                deltas["damage_total"] = -damage
            if resurrections > 0:
                deltas["resurrections"] = -resurrections
            if support_actions > 0:
                deltas["support_actions"] = -support_actions
            if guards > 0:
                deltas["guards"] = -guards
            if interrupts > 0:
                deltas["interrupts"] = -interrupts
            if cleanses > 0:
                deltas["cleanses"] = -cleanses
            if focuses > 0:
                deltas["focuses"] = -focuses
            if mechanics_countered > 0:
                deltas["mechanics_countered"] = -mechanics_countered
            if marks_cleansed > 0:
                deltas["marks_cleansed"] = -marks_cleansed
            if deltas:
                record_game_fields(guild_id, uid, "boss", **deltas)

    async def _clear_active_boss(self, guild: discord.Guild, boss: dict) -> None:
        st = _root_state(guild.id)
        await self._delete_boss_channel(guild, boss)
        _clear_current_boss(st)
        if _next_spawn_at(st) is None:
            _schedule_next_spawn(st, guild.id)
        await save_data()

    def _contributor_rows(self, boss: dict) -> list[tuple[int, dict]]:
        rows: list[tuple[int, dict]] = []
        for uid_s, raw in _as_dict(boss.get("attackers")).items():
            uid = _as_int(uid_s, 0)
            row = _as_dict(raw)
            if uid <= 0 or not _is_contributor_row(row):
                continue
            rows.append((uid, row))
        return rows

    def _top_damage_row(self, boss: dict) -> Optional[tuple[int, dict]]:
        rows = self._contributor_rows(boss)
        if not rows:
            return None
        rows.sort(
            key=lambda item: (
                -_as_int(item[1].get("damage", 0), 0),
                -_as_int(item[1].get("hits", 0), 0),
                -_as_int(item[1].get("attacks", 0), 0),
                item[0],
            )
        )
        return rows[0]

    def _top_support_row(self, boss: dict) -> Optional[tuple[int, dict]]:
        rows = self._contributor_rows(boss)
        rows = [item for item in rows if _support_score(item[1]) > 0]
        if not rows:
            return None
        rows.sort(
            key=lambda item: (
                -_support_score(item[1]),
                -_as_int(item[1].get("resurrections", 0), 0),
                -_as_int(item[1].get("mechanics_countered", 0), 0),
                item[0],
            )
        )
        return rows[0]

    async def _resolve_victory(
        self,
        guild: discord.Guild,
        boss: dict,
        *,
        killer: Optional[discord.Member] = None,
    ) -> None:
        rewarded = 0
        contributors = self._contributor_rows(boss)
        top_damage = self._top_damage_row(boss)
        top_support = self._top_support_row(boss)
        bonus_winners: dict[int, set[str]] = {}
        if top_damage is not None:
            bonus_winners.setdefault(int(top_damage[0]), set()).add("mvp_awards")
        if top_support is not None:
            bonus_winners.setdefault(int(top_support[0]), set()).add("support_awards")
        flawless_ids = {
            int(uid)
            for uid, row in contributors
            if _as_int(row.get("downs_taken", 0), 0) <= 0
        }

        for uid, row in contributors:
            member = guild.get_member(uid)
            if member is None or member.bot:
                continue
            await grant_fixed_boost(
                member,
                pct=BOSS_BASE_REWARD_PCT,
                minutes=BOSS_BASE_REWARD_MINUTES,
                source=f"boss victory: {boss.get('display_name', 'raid boss')}",
                reward_seed_xp=(BOSS_BASE_REWARD_PCT * 100.0) * max(1, int(BOSS_BASE_REWARD_MINUTES)),
                persist=False,
            )
            record_game_fields(guild.id, member.id, "boss", victory_rewards=1)
            rewarded += 1
            if uid in bonus_winners:
                await grant_fixed_boost(
                    member,
                    pct=BOSS_BONUS_REWARD_PCT,
                    minutes=BOSS_BONUS_REWARD_MINUTES,
                    source=f"boss commendation: {boss.get('display_name', 'raid boss')}",
                    reward_seed_xp=(BOSS_BONUS_REWARD_PCT * 100.0) * max(1, int(BOSS_BONUS_REWARD_MINUTES)),
                    persist=False,
                )
                record_game_fields(
                    guild.id,
                    member.id,
                    "boss",
                    **{field: 1 for field in sorted(bonus_winners[uid])},
                )
            if uid in flawless_ids:
                await grant_fixed_boost(
                    member,
                    pct=BOSS_SURVIVOR_REWARD_PCT,
                    minutes=BOSS_SURVIVOR_REWARD_MINUTES,
                    source=f"boss survivor: {boss.get('display_name', 'raid boss')}",
                    reward_seed_xp=(BOSS_SURVIVOR_REWARD_PCT * 100.0) * max(1, int(BOSS_SURVIVOR_REWARD_MINUTES)),
                    persist=False,
                )
                record_game_fields(guild.id, member.id, "boss", survivor_awards=1)
        await save_data()
        await self._finish_boss(guild, boss, outcome="victory", reward_count=rewarded, killer=killer)

    async def _resolve_failure(self, guild: discord.Guild, boss: dict) -> None:
        punished = 0
        for uid, row in self._contributor_rows(boss):
            member = guild.get_member(uid)
            if member is None or member.bot:
                continue
            await grant_fixed_debuff(
                member,
                pct=BOSS_FAILURE_PENALTY_PCT,
                minutes=BOSS_FAILURE_PENALTY_MINUTES,
                source=f"boss failure: {boss.get('display_name', 'raid boss')}",
                reward_seed_xp=(BOSS_FAILURE_PENALTY_PCT * 100.0) * max(1, int(BOSS_FAILURE_PENALTY_MINUTES)),
                persist=False,
            )
            record_game_fields(guild.id, member.id, "boss", failure_penalties=1)
            punished += 1
        await save_data()
        await self._finish_boss(guild, boss, outcome="failure", reward_count=punished, killer=None)

    async def _resolve_idle_fade(self, guild: discord.Guild, boss: dict) -> None:
        await self._finish_boss(guild, boss, outcome="faded", reward_count=0, killer=None)

    def _summary_lines(
        self,
        guild: discord.Guild,
        boss: dict,
        *,
        outcome: str,
        reward_count: int,
        killer: Optional[discord.Member],
    ) -> list[str]:
        label = {
            "victory": "Defeated",
            "failure": "Escaped",
            "faded": "Faded Unchallenged",
        }.get(str(outcome or "").strip().lower(), "Resolved")
        contributors = self._contributor_rows(boss)
        participant_count = len(contributors)
        top_rows = sorted(
            contributors,
            key=lambda item: (
                -_as_int(item[1].get("damage", 0), 0),
                -_support_score(item[1]),
                item[0],
            ),
        )
        top_damage = self._top_damage_row(boss)
        top_support = self._top_support_row(boss)
        hist = _boss_history(_root_state(guild.id))

        engaged_at = _parse_iso(boss.get("engaged_at"))
        ended_at = _utcnow()
        engaged_duration = "Not engaged"
        engaged_seconds = 0
        if engaged_at is not None:
            engaged_seconds = max(0, int((ended_at - engaged_at).total_seconds()))
            engaged_duration = _fmt_duration_minutes(max(0, engaged_seconds // 60))

        lines = [
            "**Boss Summary**",
            f"**{boss.get('display_name', 'Unknown Boss')}** - **{label}**",
            f"Affix: **{boss.get('affix_name', 'Unknown')}** | Final phase: **{_phase_name(_as_int(boss.get('phase', 1), 1))}**",
            (
                f"HP: **{_fmt_num(boss.get('hp', 0))} / {_fmt_num(boss.get('max_hp', 0))}** left | "
                f"Damage dealt: **{_fmt_num(boss.get('total_damage', 0))}** | "
                f"Hits: **{_fmt_num(boss.get('hit_count', 0))}** / **{_fmt_num(boss.get('attack_count', 0))}**"
            ),
            (
                f"Participants: **{participant_count}** | "
                f"Downs: **{_fmt_num(boss.get('down_count', 0))}** | "
                f"Boss heals: **{_fmt_num(boss.get('heal_total', 0))}** | "
                f"Mechanics countered: **{_fmt_num(boss.get('mechanics_countered', 0))}** / **{_fmt_num(boss.get('mechanic_count', 0))}**"
            ),
            f"Fight duration: **{engaged_duration}**",
            (
                f"Guild raid record: **{_fmt_num(hist.get('kills', 0))}** kills | "
                f"Fastest win: **{_fmt_remaining(_as_int(hist.get('fastest_kill_seconds', 0), 0)) if _as_int(hist.get('fastest_kill_seconds', 0), 0) > 0 else 'N/A'}** | "
                f"Biggest hit: **{_fmt_num(hist.get('largest_hit', 0))}**"
            ),
        ]
        if killer is not None:
            lines.append(f"Final blow: **{killer.display_name}**")
        if _as_int(boss.get("best_hit", 0), 0) > 0:
            lines.append(
                f"Best hit this fight: **{_fmt_num(boss.get('best_hit', 0))}** by **{boss.get('best_hit_by', 'Unknown')}**"
            )
        if outcome == "victory":
            lines.append(
                f"Victory reward: **{reward_count}** contributor(s) received **+{_fmt_pct(BOSS_BASE_REWARD_PCT)} XP/min** "
                f"for **{_fmt_duration_minutes(int(BOSS_BASE_REWARD_MINUTES))}**."
            )
            lines.append(
                f"Commendations: MVP/support awards grant **+{_fmt_pct(BOSS_BONUS_REWARD_PCT)} XP/min** for **{_fmt_duration_minutes(int(BOSS_BONUS_REWARD_MINUTES))}**. "
                f"Flawless raiders get **+{_fmt_pct(BOSS_SURVIVOR_REWARD_PCT)} XP/min** for **{_fmt_duration_minutes(int(BOSS_SURVIVOR_REWARD_MINUTES))}**."
            )
        elif outcome == "failure":
            lines.append(
                f"Failure penalty: **{reward_count}** contributor(s) received **-{_fmt_pct(BOSS_FAILURE_PENALTY_PCT)} XP/min** "
                f"for **{_fmt_duration_minutes(int(BOSS_FAILURE_PENALTY_MINUTES))}**."
            )
        else:
            lines.append("Nobody engaged the boss before it dissolved.")

        if top_damage is not None:
            uid, row = top_damage
            member = guild.get_member(uid)
            name = member.display_name if member is not None else str(row.get("display_name", uid))
            lines.append(f"Top damage: **{name}** with **{_fmt_num(row.get('damage', 0))}** damage.")
        if top_support is not None:
            uid, row = top_support
            member = guild.get_member(uid)
            name = member.display_name if member is not None else str(row.get("display_name", uid))
            lines.append(
                f"Top support: **{name}** with **{_fmt_num(_support_score(row))}** support score "
                f"({row.get('resurrections', 0)} res, {row.get('guards', 0)} guard, {row.get('interrupts', 0)} interrupt, {row.get('cleanses', 0)} purge, {row.get('focuses', 0)} focus)."
            )

        if top_rows:
            lines.append("Top raiders:")
            for uid, row in top_rows[:5]:
                member = guild.get_member(uid)
                name = member.display_name if member is not None else str(row.get("display_name", uid))
                lines.append(
                    f"- **{name}**: **{_fmt_num(row.get('damage', 0))}** damage | "
                    f"**{_fmt_num(row.get('hits', 0))}/{_fmt_num(row.get('attacks', 0))}** hits | "
                    f"support **{_fmt_num(_support_score(row))}**"
                )
        return lines

    async def _finish_boss(
        self,
        guild: discord.Guild,
        boss: dict,
        *,
        outcome: str,
        reward_count: int,
        killer: Optional[discord.Member],
    ) -> None:
        st = _root_state(guild.id)
        hist = _boss_history(st)
        hist["support_actions"] = _as_int(hist.get("support_actions", 0), 0) + sum(
            _as_int(row.get("support_actions", 0), 0) for _, row in self._contributor_rows(boss)
        )
        best_hit = _as_int(boss.get("best_hit", 0), 0)
        if best_hit > _as_int(hist.get("largest_hit", 0), 0):
            hist["largest_hit"] = int(best_hit)
            hist["largest_hit_by"] = str(boss.get("best_hit_by", "")).strip()
            hist["largest_hit_boss"] = str(boss.get("display_name", "")).strip()
        if outcome == "victory":
            hist["kills"] = _as_int(hist.get("kills", 0), 0) + 1
            engaged_at = _parse_iso(boss.get("engaged_at"))
            if engaged_at is not None:
                elapsed = max(0, int((_utcnow() - engaged_at).total_seconds()))
                fastest = _as_int(hist.get("fastest_kill_seconds", 0), 0)
                if fastest <= 0 or elapsed < fastest:
                    hist["fastest_kill_seconds"] = int(elapsed)
        elif outcome == "failure":
            hist["failures"] = _as_int(hist.get("failures", 0), 0) + 1
        elif outcome == "faded":
            hist["fades"] = _as_int(hist.get("fades", 0), 0) + 1
        lines = self._summary_lines(guild, boss, outcome=outcome, reward_count=reward_count, killer=killer)
        await self._clear_active_boss(guild, boss)
        await self._announce_log(guild, "\n".join(lines))

    async def _perform_retaliation(
        self,
        guild: discord.Guild,
        boss: dict,
        attacker: discord.Member,
    ) -> str:
        rng = random.Random()
        now = _utcnow()
        now_ts = now.timestamp()
        row = _participant_row(boss, attacker)
        affix = _affix_data(boss)
        phase = max(1, _as_int(boss.get("phase", 1), 1))
        boss["retaliations"] = _as_int(boss.get("retaliations", 0), 0) + 1

        if _boss_is_stunned(boss, now):
            remaining = max(0, int((_parse_iso(boss.get("stunned_until")) - now).total_seconds()))
            return f"The boss reels from the interruption and cannot retaliate for **{_fmt_remaining(remaining)}**."

        weights = {
            "ashen_claw": 18,
            "grave_brand": 16,
            "iron_sentence": 12,
            "sundering_roar": 10,
            "black_tithe": 10,
            "sable_chain": 8,
            "grave_fall": 4 + max(0, phase - 2),
            "void_glare": 12,
        }
        bias = str(affix.get("mechanic_bias", "")).strip().lower()
        if bias == "guard":
            weights["sundering_roar"] += 4
        elif bias == "interrupt":
            weights["void_glare"] += 4
        elif bias == "cleanse":
            weights["grave_brand"] += 4
        action = rng.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]

        if action == "ashen_claw":
            extra = rng.randint(10, 18) + (phase * 2)
            if _consume_ward(row, now):
                return f"**{RETALIATION_NAMES[action]}** crashes into {attacker.mention}'s guard and splinters harmlessly."
            row["next_attack_ts"] = max(float(row.get("next_attack_ts", 0.0)), now_ts) + extra
            row["cooldown_extensions"] = _as_int(row.get("cooldown_extensions", 0), 0) + 1
            return (
                f"**{RETALIATION_NAMES[action]}** clips {attacker.mention}. Their next attack is delayed by **{_fmt_remaining(extra)}**."
            )

        if action == "grave_brand":
            if _consume_ward(row, now):
                return f"**{RETALIATION_NAMES[action]}** tries to brand {attacker.mention}, but their guard holds."
            duration = int(round(BOSS_MARK_DURATION_SECONDS * float(affix.get("mark_duration_mult", 1.0))))
            _set_mark(
                boss,
                attacker.id,
                kind="blight",
                name="Blight",
                source=RETALIATION_NAMES[action],
                now=now,
                duration_seconds=duration,
            )
            row["debuffs_taken"] = _as_int(row.get("debuffs_taken", 0), 0) + 1
            return (
                f"**{RETALIATION_NAMES[action]}** brands {attacker.mention}. Their next attacks lose **{_fmt_pct(BOSS_MARK_HIT_PENALTY_PCT)}** hit chance "
                f"and **{_fmt_pct(BOSS_MARK_DAMAGE_PENALTY_PCT)}** damage until cleansed or **{_fmt_remaining(duration)}** passes."
            )

        if action == "iron_sentence":
            extra = rng.randint(15, 24) + (phase * 2)
            if _consume_ward(row, now):
                return f"**{RETALIATION_NAMES[action]}** catches the guard instead of {attacker.mention}."
            row["next_attack_ts"] = max(float(row.get("next_attack_ts", 0.0)), now_ts) + extra
            row["next_support_ts"] = max(float(row.get("next_support_ts", 0.0)), now_ts) + max(10, extra - 5)
            row["cooldown_extensions"] = _as_int(row.get("cooldown_extensions", 0), 0) + 1
            return (
                f"**{RETALIATION_NAMES[action]}** pins {attacker.mention} in place. Attack and support actions are slowed for **{_fmt_remaining(extra)}**."
            )

        if action == "sundering_roar":
            extra = rng.randint(12, 20) + phase
            if _consume_ward(row, now):
                self._schedule_next_mechanic(boss, now, delay_seconds=20)
                return f"**{RETALIATION_NAMES[action]}** is absorbed by the guard, but the chamber still starts to rumble."
            row["next_attack_ts"] = max(float(row.get("next_attack_ts", 0.0)), now_ts) + extra
            row["cooldown_extensions"] = _as_int(row.get("cooldown_extensions", 0), 0) + 1
            self._schedule_next_mechanic(boss, now, delay_seconds=18)
            return (
                f"**{RETALIATION_NAMES[action]}** staggers {attacker.mention} and accelerates the next mechanic. Attack delayed **{_fmt_remaining(extra)}**."
            )

        if action == "black_tithe":
            max_hp = max(1, _as_int(boss.get("max_hp", 1), 1))
            heal = int(round(max_hp * (0.020 + (0.005 * max(0, phase - 1))) * float(affix.get("heal_mult", 1.0))))
            before = _as_int(boss.get("hp", 0), 0)
            boss["hp"] = min(max_hp, before + heal)
            healed = max(0, _as_int(boss.get("hp", 0), 0) - before)
            boss["heal_total"] = _as_int(boss.get("heal_total", 0), 0) + healed
            return f"**{RETALIATION_NAMES[action]}** feeds on the chaos and restores **{_fmt_num(healed)} HP**."

        if action == "sable_chain":
            ally = _pick_other_recent_attacker(guild, boss, attacker.id)
            if ally is None:
                self._schedule_next_mechanic(boss, now, delay_seconds=24)
                return f"**{RETALIATION_NAMES[action]}** scrapes the walls and drags the next mechanic closer."
            ally_row = _participant_row(boss, ally)
            extra = rng.randint(15, 26)
            if _consume_ward(ally_row, now):
                return f"**{RETALIATION_NAMES[action]}** lashes toward {ally.mention}, but their guard catches the chain."
            ally_row["next_attack_ts"] = max(float(ally_row.get("next_attack_ts", 0.0)), now_ts) + extra
            ally_row["cooldown_extensions"] = _as_int(ally_row.get("cooldown_extensions", 0), 0) + 1
            return f"**{RETALIATION_NAMES[action]}** catches {ally.mention}. Their next attack is delayed by **{_fmt_remaining(extra)}**."

        if action == "grave_fall":
            down_chance = min(0.18, 0.04 + (0.03 * max(0, phase - 2)))
            if _consume_ward(row, now):
                return f"**{RETALIATION_NAMES[action]}** would have dropped {attacker.mention}, but their guard keeps them standing."
            if rng.random() > down_chance:
                return f"**{RETALIATION_NAMES[action]}** nearly drops {attacker.mention}, but they cling to the fight."
            _down_member(boss, attacker, reason=RETALIATION_NAMES[action], now=now)
            row["downs_taken"] = _as_int(row.get("downs_taken", 0), 0) + 1
            boss["down_count"] = _as_int(boss.get("down_count", 0), 0) + 1
            return (
                f"**{RETALIATION_NAMES[action]}** downs {attacker.mention}. Another raider must use `{COMMAND_PREFIX}res @{attacker.display_name}` before they can act again."
            )

        self._schedule_next_mechanic(boss, now, delay_seconds=18)
        return f"**{RETALIATION_NAMES[action]}** fixes on the raid and hastens the next telegraphed mechanic."

    async def _maybe_spawn_scheduled_boss(self, guild: discord.Guild) -> None:
        if not BOSS_ENABLED:
            return
        st = _root_state(guild.id)
        if _current_boss(st):
            return

        next_spawn = _next_spawn_at(st)
        if next_spawn is None:
            _schedule_next_spawn(st, guild.id)
            await save_data()
            return

        if _utcnow() < next_spawn:
            return

        await self._spawn_boss(guild, forced=False)

    async def _maintain_current_boss(self, guild: discord.Guild, boss: dict) -> None:
        previous_channel_id = _as_int(boss.get("channel_id", 0), 0)
        channel = await self._ensure_boss_channel(guild, boss)
        if channel is not None and previous_channel_id != _as_int(boss.get("channel_id", 0), 0):
            await save_data()
        now = _utcnow()
        status = str(boss.get("status", "idle")).strip().lower()
        runtime_changed = await self._refresh_boss_runtime(guild, boss, channel)
        if runtime_changed:
            await save_data()

        if status == "idle":
            idle_expires_at = _parse_iso(boss.get("idle_expires_at"))
            if idle_expires_at is not None and now >= idle_expires_at:
                await self._resolve_idle_fade(guild, boss)
                return
            return

        if status != "active":
            return

        expires_at = _parse_iso(boss.get("expires_at"))
        if expires_at is not None and now >= expires_at:
            await self._resolve_failure(guild, boss)

    def _live_channel(self, guild: discord.Guild, boss: dict) -> Optional[discord.TextChannel]:
        channel = guild.get_channel(_as_int(boss.get("channel_id", 0), 0))
        return channel if isinstance(channel, discord.TextChannel) else None

    def _status_lines(self, guild: discord.Guild, boss: dict) -> list[str]:
        now = _utcnow()
        lines = [
            f"**{boss.get('display_name', 'Unknown Boss')}**",
            f"HP: **{_fmt_num(boss.get('hp', 0))} / {_fmt_num(boss.get('max_hp', 0))}**",
            (
                f"Boss prestige: **{_as_int(boss.get('boss_prestige', 0), 0)}** | "
                f"Tuned raid size: **{_as_int(boss.get('target_fighters', 1), 1)}** fighter(s)"
            ),
            (
                f"Affix: **{boss.get('affix_name', 'Unknown')}** | "
                f"Phase: **{_phase_name(_as_int(boss.get('phase', 1), 1))}** | "
                f"Target clear pace: **{_fmt_duration_minutes(_as_int(boss.get('target_clear_minutes', BOSS_TARGET_CLEAR_MINUTES), BOSS_TARGET_CLEAR_MINUTES))}**"
            ),
        ]
        downed_count = len(_as_dict(boss.get("downed")))
        if downed_count > 0:
            lines.append(f"Downed raiders: **{downed_count}**")
        marked_count = len(_player_marks(boss))
        if marked_count > 0:
            lines.append(f"Marked raiders: **{marked_count}**")

        status = str(boss.get("status", "idle")).strip().lower()
        if status == "idle":
            idle_expires_at = _parse_iso(boss.get("idle_expires_at"))
            if idle_expires_at is not None:
                remaining = max(0, int((idle_expires_at - now).total_seconds()))
                lines.append(
                    f"Status: **Idle**. First `{COMMAND_PREFIX}attack` starts the timer. "
                    f"Idle window left: **{_fmt_remaining(remaining)}**."
                )
            else:
                lines.append(f"Status: **Idle**. First `{COMMAND_PREFIX}attack` starts the timer.")
        else:
            expires_at = _parse_iso(boss.get("expires_at"))
            if expires_at is not None:
                remaining = max(0, int((expires_at - now).total_seconds()))
                lines.append(f"Status: **Active**. Time left: **{_fmt_remaining(remaining)}**.")
            else:
                lines.append("Status: **Active**.")
            if _boss_is_stunned(boss, now):
                stunned_until = _parse_iso(boss.get("stunned_until"))
                stunned_for = max(0, int((stunned_until - now).total_seconds())) if stunned_until is not None else 0
                lines.append(f"Boss staggered: **{_fmt_remaining(stunned_for)}**.")
            exposed_bonus = _boss_exposed_bonus(boss, now)
            if exposed_bonus > 0.0:
                exposed_until = _parse_iso(boss.get("exposed_until"))
                exposed_for = max(0, int((exposed_until - now).total_seconds())) if exposed_until is not None else 0
                lines.append(
                    f"Boss exposed: **+{_fmt_pct(exposed_bonus)} damage** for **{_fmt_remaining(exposed_for)}**."
                )
            mechanic_line = self._pending_mechanic_line(boss, now)
            if mechanic_line:
                lines.append(mechanic_line)

        top_damage = self._top_damage_row(boss)
        if top_damage is not None:
            uid, row = top_damage
            member = guild.get_member(uid)
            name = member.display_name if member is not None else str(row.get("display_name", uid))
            lines.append(f"Raid lead: **{name}** with **{_fmt_num(row.get('damage', 0))}** damage.")

        top_support = self._top_support_row(boss)
        if top_support is not None:
            uid, row = top_support
            member = guild.get_member(uid)
            name = member.display_name if member is not None else str(row.get("display_name", uid))
            lines.append(f"Support lead: **{name}** with **{_fmt_num(_support_score(row))}** support score.")

        channel = self._live_channel(guild, boss)
        if channel is not None:
            lines.append(f"Fight here: {channel.mention}")
        return lines

    async def _run_attack(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        attacker: discord.Member,
        *,
        reference: Optional[discord.Message] = None,
    ) -> None:
        st = _root_state(guild.id)
        boss = _current_boss(st)
        if not boss:
            await self._send_boss_message(channel, boss, "There is no raid boss to attack right now.", reference=reference)
            return

        runtime_changed = await self._refresh_boss_runtime(guild, boss, channel)
        if runtime_changed:
            await save_data()
        boss = _current_boss(_root_state(guild.id))
        if not boss:
            await self._send_boss_message(channel, boss, "The boss has already been resolved.", reference=reference)
            return

        if _is_downed(boss, attacker.id):
            await self._send_boss_message(
                channel,
                boss,
                (
                    f"You are downed. Another raider must use `{COMMAND_PREFIX}res {attacker.mention}` "
                    "before you can attack again."
                ),
                reference=reference,
            )
            return

        row = _participant_row(boss, attacker)
        now = _utcnow()
        now_ts = now.timestamp()
        next_attack_ts = float(row.get("next_attack_ts", 0.0) or 0.0)
        if now_ts < next_attack_ts:
            await self._send_boss_message(
                channel,
                boss,
                f"You are recovering. Attack again in **{_fmt_remaining(int(next_attack_ts - now_ts))}**.",
                reference=reference,
            )
            return

        if str(boss.get("status", "idle")).strip().lower() == "idle":
            boss["status"] = "active"
            boss["engaged_at"] = _iso(now)
            boss["expires_at"] = _iso(now + timedelta(minutes=int(BOSS_ACTIVE_DURATION_MINUTES)))
            self._schedule_next_mechanic(boss, now, delay_seconds=random.randint(50, 70))

        prestige = _member_prestige(guild.id, attacker.id)
        chance = _hit_chance(boss, prestige)
        rng = random.Random()
        focus_damage_bonus, focus_hit_bonus = _focus_bonus(row, now)
        mark = _as_dict(_player_marks(boss).get(str(attacker.id)))
        mark_active = bool(mark and ((_parse_iso(mark.get("expires_at")) or now) > now))
        if mark_active:
            chance -= float(BOSS_MARK_HIT_PENALTY_PCT)
        chance += float(focus_hit_bonus)
        chance = max(0.25, min(1.0, chance))
        landed = rng.random() <= chance

        row["display_name"] = attacker.display_name
        row["attacks"] = _as_int(row.get("attacks", 0), 0) + 1
        row["last_attack_at"] = _iso(now)
        if not str(row.get("first_attack_at", "")).strip():
            row["first_attack_at"] = _iso(now)
        row["next_attack_ts"] = now_ts + _attack_cooldown_seconds(boss)
        _push_recent_attacker(boss, attacker.id)

        boss["attack_count"] = _as_int(boss.get("attack_count", 0), 0) + 1

        attack_lines = [f"**{boss.get('display_name', 'Unknown Boss')}** lashes back at the raid."]
        if landed:
            damage, crit = _roll_damage(rng, prestige, boss)
            damage_mult = 1.0 + float(focus_damage_bonus) + float(_boss_exposed_bonus(boss, now))
            if mark_active:
                damage_mult = max(0.35, damage_mult - float(BOSS_MARK_DAMAGE_PENALTY_PCT))
            damage = max(1, int(round(damage * max(0.35, damage_mult))))
            damage = min(damage, max(0, _as_int(boss.get("hp", 0), 0)))
            boss["hp"] = max(0, _as_int(boss.get("hp", 0), 0) - damage)
            boss["hit_count"] = _as_int(boss.get("hit_count", 0), 0) + 1
            boss["total_damage"] = _as_int(boss.get("total_damage", 0), 0) + damage
            row["hits"] = _as_int(row.get("hits", 0), 0) + 1
            row["damage"] = _as_int(row.get("damage", 0), 0) + damage
            if damage > _as_int(boss.get("best_hit", 0), 0):
                boss["best_hit"] = int(damage)
                boss["best_hit_by"] = str(attacker.display_name)
            record_game_fields(
                guild.id,
                attacker.id,
                "boss",
                attacks=1,
                hits=1,
                damage_total=damage,
            )
            rider_bits: list[str] = []
            if crit:
                rider_bits.append("crit")
            if focus_damage_bonus > 0.0 or focus_hit_bonus > 0.0:
                rider_bits.append("focused")
            exposed_bonus = _boss_exposed_bonus(boss, now)
            if exposed_bonus > 0.0:
                rider_bits.append("exposed")
            if mark_active:
                rider_bits.append("blighted")
            attack_lines.append(
                f"{attacker.mention} strikes true at **{chance * 100.0:.1f}%** odds for **{_fmt_num(damage)}** damage"
                + (f" ({', '.join(rider_bits)})." if rider_bits else ".")
            )
        else:
            row["misses"] = _as_int(row.get("misses", 0), 0) + 1
            record_game_fields(guild.id, attacker.id, "boss", attacks=1, misses=1)
            attack_lines.append(
                f"{attacker.mention} misses at **{chance * 100.0:.1f}%** odds. The boss barely shifts."
            )
        if focus_damage_bonus > 0.0 or focus_hit_bonus > 0.0:
            _consume_focus(row)

        await self._sync_channel_name(guild, boss)

        if _as_int(boss.get("hp", 0), 0) <= 0:
            await save_data()
            await self._send_boss_message(
                channel,
                boss,
                "\n".join(
                    attack_lines
                    + [
                        f"HP left: **0 / {_fmt_num(boss.get('max_hp', 0))}**.",
                        "The boss collapses. Closing the chamber and posting the summary in `paragon-log`.",
                    ]
                ),
                reference=reference,
            )
            await self._resolve_victory(guild, boss, killer=attacker)
            return

        await self._maybe_handle_phase_transition(guild, boss, channel, now=now)

        retaliation_line = "The boss lashes out, but I hit an internal error resolving the retaliation."
        try:
            retaliation_line = await self._perform_retaliation(guild, boss, attacker)
        except Exception:
            pass
        await self._sync_channel_name(guild, boss)
        await save_data()

        expires_at = _parse_iso(boss.get("expires_at"))
        remaining_line = ""
        if expires_at is not None:
            remaining_line = f" Time left: **{_fmt_remaining(max(0, int((expires_at - _utcnow()).total_seconds())))}**."
        mechanic_line = self._pending_mechanic_line(boss, _utcnow())
        await self._send_boss_message(
            channel,
            boss,
            "\n".join(
                [
                    *attack_lines,
                    f"HP left: **{_fmt_num(boss.get('hp', 0))} / {_fmt_num(boss.get('max_hp', 0))}**.{remaining_line}",
                    retaliation_line,
                ]
                + ([mechanic_line] if mechanic_line else [])
            ),
            reference=reference,
        )

    async def _run_resurrection(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        member: discord.Member,
        *,
        target: Optional[discord.Member] = None,
        reference: Optional[discord.Message] = None,
    ) -> None:
        st = _root_state(guild.id)
        boss = _current_boss(st)
        if not boss:
            await self._send_boss_message(channel, boss, "There is no raid boss active right now.", reference=reference)
            return

        runtime_changed = await self._refresh_boss_runtime(guild, boss, channel)
        if runtime_changed:
            await save_data()
        boss = _current_boss(_root_state(guild.id))
        if not boss:
            await self._send_boss_message(channel, boss, "The boss has already been resolved.", reference=reference)
            return

        if _is_downed(boss, member.id):
            await self._send_boss_message(
                channel,
                boss,
                "You are downed and cannot resurrect anyone until another raider revives you.",
                reference=reference,
            )
            return

        if target is None:
            target = _pick_downed_target(guild, boss)
            if target is None:
                await self._send_boss_message(channel, boss, "No one is downed right now.", reference=reference)
                return

        if target.bot:
            await self._send_boss_message(channel, boss, "Bots do not need resurrection.", reference=reference)
            return
        if target.id == member.id:
            await self._send_boss_message(channel, boss, "You cannot resurrect yourself.", reference=reference)
            return
        if not _is_downed(boss, target.id):
            await self._send_boss_message(
                channel,
                boss,
                f"{target.display_name} is not downed.",
                reference=reference,
            )
            return

        row = _participant_row(boss, member)
        now_ts = _utcnow().timestamp()
        next_res_ts = float(row.get("next_res_ts", 0.0) or 0.0)
        if now_ts < next_res_ts:
            await self._send_boss_message(
                channel,
                boss,
                (
                    "You are still recovering from your last rescue. "
                    f"Try again in **{_fmt_remaining(int(next_res_ts - now_ts))}**."
                ),
                reference=reference,
            )
            return

        revived = _revive_member(boss, target.id)
        if not revived:
            await self._send_boss_message(
                channel,
                boss,
                f"{target.display_name} is no longer downed.",
                reference=reference,
            )
            return

        row["resurrections"] = _as_int(row.get("resurrections", 0), 0) + 1
        row["support_actions"] = _as_int(row.get("support_actions", 0), 0) + 1
        row["next_res_ts"] = now_ts + _res_cooldown_seconds(boss)
        target_row = _participant_row(boss, target)
        target_row["next_attack_ts"] = max(float(target_row.get("next_attack_ts", 0.0)), now_ts + 5.0)
        record_game_fields(guild.id, member.id, "boss", resurrections=1, support_actions=1)
        await save_data()
        await self._send_boss_message(
            channel,
            boss,
            (
                f"{member.mention} hauls {target.mention} back into the fight. "
                f"{target.display_name} can attack again in a few seconds."
            ),
            reference=reference,
        )

    def _pick_marked_target(self, guild: discord.Guild, boss: dict) -> Optional[discord.Member]:
        rows: list[tuple[datetime, int]] = []
        for uid_s, raw in _player_marks(boss).items():
            uid = _as_int(uid_s, 0)
            row = _as_dict(raw)
            expires_at = _parse_iso(row.get("expires_at")) or datetime.max.replace(tzinfo=timezone.utc)
            if uid <= 0:
                continue
            rows.append((expires_at, uid))
        rows.sort(key=lambda item: (item[0], item[1]))
        for _, uid in rows:
            member = guild.get_member(uid)
            if member is not None and not member.bot:
                return member
        return None

    async def _run_support_action(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        member: discord.Member,
        *,
        action: str,
        target: Optional[discord.Member] = None,
        reference: Optional[discord.Message] = None,
    ) -> None:
        st = _root_state(guild.id)
        boss = _current_boss(st)
        if not boss:
            await self._send_boss_message(channel, boss, "There is no raid boss active right now.", reference=reference)
            return

        runtime_changed = await self._refresh_boss_runtime(guild, boss, channel)
        if runtime_changed:
            await save_data()
        boss = _current_boss(_root_state(guild.id))
        if not boss:
            await self._send_boss_message(channel, boss, "The boss has already been resolved.", reference=reference)
            return

        if _is_downed(boss, member.id):
            await self._send_boss_message(
                channel,
                boss,
                "You are downed. Another raider must revive you before you can use support actions.",
                reference=reference,
            )
            return

        if str(boss.get("status", "idle")).strip().lower() != "active":
            await self._send_boss_message(
                channel,
                boss,
                f"The boss is still dormant. Start the fight with `{COMMAND_PREFIX}attack` first.",
                reference=reference,
            )
            return

        action = str(action or "").strip().lower()
        row = _participant_row(boss, member)
        now = _utcnow()
        now_ts = now.timestamp()
        next_support_ts = float(row.get("next_support_ts", 0.0) or 0.0)
        if now_ts < next_support_ts:
            await self._send_boss_message(
                channel,
                boss,
                f"You are still recovering. Support again in **{_fmt_remaining(int(next_support_ts - now_ts))}**.",
                reference=reference,
            )
            return

        pending = _pending_mechanic(boss)
        pending_counter = str(pending.get("counter", "")).strip().lower()

        if action == "guard":
            counted = self._register_mechanic_response(boss, member, "guard")
            _grant_ward(row, now)
            row["guards"] = _as_int(row.get("guards", 0), 0) + 1
            row["support_actions"] = _as_int(row.get("support_actions", 0), 0) + 1
            row["next_support_ts"] = now_ts + _support_cooldown_seconds(boss)
            record_game_fields(guild.id, member.id, "boss", guards=1, support_actions=1)
            await save_data()
            msg = (
                f"{member.mention} braces the line. Guard active for **{_fmt_remaining(BOSS_GUARD_DURATION_SECONDS)}**."
            )
            if counted:
                msg += (
                    f" Mechanic progress: **{_pending_response_count(boss)} / "
                    f"{_as_int(_pending_mechanic(boss).get('required', 1), 1)}**."
                )
            await self._send_boss_message(channel, boss, msg, reference=reference)
            return

        if action == "interrupt":
            if pending_counter != "interrupt":
                await self._send_boss_message(channel, boss, "Nothing is channeling right now. Save `!interrupt` for a telegraphed cast.", reference=reference)
                return
            counted = self._register_mechanic_response(boss, member, "interrupt")
            if not counted:
                await self._send_boss_message(channel, boss, "You have already committed your interrupt to this cast.", reference=reference)
                return
            row["interrupts"] = _as_int(row.get("interrupts", 0), 0) + 1
            row["support_actions"] = _as_int(row.get("support_actions", 0), 0) + 1
            row["next_support_ts"] = now_ts + _support_cooldown_seconds(boss)
            record_game_fields(guild.id, member.id, "boss", interrupts=1, support_actions=1)
            await save_data()
            await self._send_boss_message(
                channel,
                boss,
                (
                    f"{member.mention} commits the interrupt. Progress: **{_pending_response_count(boss)} / "
                    f"{_as_int(_pending_mechanic(boss).get('required', 1), 1)}**."
                ),
                reference=reference,
            )
            return

        if action == "purge":
            if target is None:
                target = self._pick_marked_target(guild, boss)
            if target is not None and target.bot:
                await self._send_boss_message(channel, boss, "Bots do not need a purge.", reference=reference)
                return
            cleared = False
            if target is not None:
                cleared = _clear_mark(boss, target.id)
            counted = False
            if pending_counter == "purge":
                counted = self._register_mechanic_response(boss, member, "purge")
            if not cleared and not counted:
                await self._send_boss_message(channel, boss, "No blight is active right now, and there is no purge mechanic to answer.", reference=reference)
                return
            row["cleanses"] = _as_int(row.get("cleanses", 0), 0) + 1
            row["support_actions"] = _as_int(row.get("support_actions", 0), 0) + 1
            row["next_support_ts"] = now_ts + _support_cooldown_seconds(boss)
            fields: dict[str, int] = {"cleanses": 1, "support_actions": 1}
            if cleared:
                row["marks_cleansed"] = _as_int(row.get("marks_cleansed", 0), 0) + 1
                fields["marks_cleansed"] = 1
            record_game_fields(guild.id, member.id, "boss", **fields)
            await save_data()
            bits: list[str] = [f"{member.mention} purges the spreading rot."]
            if cleared and target is not None:
                bits.append(f"Cleared **Blight** from {target.mention}.")
            if counted:
                bits.append(
                    f"Mechanic progress: **{_pending_response_count(boss)} / "
                    f"{_as_int(_pending_mechanic(boss).get('required', 1), 1)}**."
                )
            await self._send_boss_message(channel, boss, " ".join(bits), reference=reference)
            return

        if action == "focus":
            if target is None:
                target = member
            if target.bot:
                await self._send_boss_message(channel, boss, "Bots cannot be focused.", reference=reference)
                return
            if _is_downed(boss, target.id):
                await self._send_boss_message(channel, boss, f"{target.display_name} is downed and cannot be focused right now.", reference=reference)
                return
            target_row = _participant_row(boss, target)
            damage_bonus = float(BOSS_FOCUS_DAMAGE_BONUS_PCT) + float(_affix_data(boss).get("focus_bonus_pct", 0.0))
            _grant_focus(
                target_row,
                now,
                damage_bonus_pct=damage_bonus,
                hit_bonus_pct=float(BOSS_FOCUS_HIT_BONUS_PCT),
            )
            row["focuses"] = _as_int(row.get("focuses", 0), 0) + 1
            row["support_actions"] = _as_int(row.get("support_actions", 0), 0) + 1
            row["next_support_ts"] = now_ts + _support_cooldown_seconds(boss)
            record_game_fields(guild.id, member.id, "boss", focuses=1, support_actions=1)
            await save_data()
            await self._send_boss_message(
                channel,
                boss,
                (
                    f"{member.mention} calls the shot for {target.mention}. "
                    f"Their next attack within **{_fmt_remaining(BOSS_FOCUS_DURATION_SECONDS)}** gains **+{_fmt_pct(damage_bonus)} damage** "
                    f"and **+{_fmt_pct(BOSS_FOCUS_HIT_BONUS_PCT)}** hit chance."
                ),
                reference=reference,
            )
            return

        await self._send_boss_message(channel, boss, "That support action is not recognized.", reference=reference)

    @tasks.loop(seconds=10)
    async def boss_loop(self):
        for guild in list(self.bot.guilds):
            st = _root_state(guild.id)
            boss = _current_boss(st)
            if boss:
                await self._maintain_current_boss(guild, boss)
            else:
                await self._maybe_spawn_scheduled_boss(guild)

    @boss_loop.before_loop
    async def _before_boss_loop(self):
        await self.bot.wait_until_ready()

    @commands.command(name="boss", aliases=["raid"])
    async def boss(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        st = _root_state(ctx.guild.id)
        hist = _boss_history(st)
        boss = _current_boss(st)
        if not boss:
            next_spawn = _next_spawn_at(st)
            if next_spawn is None:
                next_spawn = _schedule_next_spawn(st, ctx.guild.id)
                await save_data()
            lines = [
                f"There is no active raid boss right now. The next random spawn is scheduled for **{_fmt_local_spawn(next_spawn)}**.",
                (
                    f"Guild raid record: **{_fmt_num(hist.get('kills', 0))}** kills | "
                    f"Fastest win: **{_fmt_remaining(_as_int(hist.get('fastest_kill_seconds', 0), 0)) if _as_int(hist.get('fastest_kill_seconds', 0), 0) > 0 else 'N/A'}** | "
                    f"Biggest hit: **{_fmt_num(hist.get('largest_hit', 0))}**"
                ),
            ]
            if str(hist.get("largest_hit_by", "")).strip():
                lines.append(f"Record holder: **{hist.get('largest_hit_by', 'Unknown')}**")
            await ctx.reply("\n".join(lines))
            return
        channel = self._live_channel(ctx.guild, boss)
        if channel is not None:
            runtime_changed = await self._refresh_boss_runtime(ctx.guild, boss, channel)
            if runtime_changed:
                await save_data()
            boss = _current_boss(_root_state(ctx.guild.id))
            channel = self._live_channel(ctx.guild, boss)
        channel = self._live_channel(ctx.guild, boss)
        if channel is not None and ctx.channel.id == channel.id:
            await self._send_boss_message(channel, boss, "\n".join(self._status_lines(ctx.guild, boss)), reference=ctx.message)
            return
        await ctx.reply("\n".join(self._status_lines(ctx.guild, boss)))

    @commands.command(name="attack", aliases=["atk"])
    async def attack(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if ctx.author.bot:
            return

        st = _root_state(ctx.guild.id)
        boss = _current_boss(st)
        if not boss:
            await ctx.reply("There is no raid boss to attack right now.")
            return

        channel = self._live_channel(ctx.guild, boss)
        if channel is None:
            previous_channel_id = _as_int(boss.get("channel_id", 0), 0)
            channel = await self._ensure_boss_channel(ctx.guild, boss)
            if channel is not None and previous_channel_id != _as_int(boss.get("channel_id", 0), 0):
                await save_data()
        if channel is None:
            await ctx.reply("The boss chamber is unavailable right now. Try again in a moment.")
            return
        if ctx.channel.id != channel.id:
            await ctx.reply(f"Use `{ctx.clean_prefix}attack` in {channel.mention}.")
            return
        await self._run_attack(ctx.guild, channel, ctx.author, reference=ctx.message)

    @commands.command(name="resurrect", aliases=["res"])
    async def resurrect(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if ctx.author.bot:
            return

        st = _root_state(ctx.guild.id)
        boss = _current_boss(st)
        if not boss:
            await ctx.reply("There is no raid boss active right now.")
            return

        channel = self._live_channel(ctx.guild, boss)
        if channel is None:
            await ctx.reply("The boss chamber is unavailable right now.")
            return
        if ctx.channel.id != channel.id:
            await ctx.reply(f"Use `{ctx.clean_prefix}res` in {channel.mention}.")
            return
        await self._run_resurrection(ctx.guild, channel, ctx.author, target=target, reference=ctx.message)

    @commands.command(name="guard")
    async def guard(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if ctx.author.bot:
            return

        st = _root_state(ctx.guild.id)
        boss = _current_boss(st)
        if not boss:
            await ctx.reply("There is no raid boss active right now.")
            return

        channel = self._live_channel(ctx.guild, boss)
        if channel is None:
            await ctx.reply("The boss chamber is unavailable right now.")
            return
        if ctx.channel.id != channel.id:
            await ctx.reply(f"Use `{ctx.clean_prefix}guard` in {channel.mention}.")
            return
        await self._run_support_action(ctx.guild, channel, ctx.author, action="guard", reference=ctx.message)

    @commands.command(name="interrupt")
    async def interrupt(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if ctx.author.bot:
            return

        st = _root_state(ctx.guild.id)
        boss = _current_boss(st)
        if not boss:
            await ctx.reply("There is no raid boss active right now.")
            return

        channel = self._live_channel(ctx.guild, boss)
        if channel is None:
            await ctx.reply("The boss chamber is unavailable right now.")
            return
        if ctx.channel.id != channel.id:
            await ctx.reply(f"Use `{ctx.clean_prefix}interrupt` in {channel.mention}.")
            return
        await self._run_support_action(ctx.guild, channel, ctx.author, action="interrupt", reference=ctx.message)

    @commands.command(name="purge", aliases=["raidpurge"])
    async def purge(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if ctx.author.bot:
            return

        st = _root_state(ctx.guild.id)
        boss = _current_boss(st)
        if not boss:
            await ctx.reply("There is no raid boss active right now.")
            return

        channel = self._live_channel(ctx.guild, boss)
        if channel is None:
            await ctx.reply("The boss chamber is unavailable right now.")
            return
        if ctx.channel.id != channel.id:
            await ctx.reply(f"Use `{ctx.clean_prefix}purge` in {channel.mention}.")
            return
        await self._run_support_action(ctx.guild, channel, ctx.author, action="purge", target=target, reference=ctx.message)

    @commands.command(name="focus")
    async def focus(self, ctx: commands.Context, target: Optional[discord.Member] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if ctx.author.bot:
            return

        st = _root_state(ctx.guild.id)
        boss = _current_boss(st)
        if not boss:
            await ctx.reply("There is no raid boss active right now.")
            return

        channel = self._live_channel(ctx.guild, boss)
        if channel is None:
            await ctx.reply("The boss chamber is unavailable right now.")
            return
        if ctx.channel.id != channel.id:
            await ctx.reply(f"Use `{ctx.clean_prefix}focus` in {channel.mention}.")
            return
        await self._run_support_action(ctx.guild, channel, ctx.author, action="focus", target=target, reference=ctx.message)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        guild = self.bot.get_guild(payload.guild_id or 0)
        if guild is None:
            return
        st = _root_state(guild.id)
        boss = _current_boss(st)
        if not boss:
            return
        channel = self._live_channel(guild, boss)
        if not isinstance(channel, discord.TextChannel):
            return
        if payload.channel_id != channel.id:
            return
        if not _is_active_control_message(boss, payload.message_id):
            return

        member = payload.member if isinstance(payload.member, discord.Member) else guild.get_member(payload.user_id)
        if member is None or member.bot:
            return

        emoji_name = str(payload.emoji).replace("\ufe0f", "")
        emoji_alias = str(payload.emoji.name or "").replace("\ufe0f", "")
        await self._remove_user_reaction(channel, payload.message_id, payload.emoji, member)

        if emoji_name in BOSS_REACTION_ATTACK_NAMES or emoji_alias in BOSS_REACTION_ATTACK_NAMES:
            await self._run_attack(guild, channel, member)
            return
        if emoji_name in BOSS_REACTION_RES_NAMES or emoji_alias in BOSS_REACTION_RES_NAMES:
            await self._run_resurrection(guild, channel, member)

    @commands.command(name="spawnboss", aliases=["bossnow"])
    @owner_only()
    async def spawnboss(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        st = _root_state(ctx.guild.id)
        boss = _current_boss(st)
        if boss:
            channel = self._live_channel(ctx.guild, boss)
            if channel is not None:
                await ctx.reply(f"There is already a boss active in {channel.mention}.")
            else:
                await ctx.reply("There is already a boss active.")
            return
        spawned = await self._spawn_boss(ctx.guild, forced=True)
        if not spawned:
            await ctx.reply("I couldn't spawn a boss right now. Check my channel permissions.")
            return
        boss = _current_boss(_root_state(ctx.guild.id))
        channel = self._live_channel(ctx.guild, boss)
        if channel is not None:
            await ctx.reply(f"Spawned a boss in {channel.mention}.")
            return
        await ctx.reply("Spawned a boss.")

    @commands.command(name="clearboss")
    @owner_only()
    async def clearboss(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        st = _root_state(ctx.guild.id)
        boss = _current_boss(st)
        if not boss:
            await ctx.reply("There is no raid boss active right now.")
            return

        boss_name = str(boss.get("display_name", "the current boss"))
        channel = self._live_channel(ctx.guild, boss)
        same_channel = channel is not None and ctx.channel.id == channel.id

        if same_channel:
            try:
                await ctx.reply(
                    f"Clearing **{boss_name}** now. No rewards, penalties, or boss stats will be applied."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            self._rollback_boss_stats(ctx.guild.id, boss)
            await self._clear_active_boss(ctx.guild, boss)
            return

        self._rollback_boss_stats(ctx.guild.id, boss)
        await self._clear_active_boss(ctx.guild, boss)
        if channel is not None:
            await ctx.reply(
                f"Cleared **{boss_name}** from {channel.mention}. No rewards, penalties, or boss stats were applied."
            )
            return
        await ctx.reply(
            f"Cleared **{boss_name}**. No rewards, penalties, or boss stats were applied."
        )
