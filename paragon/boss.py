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
    BOSS_DAMAGE_MAX,
    BOSS_DAMAGE_MIN,
    BOSS_DAMAGE_PRESTIGE_STEP,
    BOSS_DURATION_MAX_MINUTES,
    BOSS_DURATION_MIN_MINUTES,
    BOSS_ENABLED,
    BOSS_FAILURE_DEBUFF_MINUTES,
    BOSS_FAILURE_DEBUFF_PCT,
    BOSS_HP_BASE,
    BOSS_HP_PER_BOSS_PRESTIGE,
    BOSS_HP_PER_TARGET_FIGHTER,
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
from .guild_setup import ensure_guild_setup, get_log_channel
from .ownership import owner_only
from .stats_store import record_game_fields
from .storage import _gdict, _udict, save_data
from .xp import grant_fixed_boost, grant_fixed_debuff, grant_stacked_fixed_debuff

BOSS_STATE_KEY = "boss"
BOSS_ATTACKER_LIMIT = 5

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
    return st


def _current_boss(st: dict) -> dict:
    cur = _as_dict(st.get("current"))
    if _as_int(cur.get("event_id", 0), 0) <= 0:
        st["current"] = {}
        return {}
    cur.setdefault("attackers", {})
    cur.setdefault("recent_attackers", [])
    cur["attackers"] = _as_dict(cur.get("attackers"))
    cur["recent_attackers"] = [int(uid) for uid in _as_list(cur.get("recent_attackers")) if _as_int(uid, 0) > 0]
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
    member_count = max(1, len(members))
    divisor = max(1, int(BOSS_TARGET_MEMBER_DIVISOR))
    target_fighters = max(1, (member_count + divisor - 1) // divisor)

    prestiges = [_member_prestige(guild.id, member.id) for member in members]
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

    hp = (
        int(BOSS_HP_BASE)
        + (target_fighters * int(BOSS_HP_PER_TARGET_FIGHTER))
        + (boss_prestige * int(BOSS_HP_PER_BOSS_PRESTIGE))
    )
    return {
        "member_count": int(member_count),
        "target_fighters": int(target_fighters),
        "avg_prestige": float(avg_prestige),
        "min_prestige": int(min_prestige),
        "max_prestige": int(max_prestige),
        "boss_prestige": int(boss_prestige),
        "hp": max(1, int(hp)),
    }


def _spawn_at_local(guild_id: int, spawn_date: date, *, salt: str = "") -> datetime:
    start_hour = max(0, min(23, int(BOSS_SPAWN_START_HOUR)))
    end_hour = max(start_hour + 1, min(24, int(BOSS_SPAWN_END_HOUR)))
    total_minutes = max(1, ((end_hour - start_hour) * 60))
    seed = hashlib.sha256(f"boss:{guild_id}:{spawn_date.isoformat()}:{salt}".encode("utf-8")).digest()
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
    min_days = max(1, int(BOSS_SPAWN_MIN_DAYS))
    max_days = max(min_days, int(BOSS_SPAWN_MAX_DAYS))
    rng = random.SystemRandom()
    day_offset = rng.randint(min_days, max_days)
    spawn_date = now_local.date() + timedelta(days=day_offset)
    salt = f"{now_local.date().isoformat()}:{rng.randint(0, 1_000_000)}"
    spawn_local = _spawn_at_local(guild_id, spawn_date, salt=salt)
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
    hp = max(0, _as_int(boss.get("hp", 0), 0))
    slug = _slugify(str(boss.get("display_name", boss.get("slug", "boss"))), max_length=78)
    name = f"{hp}-hp-{slug}"
    if len(name) <= 100:
        return name
    return name[:100].rstrip("-")


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
    row.setdefault("first_attack_at", "")
    row.setdefault("last_attack_at", "")
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
    damage += min(2, max(0, int(prestige)) // step)

    crit_bonus = False
    boss_prestige = max(0, _as_int(boss.get("boss_prestige", 0), 0))
    crit_chance = 0.08
    if prestige >= boss_prestige:
        crit_chance = 0.18
    elif prestige >= max(1, boss_prestige // 2):
        crit_chance = 0.12
    if rng.random() < crit_chance:
        damage += 2
        crit_bonus = True
    return max(1, int(damage)), crit_bonus


async def _timeout_member(member: discord.Member, seconds: int, reason: str) -> bool:
    until = _utcnow() + timedelta(seconds=max(1, int(seconds)))
    try:
        if hasattr(member, "timeout"):
            await member.timeout(until, reason=reason)
        else:
            await member.edit(communication_disabled_until=until, reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


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
            return existing

        log_channel = get_log_channel(guild)
        category = log_channel.category if log_channel is not None else None
        name = _channel_name_for_boss(boss)
        try:
            channel = await guild.create_text_channel(
                name=name,
                category=category,
                topic=f"Raid boss: {boss.get('display_name', 'Unknown Boss')}",
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
        if str(channel.name) == desired:
            boss["last_channel_name"] = desired
            return
        try:
            await channel.edit(name=desired, reason="Paragon boss HP update")
            boss["last_channel_name"] = desired
        except (discord.Forbidden, discord.HTTPException):
            pass

    def _make_new_boss(self, guild: discord.Guild, now: datetime, *, seed_value: str) -> dict[str, object]:
        snapshot = _guild_snapshot(guild)
        rng = _seeded_rng(guild.id, seed_value)
        _, _, display_name = _build_boss_name(rng)
        min_minutes = max(30, int(BOSS_DURATION_MIN_MINUTES))
        max_minutes = max(min_minutes, int(BOSS_DURATION_MAX_MINUTES))
        duration_minutes = rng.randint(min_minutes, max_minutes)
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
            "max_hp": int(snapshot["hp"]),
            "hp": int(snapshot["hp"]),
            "member_count": int(snapshot["member_count"]),
            "target_fighters": int(snapshot["target_fighters"]),
            "avg_prestige": float(snapshot["avg_prestige"]),
            "min_prestige": int(snapshot["min_prestige"]),
            "max_prestige": int(snapshot["max_prestige"]),
            "boss_prestige": int(snapshot["boss_prestige"]),
            "channel_id": 0,
            "last_channel_name": "",
            "attack_count": 0,
            "hit_count": 0,
            "total_damage": 0,
            "retaliations": 0,
            "heal_total": 0,
            "down_count": 0,
            "timeouts_inflicted": 0,
            "attackers": {},
            "downed": {},
            "recent_attackers": [],
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
        lines = [
            f"{prefix}**Raid Boss Appeared**",
            f"**{boss.get('display_name', 'Unknown Boss')}**",
            f"HP: **{_fmt_num(boss.get('hp', 0))} / {_fmt_num(boss.get('max_hp', 0))}**",
            (
                f"Tuned for about **{_as_int(boss.get('target_fighters', 1), 1)}** fighters out of "
                f"**{_as_int(boss.get('member_count', 1), 1)}** member(s)."
            ),
            (
                f"Boss prestige: **{_as_int(boss.get('boss_prestige', 0), 0)}** "
                f"(guild average **{_as_float(boss.get('avg_prestige', 0.0), 0.0):.1f}**). "
                "Hit chance scales from **50%** at the low end to **100%** at the top end."
            ),
            timer_line,
            f"`{COMMAND_PREFIX}attack` once every **{_fmt_remaining(int(BOSS_ATTACK_COOLDOWN_SECONDS))}**",
            f"`{COMMAND_PREFIX}res @user` or `{COMMAND_PREFIX}resurrect @user` to revive a downed raider",
            f"`{COMMAND_PREFIX}boss` from anywhere to check status",
            (
                f"Victory reward: attackers receive **+{BOSS_VICTORY_BOOST_PCT * 100.0:.0f}% XP/min** "
                f"for **{_fmt_duration_minutes(int(BOSS_VICTORY_BOOST_MINUTES))}**."
            ),
            (
                f"Failure penalty: attackers take **-{BOSS_FAILURE_DEBUFF_PCT * 100.0:.0f}% XP/min** "
                f"for **{_fmt_duration_minutes(int(BOSS_FAILURE_DEBUFF_MINUTES))}**."
            ),
        ]
        if forced:
            lines.append("*Forced spawn for testing/admin use.*")
        try:
            await channel.send(
                "\n".join(lines),
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
        except Exception:
            pass

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

    async def _resolve_victory(
        self,
        guild: discord.Guild,
        boss: dict,
        *,
        killer: Optional[discord.Member] = None,
    ) -> None:
        rewarded = 0
        attackers = _as_dict(boss.get("attackers"))
        for uid_s, row in attackers.items():
            uid = _as_int(uid_s, 0)
            if uid <= 0 or _as_int(_as_dict(row).get("attacks", 0), 0) <= 0:
                continue
            member = guild.get_member(uid)
            if member is None or member.bot:
                continue
            await grant_fixed_boost(
                member,
                pct=BOSS_VICTORY_BOOST_PCT,
                minutes=BOSS_VICTORY_BOOST_MINUTES,
                source=f"boss victory: {boss.get('display_name', 'raid boss')}",
                reward_seed_xp=(BOSS_VICTORY_BOOST_PCT * 100.0) * max(1, int(BOSS_VICTORY_BOOST_MINUTES)),
                persist=False,
            )
            record_game_fields(guild.id, member.id, "boss", victory_rewards=1)
            rewarded += 1
        await save_data()
        await self._finish_boss(guild, boss, outcome="victory", reward_count=rewarded, killer=killer)

    async def _resolve_failure(self, guild: discord.Guild, boss: dict) -> None:
        punished = 0
        attackers = _as_dict(boss.get("attackers"))
        for uid_s, row in attackers.items():
            uid = _as_int(uid_s, 0)
            if uid <= 0 or _as_int(_as_dict(row).get("attacks", 0), 0) <= 0:
                continue
            member = guild.get_member(uid)
            if member is None or member.bot:
                continue
            await grant_fixed_debuff(
                member,
                pct=BOSS_FAILURE_DEBUFF_PCT,
                minutes=BOSS_FAILURE_DEBUFF_MINUTES,
                source=f"boss failure: {boss.get('display_name', 'raid boss')}",
                reward_seed_xp=(BOSS_FAILURE_DEBUFF_PCT * 100.0) * max(1, int(BOSS_FAILURE_DEBUFF_MINUTES)),
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
        attackers = _as_dict(boss.get("attackers"))
        participant_count = sum(1 for row in attackers.values() if _as_int(_as_dict(row).get("attacks", 0), 0) > 0)
        top_rows: list[tuple[int, dict]] = []
        for uid_s, raw in attackers.items():
            uid = _as_int(uid_s, 0)
            row = _as_dict(raw)
            if uid <= 0 or _as_int(row.get("attacks", 0), 0) <= 0:
                continue
            top_rows.append((uid, row))
        top_rows.sort(
            key=lambda item: (
                -_as_int(item[1].get("damage", 0), 0),
                -_as_int(item[1].get("hits", 0), 0),
                -_as_int(item[1].get("attacks", 0), 0),
                item[0],
            )
        )

        engaged_at = _parse_iso(boss.get("engaged_at"))
        ended_at = _utcnow()
        engaged_duration = "Not engaged"
        if engaged_at is not None:
            engaged_duration = _fmt_duration_minutes(
                max(0, int((ended_at - engaged_at).total_seconds() // 60))
            )

        lines = [
            "**Boss Summary**",
            f"**{boss.get('display_name', 'Unknown Boss')}** - **{label}**",
            (
                f"HP: **{_fmt_num(boss.get('hp', 0))} / {_fmt_num(boss.get('max_hp', 0))}** left | "
                f"Damage dealt: **{_fmt_num(boss.get('total_damage', 0))}** | "
                f"Hits: **{_fmt_num(boss.get('hit_count', 0))}** / **{_fmt_num(boss.get('attack_count', 0))}**"
            ),
            (
                f"Participants: **{participant_count}** | "
                f"Downs: **{_fmt_num(boss.get('down_count', 0))}** | "
                f"Boss heals: **{_fmt_num(boss.get('heal_total', 0))}**"
            ),
            f"Fight duration: **{engaged_duration}**",
        ]
        if killer is not None:
            lines.append(f"Final blow: **{killer.display_name}**")
        if outcome == "victory":
            lines.append(
                f"Victory reward: **{reward_count}** attacker(s) received **+{BOSS_VICTORY_BOOST_PCT * 100.0:.0f}% XP/min** "
                f"for **{_fmt_duration_minutes(int(BOSS_VICTORY_BOOST_MINUTES))}**."
            )
        elif outcome == "failure":
            lines.append(
                f"Failure penalty: **{reward_count}** attacker(s) received **-{BOSS_FAILURE_DEBUFF_PCT * 100.0:.0f}% XP/min** "
                f"for **{_fmt_duration_minutes(int(BOSS_FAILURE_DEBUFF_MINUTES))}**."
            )
        else:
            lines.append("Nobody engaged the boss before it dissolved.")

        if top_rows:
            lines.append("Top raiders:")
            for uid, row in top_rows[:5]:
                member = guild.get_member(uid)
                name = member.display_name if member is not None else str(row.get("display_name", uid))
                lines.append(
                    f"- **{name}**: **{_fmt_num(row.get('damage', 0))}** damage, "
                    f"**{_fmt_num(row.get('hits', 0))}/{_fmt_num(row.get('attacks', 0))}** hits"
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
        lines = self._summary_lines(guild, boss, outcome=outcome, reward_count=reward_count, killer=killer)
        await self._delete_boss_channel(guild, boss)
        _clear_current_boss(st)
        await save_data()
        await self._announce_log(guild, "\n".join(lines))

    async def _retaliation_debuff(
        self,
        member: discord.Member,
        *,
        source_name: str,
        pct_range: tuple[float, float],
        minutes_range: tuple[int, int],
    ) -> tuple[str, dict]:
        pct_low, pct_high = pct_range
        min_minutes, max_minutes = minutes_range
        pct = random.uniform(min(pct_low, pct_high), max(pct_low, pct_high))
        minutes = random.randint(min(min_minutes, max_minutes), max(min_minutes, max_minutes))
        result = await grant_stacked_fixed_debuff(
            member,
            pct_add=pct,
            minutes_add=minutes,
            pct_cap=0.95,
            minutes_cap=max(180, max_minutes * 3),
            source=f"boss retaliation: {source_name}",
            source_prefix="boss retaliation",
            reward_seed_xp=(pct * 100.0) * minutes,
            persist=False,
        )
        if result.get("blocked", False):
            return f"{source_name} crashed against a Mulligan. The debuff was blocked.", result
        return (
            f"{source_name}: **-{result['percent']:.1f}% XP/min** for **{result['minutes']}m**.",
            result,
        )

    async def _perform_retaliation(
        self,
        guild: discord.Guild,
        boss: dict,
        attacker: discord.Member,
    ) -> str:
        rng = random.Random()
        boss_name = str(boss.get("display_name", "The Boss"))
        action = rng.choices(
            [
                "ashen_claw",
                "grave_brand",
                "iron_sentence",
                "sundering_roar",
                "black_tithe",
                "choir_of_ruin",
                "hollow_judgment",
                "sable_chain",
                "grave_fall",
                "void_glare",
            ],
            weights=[18, 14, 14, 12, 10, 10, 8, 7, 5, 14],
            k=1,
        )[0]
        row = _participant_row(boss, attacker)
        now_ts = _utcnow().timestamp()
        boss["retaliations"] = _as_int(boss.get("retaliations", 0), 0) + 1

        if action == "ashen_claw":
            line, result = await self._retaliation_debuff(
                attacker,
                source_name=f"{RETALIATION_NAMES[action]} from {boss_name}",
                pct_range=(float(BOSS_RETALIATE_DEBUFF_MIN_PCT), min(float(BOSS_RETALIATE_DEBUFF_MAX_PCT), 0.35)),
                minutes_range=(
                    int(BOSS_RETALIATE_DEBUFF_MIN_MINUTES),
                    min(int(BOSS_RETALIATE_DEBUFF_MAX_MINUTES), max(int(BOSS_RETALIATE_DEBUFF_MIN_MINUTES), 90)),
                ),
            )
            row["debuffs_taken"] = _as_int(row.get("debuffs_taken", 0), 0) + 1
            return f"**{RETALIATION_NAMES[action]}** lashes back at {attacker.mention}. {line}"

        if action == "grave_brand":
            line, result = await self._retaliation_debuff(
                attacker,
                source_name=f"{RETALIATION_NAMES[action]} from {boss_name}",
                pct_range=(max(float(BOSS_RETALIATE_DEBUFF_MIN_PCT), 0.35), float(BOSS_RETALIATE_DEBUFF_MAX_PCT)),
                minutes_range=(
                    max(int(BOSS_RETALIATE_DEBUFF_MIN_MINUTES), 90),
                    int(BOSS_RETALIATE_DEBUFF_MAX_MINUTES),
                ),
            )
            row["debuffs_taken"] = _as_int(row.get("debuffs_taken", 0), 0) + 1
            return f"**{RETALIATION_NAMES[action]}** brands {attacker.mention}. {line}"

        if action == "iron_sentence":
            seconds = random.randint(
                max(1, int(BOSS_RETALIATE_TIMEOUT_MIN_SECONDS)),
                max(int(BOSS_RETALIATE_TIMEOUT_MIN_SECONDS), int(BOSS_RETALIATE_TIMEOUT_MAX_SECONDS)),
            )
            applied = await _timeout_member(attacker, seconds, f"Boss retaliation: {boss_name}")
            if applied:
                row["timeouts_taken"] = _as_int(row.get("timeouts_taken", 0), 0) + 1
                boss["timeouts_inflicted"] = _as_int(boss.get("timeouts_inflicted", 0), 0) + 1
                return (
                    f"**{RETALIATION_NAMES[action]}** lands. {attacker.mention} is timed out for "
                    f"**{_fmt_remaining(seconds)}**."
                )
            return f"**{RETALIATION_NAMES[action]}** detonates, but I could not apply the timeout."

        if action == "sundering_roar":
            extra = random.randint(30, 90)
            row["next_attack_ts"] = max(float(row.get("next_attack_ts", 0.0)), now_ts) + extra
            row["cooldown_extensions"] = _as_int(row.get("cooldown_extensions", 0), 0) + 1
            return (
                f"**{RETALIATION_NAMES[action]}** shakes the chamber. {attacker.mention}'s next attack is delayed "
                f"by **{_fmt_remaining(extra)}**."
            )

        if action == "black_tithe":
            heal = random.randint(3, 8)
            before = _as_int(boss.get("hp", 0), 0)
            max_hp = max(1, _as_int(boss.get("max_hp", 1), 1))
            boss["hp"] = min(max_hp, before + heal)
            healed = max(0, _as_int(boss.get("hp", 0), 0) - before)
            boss["heal_total"] = _as_int(boss.get("heal_total", 0), 0) + healed
            return (
                f"**{RETALIATION_NAMES[action]}** drinks from the chaos and restores "
                f"**{_fmt_num(healed)} HP**."
            )

        if action == "choir_of_ruin":
            ally = _pick_other_recent_attacker(guild, boss, attacker.id)
            line, result = await self._retaliation_debuff(
                attacker,
                source_name=f"{RETALIATION_NAMES[action]} from {boss_name}",
                pct_range=(0.15, 0.25),
                minutes_range=(30, 60),
            )
            row["debuffs_taken"] = _as_int(row.get("debuffs_taken", 0), 0) + 1
            if ally is None:
                return f"**{RETALIATION_NAMES[action]}** rolls over {attacker.mention}. {line}"
            ally_row = _participant_row(boss, ally)
            ally_line, ally_result = await self._retaliation_debuff(
                ally,
                source_name=f"{RETALIATION_NAMES[action]} from {boss_name}",
                pct_range=(0.10, 0.20),
                minutes_range=(20, 45),
            )
            ally_row["debuffs_taken"] = _as_int(ally_row.get("debuffs_taken", 0), 0) + 1
            return (
                f"**{RETALIATION_NAMES[action]}** sweeps the raid.\n"
                f"{attacker.mention}: {line}\n"
                f"{ally.mention}: {ally_line}"
            )

        if action == "hollow_judgment":
            seconds = random.randint(20, 75)
            applied = await _timeout_member(attacker, seconds, f"Boss retaliation: {boss_name}")
            extra = random.randint(15, 45)
            row["next_attack_ts"] = max(float(row.get("next_attack_ts", 0.0)), now_ts) + extra
            row["cooldown_extensions"] = _as_int(row.get("cooldown_extensions", 0), 0) + 1
            if applied:
                row["timeouts_taken"] = _as_int(row.get("timeouts_taken", 0), 0) + 1
                boss["timeouts_inflicted"] = _as_int(boss.get("timeouts_inflicted", 0), 0) + 1
                return (
                    f"**{RETALIATION_NAMES[action]}** crushes {attacker.mention}: timeout **{_fmt_remaining(seconds)}**, "
                    f"and their next attack is delayed **{_fmt_remaining(extra)}**."
                )
            return (
                f"**{RETALIATION_NAMES[action]}** slows {attacker.mention}. "
                f"Next attack delayed **{_fmt_remaining(extra)}**."
            )

        if action == "sable_chain":
            ally = _pick_other_recent_attacker(guild, boss, attacker.id)
            if ally is None:
                return f"**{RETALIATION_NAMES[action]}** snaps through empty air."
            ally_row = _participant_row(boss, ally)
            extra = random.randint(45, 120)
            ally_row["next_attack_ts"] = max(float(ally_row.get("next_attack_ts", 0.0)), now_ts) + extra
            ally_row["cooldown_extensions"] = _as_int(ally_row.get("cooldown_extensions", 0), 0) + 1
            return (
                f"**{RETALIATION_NAMES[action]}** catches {ally.mention}. Their next attack is delayed "
                f"by **{_fmt_remaining(extra)}**."
            )

        if action == "grave_fall":
            if random.random() > max(0.0, min(1.0, float(BOSS_RETALIATE_DOWN_CHANCE))):
                return f"**{RETALIATION_NAMES[action]}** almost drops {attacker.mention}, but they stay standing."
            _down_member(boss, attacker, reason=RETALIATION_NAMES[action], now=_utcnow())
            row["downs_taken"] = _as_int(row.get("downs_taken", 0), 0) + 1
            boss["down_count"] = _as_int(boss.get("down_count", 0), 0) + 1
            return (
                f"**{RETALIATION_NAMES[action]}** downs {attacker.mention}. "
                f"Another raider must use `{COMMAND_PREFIX}res @{attacker.display_name}` before they can attack again."
            )

        return (
            f"**{RETALIATION_NAMES[action]}** fixes on {attacker.mention}, but this time the raid survives the glare."
        )

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
        lines = [
            f"**{boss.get('display_name', 'Unknown Boss')}**",
            f"HP: **{_fmt_num(boss.get('hp', 0))} / {_fmt_num(boss.get('max_hp', 0))}**",
            (
                f"Boss prestige: **{_as_int(boss.get('boss_prestige', 0), 0)}** | "
                f"Tuned raid size: **{_as_int(boss.get('target_fighters', 1), 1)}** fighter(s)"
            ),
        ]
        downed_count = len(_as_dict(boss.get("downed")))
        if downed_count > 0:
            lines.append(f"Downed raiders: **{downed_count}**")

        status = str(boss.get("status", "idle")).strip().lower()
        if status == "idle":
            idle_expires_at = _parse_iso(boss.get("idle_expires_at"))
            if idle_expires_at is not None:
                remaining = max(0, int((idle_expires_at - _utcnow()).total_seconds()))
                lines.append(f"Status: **Idle**. First `!attack` starts the timer. Idle window left: **{_fmt_remaining(remaining)}**.")
            else:
                lines.append("Status: **Idle**. First `!attack` starts the timer.")
        else:
            expires_at = _parse_iso(boss.get("expires_at"))
            if expires_at is not None:
                remaining = max(0, int((expires_at - _utcnow()).total_seconds()))
                lines.append(f"Status: **Active**. Time left: **{_fmt_remaining(remaining)}**.")
            else:
                lines.append("Status: **Active**.")

        channel = self._live_channel(guild, boss)
        if channel is not None:
            lines.append(f"Fight here: {channel.mention}")
        return lines

    @tasks.loop(minutes=1)
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
        boss = _current_boss(st)
        if not boss:
            next_spawn = _next_spawn_at(st)
            if next_spawn is None:
                next_spawn = _schedule_next_spawn(st, ctx.guild.id)
                await save_data()
            await ctx.reply(
                f"There is no active raid boss right now. The next random spawn is scheduled for **{_fmt_local_spawn(next_spawn)}**."
            )
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

        if _is_downed(boss, ctx.author.id):
            await ctx.reply(
                f"You are downed. Another raider must use `{ctx.clean_prefix}res {ctx.author.mention}` before you can attack again."
            )
            return

        row = _participant_row(boss, ctx.author)
        now = _utcnow()
        now_ts = now.timestamp()
        next_attack_ts = float(row.get("next_attack_ts", 0.0) or 0.0)
        if now_ts < next_attack_ts:
            await ctx.reply(f"You are recovering. Attack again in **{_fmt_remaining(int(next_attack_ts - now_ts))}**.")
            return

        if str(boss.get("status", "idle")).strip().lower() == "idle":
            boss["status"] = "active"
            boss["engaged_at"] = _iso(now)
            boss["expires_at"] = _iso(now + timedelta(minutes=max(1, _as_int(boss.get("duration_minutes", 1), 1))))

        prestige = _member_prestige(ctx.guild.id, ctx.author.id)
        chance = _hit_chance(boss, prestige)
        rng = random.Random()
        landed = rng.random() <= chance

        row["display_name"] = ctx.author.display_name
        row["attacks"] = _as_int(row.get("attacks", 0), 0) + 1
        row["last_attack_at"] = _iso(now)
        if not str(row.get("first_attack_at", "")).strip():
            row["first_attack_at"] = _iso(now)
        row["next_attack_ts"] = now_ts + max(1, int(BOSS_ATTACK_COOLDOWN_SECONDS))
        _push_recent_attacker(boss, ctx.author.id)

        boss["attack_count"] = _as_int(boss.get("attack_count", 0), 0) + 1

        attack_lines = [f"**{boss.get('display_name', 'Unknown Boss')}** retaliates against the raid."]
        if landed:
            damage, crit = _roll_damage(rng, prestige, boss)
            damage = min(damage, max(0, _as_int(boss.get("hp", 0), 0)))
            boss["hp"] = max(0, _as_int(boss.get("hp", 0), 0) - damage)
            boss["hit_count"] = _as_int(boss.get("hit_count", 0), 0) + 1
            boss["total_damage"] = _as_int(boss.get("total_damage", 0), 0) + damage
            row["hits"] = _as_int(row.get("hits", 0), 0) + 1
            row["damage"] = _as_int(row.get("damage", 0), 0) + damage
            record_game_fields(
                ctx.guild.id,
                ctx.author.id,
                "boss",
                attacks=1,
                hits=1,
                damage_total=damage,
            )
            attack_lines.append(
                f"{ctx.author.mention} strikes true at **{chance * 100.0:.1f}%** odds for **{_fmt_num(damage)}** damage"
                + (" on a crit." if crit else ".")
            )
        else:
            row["misses"] = _as_int(row.get("misses", 0), 0) + 1
            record_game_fields(ctx.guild.id, ctx.author.id, "boss", attacks=1, misses=1)
            attack_lines.append(
                f"{ctx.author.mention} misses at **{chance * 100.0:.1f}%** odds. The boss barely shifts."
            )

        await self._sync_channel_name(ctx.guild, boss)

        if _as_int(boss.get("hp", 0), 0) <= 0:
            await save_data()
            await ctx.reply(
                "\n".join(
                    attack_lines
                    + [
                        f"HP left: **0 / {_fmt_num(boss.get('max_hp', 0))}**.",
                        "The boss collapses. Closing the chamber and posting the summary in `paragon-log`.",
                    ]
                )
            )
            await self._resolve_victory(ctx.guild, boss, killer=ctx.author)
            return

        retaliation_line = await self._perform_retaliation(ctx.guild, boss, ctx.author)
        await self._sync_channel_name(ctx.guild, boss)
        await save_data()

        expires_at = _parse_iso(boss.get("expires_at"))
        remaining_line = ""
        if expires_at is not None:
            remaining_line = f" Time left: **{_fmt_remaining(max(0, int((expires_at - _utcnow()).total_seconds())))}**."
        await ctx.reply(
            "\n".join(
                attack_lines
                + [
                    f"HP left: **{_fmt_num(boss.get('hp', 0))} / {_fmt_num(boss.get('max_hp', 0))}**.{remaining_line}",
                    retaliation_line,
                ]
            )
        )

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

        if _is_downed(boss, ctx.author.id):
            await ctx.reply("You are downed and cannot resurrect anyone until another raider revives you.")
            return

        if target is None:
            downed_ids = [uid for uid in _as_dict(boss.get("downed")).keys() if _as_int(uid, 0) > 0]
            if len(downed_ids) == 1:
                target = ctx.guild.get_member(_as_int(downed_ids[0], 0))
            else:
                await ctx.reply(f"Usage: `{ctx.clean_prefix}res @user`")
                return

        if target.bot:
            await ctx.reply("Bots do not need resurrection.")
            return
        if target.id == ctx.author.id:
            await ctx.reply("You cannot resurrect yourself.")
            return
        if not _is_downed(boss, target.id):
            await ctx.reply(f"{target.display_name} is not downed.")
            return

        row = _participant_row(boss, ctx.author)
        now_ts = _utcnow().timestamp()
        next_res_ts = float(row.get("next_res_ts", 0.0) or 0.0)
        if now_ts < next_res_ts:
            await ctx.reply(
                f"You are still recovering from your last rescue. Try again in **{_fmt_remaining(int(next_res_ts - now_ts))}**."
            )
            return

        revived = _revive_member(boss, target.id)
        if not revived:
            await ctx.reply(f"{target.display_name} is no longer downed.")
            return

        row["resurrections"] = _as_int(row.get("resurrections", 0), 0) + 1
        row["next_res_ts"] = now_ts + max(1, int(BOSS_RES_COOLDOWN_SECONDS))
        target_row = _participant_row(boss, target)
        target_row["next_attack_ts"] = max(float(target_row.get("next_attack_ts", 0.0)), now_ts + 5.0)
        record_game_fields(ctx.guild.id, ctx.author.id, "boss", resurrections=1)
        await save_data()
        await ctx.reply(
            f"{ctx.author.mention} hauls {target.mention} back into the fight. "
            f"{target.display_name} can attack again in a few seconds."
        )

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
