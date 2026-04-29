from __future__ import annotations

import random
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

from .config import COMMAND_PREFIX
from .emojis import EMOJI_HOOK
from .fish_support import add_bait, consume_bait, get_bait, refund_bait
from .guild_setup import ensure_guild_setup, get_fishing_channel, get_fishing_channel_id
from .ownership import owner_only
from .spin import (
    _add_bonus_spins,
    _cycle_key,
    _sanitize_reset_time,
    _spin_user_state,
    _sync_spin_cycle_state,
    _wheel_state,
)
from .spin_support import (
    add_blackjack_natural_charges,
    add_cleanse_charges,
    add_mulligan_charges,
    add_roulette_backfire_shield,
    set_coinflip_win_edge,
    set_lotto_bonus_tickets_pct,
    set_lotto_jackpot_boost_multiplier,
    set_roulette_accuracy_bonus,
)
from .stats_store import record_game_fields
from .storage import _gdict, _udict, save_data
from .xp import apply_xp_change, grant_fixed_boost, prestige_cost


DOCK_CAST_EMOJI = "\N{FISHING POLE AND FISH}"
DOCK_STOP_EMOJI = "\N{OCTAGONAL SIGN}"
REEL_EMOJI = "\N{FISHING POLE AND FISH}"
LIFT_EMOJI = "\N{UPWARDS BLACK ARROW}"
GIVE_EMOJI = "\N{DOWNWARDS BLACK ARROW}"
SET_EMOJI = EMOJI_HOOK
SESSION_STOP_EMOJI = DOCK_STOP_EMOJI

BASE_CHEST_CHANCE = 0.05
FISH_LOOP_SECONDS = 5
DOCK_TIMER_STEP_MINUTES = 5
FISHING_SOURCE = "fishing catch"
CHEST_SOURCE = "fishing chest"


RARITY_META: dict[str, dict[str, object]] = {
    "common": {
        "label": "Common",
        "emoji": "\N{WHITE CIRCLE}",
        "weight": 60.0,
        "reward_pct": (1.0, 1.8),
        "required": 2,
        "max_errors": 3,
    },
    "uncommon": {
        "label": "Uncommon",
        "emoji": "\N{LARGE GREEN CIRCLE}",
        "weight": 25.0,
        "reward_pct": (1.8, 3.5),
        "required": 2,
        "max_errors": 3,
    },
    "rare": {
        "label": "Rare",
        "emoji": "\N{LARGE BLUE CIRCLE}",
        "weight": 9.0,
        "reward_pct": (4.0, 8.0),
        "required": 3,
        "max_errors": 2,
    },
    "epic": {
        "label": "Epic",
        "emoji": "\N{LARGE PURPLE CIRCLE}",
        "weight": 4.0,
        "reward_pct": (9.0, 18.0),
        "required": 3,
        "max_errors": 2,
    },
    "legendary": {
        "label": "Legendary",
        "emoji": "\N{LARGE ORANGE CIRCLE}",
        "weight": 1.5,
        "reward_pct": (20.0, 40.0),
        "required": 4,
        "max_errors": 2,
    },
    "mythic": {
        "label": "Mythic",
        "emoji": "\N{LARGE RED CIRCLE}",
        "weight": 0.45,
        "reward_pct": (45.0, 90.0),
        "required": 4,
        "max_errors": 2,
    },
    "ancient": {
        "label": "Ancient",
        "emoji": "\N{BLACK CIRCLE}",
        "weight": 0.05,
        "reward_pct": (100.0, 140.0),
        "required": 5,
        "max_errors": 2,
    },
}


FISH_BY_RARITY: dict[str, list[dict[str, object]]] = {
    "common": [
        {"name": "Bluegill", "length": (4.0, 11.0), "weight": (0.2, 1.1)},
        {"name": "Perch", "length": (5.0, 12.0), "weight": (0.3, 1.4)},
        {"name": "Sunfish", "length": (4.5, 10.5), "weight": (0.2, 1.0)},
        {"name": "Shiner", "length": (3.0, 7.5), "weight": (0.1, 0.4)},
        {"name": "Sardine", "length": (4.0, 9.0), "weight": (0.1, 0.6)},
    ],
    "uncommon": [
        {"name": "River Trout", "length": (10.0, 23.0), "weight": (0.9, 5.5)},
        {"name": "Largemouth Bass", "length": (10.0, 24.0), "weight": (1.0, 7.0)},
        {"name": "Channel Catfish", "length": (12.0, 28.0), "weight": (1.4, 10.5)},
        {"name": "Flounder", "length": (9.0, 20.0), "weight": (0.8, 4.6)},
        {"name": "Mackerel", "length": (10.0, 20.0), "weight": (0.9, 4.2)},
    ],
    "rare": [
        {"name": "Salmon", "length": (18.0, 38.0), "weight": (4.0, 20.0)},
        {"name": "Walleye", "length": (14.0, 31.0), "weight": (2.0, 12.0)},
        {"name": "Barracuda", "length": (18.0, 42.0), "weight": (3.0, 18.0)},
        {"name": "Moon Carp", "length": (16.0, 34.0), "weight": (2.0, 14.0)},
        {"name": "Steelhead", "length": (20.0, 40.0), "weight": (4.0, 19.0)},
    ],
    "epic": [
        {"name": "Ghost Eel", "length": (28.0, 55.0), "weight": (6.0, 28.0)},
        {"name": "Golden Koi", "length": (18.0, 36.0), "weight": (3.0, 16.0)},
        {"name": "Thunder Pike", "length": (26.0, 50.0), "weight": (6.0, 24.0)},
        {"name": "Blackwater Sturgeon", "length": (34.0, 70.0), "weight": (10.0, 48.0)},
    ],
    "legendary": [
        {"name": "Crown Pike", "length": (34.0, 64.0), "weight": (10.0, 40.0)},
        {"name": "Ember Salmon", "length": (28.0, 56.0), "weight": (8.0, 32.0)},
        {"name": "Mirror Tuna", "length": (42.0, 78.0), "weight": (18.0, 70.0)},
        {"name": "Old King Catfish", "length": (34.0, 72.0), "weight": (14.0, 56.0)},
    ],
    "mythic": [
        {"name": "Void Eel", "length": (48.0, 92.0), "weight": (20.0, 85.0)},
        {"name": "Sunscale Dragonfish", "length": (36.0, 66.0), "weight": (12.0, 46.0)},
        {"name": "Leviathan Gar", "length": (52.0, 102.0), "weight": (24.0, 110.0)},
    ],
    "ancient": [
        {"name": "Choir Ray", "length": (62.0, 122.0), "weight": (40.0, 180.0)},
        {"name": "Deepglass Wyrm", "length": (70.0, 138.0), "weight": (55.0, 240.0)},
    ],
}


SIZE_TIERS: list[tuple[float, str, float]] = [
    (0.18, "Tiny", 0.85),
    (0.36, "Small", 1.00),
    (0.58, "Solid", 1.12),
    (0.80, "Keeper", 1.30),
    (0.94, "Trophy", 1.62),
    (1.01, "Monster", 2.05),
]


CUE_POOLS: dict[str, list[str]] = {
    "lift": [
        "The weight softens and the fish hangs just under the surface for a breath.",
        "The line loosens with a flutter, like the fish is turning up into you.",
        "You feel a hesitant throb and a brief pocket of slack under the rod tip.",
        "The pull goes light and floaty, as if the fish has rolled near the top.",
        "The pressure fades just enough to make the line whisper instead of hum.",
        "The rod tip recovers and the fish seems to hesitate in place for a heartbeat.",
        "The line stops digging and starts wavering high, almost weightless.",
        "It feels like the fish is lifting with the current instead of boring down.",
    ],
    "give": [
        "The reel chatters and the fish tears line in one long hard run.",
        "The rod buries and something powerful starts pulling away with no pause.",
        "Line peels fast enough that the guides start to sing.",
        "The fish surges downstream and the whole rod loads up under steady pressure.",
        "A heavy run starts and the spool feels hot in a hurry.",
        "The fish digs deep and keeps taking line instead of turning.",
        "Your drag starts ticking in a clean, relentless pull away from shore.",
        "The line knifes off at an angle and the fish refuses to slow down.",
    ],
    "set": [
        "The float vanishes in a single sharp snap and the line jolts tight.",
        "A sudden downward punch hits the rod like the fish just committed.",
        "The line cuts sideways hard in one violent take.",
        "There is one clean strike, heavy and immediate, with no warning.",
        "The tip drops hard and the line comes tight all at once.",
        "A snapping hit knocks the line out of its lazy drift in an instant.",
        "The take is abrupt and surgical, like the hook needs to bite now.",
        "Everything goes from still to fully committed in one brutal yank.",
    ],
}


WATER_STATES: dict[str, dict[str, object]] = {
    "empty_reach": {
        "name": "Empty Reach",
        "weight": 12,
        "bite_range": (55, 90),
        "duration": (45, 75),
        "xp_mult": 0.85,
        "size_mult": 0.92,
        "chest_bonus": 0.00,
        "read": "Quiet, lean water. Slow bites, skinny fish, and a lot of patience.",
        "rarity_mults": {
            "common": 1.90,
            "uncommon": 1.35,
            "rare": 0.70,
            "epic": 0.35,
            "legendary": 0.12,
            "mythic": 0.03,
            "ancient": 0.01,
        },
        "flavors": [
            "The surface looks flat and indifferent, with only the occasional lazy ring breaking it.",
            "Thin ripples creep through water that feels almost too still to trust.",
            "Even the baitfish seem spread out and unsure of themselves.",
            "The bank is quiet enough that every cast feels a little optimistic.",
            "Nothing looks dead, exactly. It just looks like everything moved somewhere else.",
        ],
    },
    "frenzy_water": {
        "name": "Frenzy Water",
        "weight": 11,
        "bite_range": (18, 38),
        "duration": (35, 60),
        "xp_mult": 0.98,
        "size_mult": 0.96,
        "chest_bonus": 0.01,
        "read": "Fast action and nervous water. Plenty of bites, mostly little fighters.",
        "rarity_mults": {
            "common": 2.10,
            "uncommon": 1.45,
            "rare": 0.82,
            "epic": 0.40,
            "legendary": 0.14,
            "mythic": 0.03,
            "ancient": 0.01,
        },
        "flavors": [
            "Silver flashes keep breaking the surface like something is shoving bait in all directions.",
            "The water is twitchy and alive, full of panic ripples and quick slaps.",
            "Small wakes keep colliding in the shallows, too many to track cleanly.",
            "Bait keeps dimpling the top in fast little bursts that never fully settle.",
            "You get the feeling that if something hits, it probably won't stay subtle for long.",
        ],
    },
    "weed_tangle": {
        "name": "Weed Tangle",
        "weight": 10,
        "bite_range": (24, 50),
        "duration": (45, 75),
        "xp_mult": 0.92,
        "size_mult": 1.12,
        "chest_bonus": 0.01,
        "read": "Messy edges and hidden ambush fish. More fouls, but the better bodies hide here too.",
        "rarity_mults": {
            "common": 1.55,
            "uncommon": 1.40,
            "rare": 0.90,
            "epic": 0.52,
            "legendary": 0.20,
            "mythic": 0.05,
            "ancient": 0.01,
        },
        "flavors": [
            "The shoreline is choked with weeds that keep twitching just a little too much.",
            "Pockets in the grass open and close as if something heavier is threading through them.",
            "It looks snaggy, moody, and exactly like the kind of place a brute would sit.",
            "The green mats are thick, but they keep shivering from underneath.",
            "If anything hits here, it'll probably try to drag you into the ugly stuff first.",
        ],
    },
    "stormwash": {
        "name": "Stormwash",
        "weight": 8,
        "bite_range": (34, 68),
        "duration": (45, 80),
        "xp_mult": 1.16,
        "size_mult": 1.24,
        "chest_bonus": 0.04,
        "read": "Broken water and heavier shoulders. Slower tempo, bigger pulls, better salvage.",
        "rarity_mults": {
            "common": 0.90,
            "uncommon": 1.05,
            "rare": 1.22,
            "epic": 1.18,
            "legendary": 0.50,
            "mythic": 0.12,
            "ancient": 0.02,
        },
        "flavors": [
            "Foam is tearing loose in strips and the chop keeps breaking against itself.",
            "The water looks bruised and swollen, full of broken reflections and heavy drifts.",
            "Bits of bark and weed are rolling through like the river just coughed them up.",
            "The surface never settles in one direction for more than a heartbeat.",
            "It feels like the kind of water that gives you fewer chances and meaner ones.",
        ],
    },
    "glasswater": {
        "name": "Glasswater",
        "weight": 8,
        "bite_range": (40, 80),
        "duration": (45, 80),
        "xp_mult": 1.10,
        "size_mult": 1.06,
        "chest_bonus": 0.01,
        "read": "Clear, suspicious water. Fewer mistakes, but the fish that rise tend to be cleaner and stranger.",
        "rarity_mults": {
            "common": 0.88,
            "uncommon": 1.00,
            "rare": 1.20,
            "epic": 1.20,
            "legendary": 0.44,
            "mythic": 0.09,
            "ancient": 0.02,
        },
        "flavors": [
            "The top is almost mirror-flat, showing every ring and every hesitation.",
            "You can see deeper than usual, which somehow makes the water feel more secretive.",
            "The surface is clean enough that every bad presentation would probably get judged.",
            "Nothing is rushing. Everything looks deliberate and a little watchful.",
            "It is the sort of water that rewards clean reads more than brute persistence.",
        ],
    },
    "golden_hour": {
        "name": "Golden Hour",
        "weight": 5,
        "bite_range": (16, 32),
        "duration": (25, 45),
        "xp_mult": 1.28,
        "size_mult": 1.10,
        "chest_bonus": 0.02,
        "read": "The water is lit up and generous. Action comes fast, with real chances at quality.",
        "rarity_mults": {
            "common": 1.15,
            "uncommon": 1.25,
            "rare": 1.38,
            "epic": 1.20,
            "legendary": 0.55,
            "mythic": 0.14,
            "ancient": 0.03,
        },
        "flavors": [
            "The surface is flashing gold and every little seam seems alive.",
            "Bait keeps flickering in the light like the whole bank is waking up at once.",
            "Warm bands of color are running across the water and the fish know it.",
            "Everything out there looks briefly easier than it should.",
            "It feels like one of those windows people brag about for a week afterward.",
        ],
    },
    "wreck_drift": {
        "name": "Wreck Drift",
        "weight": 7,
        "bite_range": (28, 60),
        "duration": (40, 75),
        "xp_mult": 1.02,
        "size_mult": 1.06,
        "chest_bonus": 0.08,
        "read": "Snags, salvage, and fish using debris for cover. Chests show up here more than they should.",
        "rarity_mults": {
            "common": 1.10,
            "uncommon": 1.20,
            "rare": 1.12,
            "epic": 0.95,
            "legendary": 0.34,
            "mythic": 0.08,
            "ancient": 0.02,
        },
        "flavors": [
            "Timber scraps, rope, and old junk are lining up in the current like a moving shelf.",
            "Every cast looks like it might find either treasure or a truly annoying snag.",
            "Something about the drift feels man-made, which usually means pockets and prizes.",
            "Odd glints keep rolling through between bark and weed clumps.",
            "The water looks like it has been collecting secrets instead of cleaning itself out.",
        ],
    },
    "moon_slick": {
        "name": "Moon Slick",
        "weight": 4,
        "bite_range": (36, 72),
        "duration": (30, 55),
        "xp_mult": 1.36,
        "size_mult": 1.14,
        "chest_bonus": 0.02,
        "read": "Quiet, dark water with deep runners. Less volume, much better ceiling.",
        "rarity_mults": {
            "common": 0.60,
            "uncommon": 0.90,
            "rare": 1.18,
            "epic": 1.25,
            "legendary": 0.80,
            "mythic": 0.22,
            "ancient": 0.05,
        },
        "flavors": [
            "The water looks black and oily, cut only by silver scars that vanish too quickly.",
            "Soft rings appear without warning and disappear before they explain themselves.",
            "The current feels deeper than the bank suggests, like the channel dropped overnight.",
            "Everything about the surface says the interesting fish are not feeding shallow.",
            "It is quiet enough to make every sudden take feel slightly supernatural.",
        ],
    },
    "silt_bloom": {
        "name": "Silt Bloom",
        "weight": 9,
        "bite_range": (30, 62),
        "duration": (45, 75),
        "xp_mult": 0.96,
        "size_mult": 1.00,
        "chest_bonus": 0.02,
        "read": "Clouded, dirty water. Fish are close, but the takes can be sloppy and hard to read cleanly.",
        "rarity_mults": {
            "common": 1.30,
            "uncommon": 1.22,
            "rare": 0.92,
            "epic": 0.58,
            "legendary": 0.20,
            "mythic": 0.05,
            "ancient": 0.01,
        },
        "flavors": [
            "The water is cloudy enough that every movement becomes a rumor before it becomes a fact.",
            "Warm brown plumes keep rolling through the seams and hiding the bottom.",
            "You can feel life in it, but not much of it wants to declare itself clearly.",
            "The current looks thick, almost padded, like it's swallowing clean signals.",
            "If something good is in there, it is going to make you earn the read.",
        ],
    },
    "abyssal_pull": {
        "name": "Abyssal Pull",
        "weight": 2,
        "bite_range": (52, 95),
        "duration": (25, 45),
        "xp_mult": 1.52,
        "size_mult": 1.30,
        "chest_bonus": 0.05,
        "read": "Long silences, brutal takes, and fish that feel older than the channel they came from.",
        "rarity_mults": {
            "common": 0.24,
            "uncommon": 0.55,
            "rare": 1.00,
            "epic": 1.25,
            "legendary": 1.15,
            "mythic": 0.50,
            "ancient": 0.15,
        },
        "flavors": [
            "The center of the channel just looks darker than it has any right to.",
            "The water swallows reflections instead of throwing them back.",
            "Even the current seems quieter here, like it is trying not to wake something.",
            "Every few moments the surface dimples once, too deep and too slow for small fish.",
            "It feels like dead time right up until the moment it absolutely is not.",
        ],
    },
}


CHEST_LOOT: list[dict[str, object]] = [
    {"key": "bonus_spins", "name": "Rust-Scored Wheel Token", "weight": 18},
    {"key": "bait_cache", "name": "Salted Bait Bundle", "weight": 20},
    {"key": "boost_small", "name": "Lucky Tackle Charm", "weight": 18},
    {"key": "boost_big", "name": "Captain's Thermos", "weight": 10},
    {"key": "shield", "name": "Kelp-Wrapped Ward", "weight": 10},
    {"key": "aim", "name": "Polished Glass Sinker", "weight": 10},
    {"key": "cleanse", "name": "Saltbite Cleanser", "weight": 7},
    {"key": "natural", "name": "Loaded Card Scale", "weight": 3},
    {"key": "mulligan", "name": "Driftwood Totem", "weight": 2},
    {"key": "coinflip_edge", "name": "Tideworn Coin", "weight": 6},
    {"key": "lotto_bonus", "name": "Numbered Map Scrap", "weight": 4},
    {"key": "lotto_amp", "name": "Jackpot Pearl", "weight": 2},
]


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


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


def _now_ts() -> int:
    return int(time.time())


def _fmt_num(value: int | float) -> str:
    num = float(value)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.2f}"


def _fmt_pct(value: float) -> str:
    pct = float(value)
    if abs(pct - round(pct)) < 1e-9:
        return f"{int(round(pct))}%"
    return f"{pct:.1f}%"


def _fmt_remaining(seconds: int | float) -> str:
    total = max(0, int(round(float(seconds))))
    minutes, secs = divmod(total, 60)
    if minutes <= 0:
        return f"{secs}s"
    hours, mins = divmod(minutes, 60)
    if hours <= 0:
        return f"{mins}m {secs:02d}s"
    return f"{hours}h {mins:02d}m"


def _command_text(command: str, *, prefix: str = COMMAND_PREFIX) -> str:
    return f"`{prefix}{command}`"


def _join_lines(lines: list[str]) -> str:
    return "\n".join(line for line in lines if str(line).strip())


def _dock_timer_minutes(expires_at: int, *, now_ts: Optional[int] = None) -> int:
    now = _now_ts() if now_ts is None else int(now_ts)
    remaining = max(0, int(expires_at) - now)
    if remaining <= 0:
        return 0
    whole_minutes = max(1, (remaining + 59) // 60)
    step = max(1, int(DOCK_TIMER_STEP_MINUTES))
    return max(step, ((whole_minutes + step - 1) // step) * step)


def _fmt_dock_timer(minutes: int) -> str:
    rounded = max(0, int(minutes))
    if rounded <= 0:
        return "0m"
    hours, mins = divmod(rounded, 60)
    if hours <= 0:
        return f"{mins}m"
    if mins <= 0:
        return f"{hours}h"
    return f"{hours}h {mins:02d}m"


def _weighted_choice(rng: random.Random, rows: list[tuple[str, float]]) -> str:
    keys = [key for key, _ in rows]
    weights = [max(0.0, float(weight)) for _, weight in rows]
    return rng.choices(keys, weights=weights, k=1)[0]


def _state_root(gid: int) -> dict:
    g = _gdict(gid)
    st = g.get("fishing")
    if not isinstance(st, dict):
        st = {}
        g["fishing"] = st
    st.setdefault("dock_message_id", 0)
    st.setdefault("controls_message_id", 0)
    st.setdefault("dock_timer_minutes", -1)
    st.setdefault("state_key", "")
    st.setdefault("state_flavor", "")
    st.setdefault("state_expires_at", 0)
    return st


def _session_state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("fishing_session")
    if not isinstance(st, dict):
        st = {}
        u["fishing_session"] = st
    st.setdefault("active", False)
    st.setdefault("phase", "idle")
    st.setdefault("channel_id", 0)
    st.setdefault("session_message_id", 0)
    st.setdefault("loaded_bait", 0)
    st.setdefault("bite_at", 0)
    st.setdefault("cast_state_key", "")
    st.setdefault("cast_state_name", "")
    st.setdefault("cast_state_flavor", "")
    st.setdefault("fish", {})
    st.setdefault("successes", 0)
    st.setdefault("mistakes", 0)
    st.setdefault("required", 0)
    st.setdefault("max_mistakes", 0)
    st.setdefault("current_action", "")
    st.setdefault("current_cue", "")
    st.setdefault("last_result_text", "")
    st["fish"] = _as_dict(st.get("fish"))
    return st


def _active_sessions(gid: int) -> list[tuple[int, dict]]:
    g = _gdict(gid)
    users = _as_dict(g.get("users"))
    out: list[tuple[int, dict]] = []
    for uid_s, user_raw in users.items():
        try:
            uid = int(uid_s)
        except Exception:
            continue
        user = _as_dict(user_raw)
        st = _as_dict(user.get("fishing_session"))
        if not st:
            continue
        if bool(st.get("active", False)) or _as_int(st.get("session_message_id", 0), 0) > 0:
            out.append((uid, st))
    return out


def _norm_emoji_name(emoji: discord.PartialEmoji | str) -> str:
    try:
        name = emoji.name if isinstance(emoji, discord.PartialEmoji) else str(emoji)
    except Exception:
        name = str(emoji)
    return name.replace("\ufe0f", "")


def _water_state(st: dict) -> dict:
    return WATER_STATES.get(str(st.get("state_key", "")).strip().lower(), WATER_STATES["empty_reach"])


def _roll_water_state(gid: int) -> bool:
    st = _state_root(gid)
    now = _now_ts()
    current_key = str(st.get("state_key", "")).strip().lower()
    if current_key in WATER_STATES and _as_int(st.get("state_expires_at", 0), 0) > now:
        return False

    seed = int.from_bytes(f"{gid}:{now // 60}:{random.random()}".encode("utf-8"), "little", signed=False) % (2**32)
    rng = random.Random(seed)
    key = _weighted_choice(rng, [(name, _as_float(data.get("weight", 1.0), 1.0)) for name, data in WATER_STATES.items()])
    data = WATER_STATES[key]
    minutes_min, minutes_max = data.get("duration", (45, 75))
    expires_at = now + rng.randint(int(minutes_min) * 60, int(minutes_max) * 60)
    st["state_key"] = key
    st["state_flavor"] = rng.choice(list(data.get("flavors", []) or [data.get("read", "The water shifts.")]))
    st["state_expires_at"] = int(expires_at)
    return True


def _roll_size_tier(size_roll: float) -> tuple[str, float]:
    ratio = max(0.0, min(1.0, float(size_roll)))
    for limit, label, mult in SIZE_TIERS:
        if ratio <= limit:
            return label, float(mult)
    return "Solid", 1.0


def _roll_fish_for_state(gid: int, uid: int, state_key: str) -> dict[str, object]:
    state = WATER_STATES.get(state_key, WATER_STATES["empty_reach"])
    rng = random.Random(f"{gid}:{uid}:{time.time_ns()}:{state_key}")
    rarity_rows: list[tuple[str, float]] = []
    rarity_mults = _as_dict(state.get("rarity_mults"))
    for rarity, meta in RARITY_META.items():
        weight = _as_float(meta.get("weight", 1.0), 1.0) * _as_float(rarity_mults.get(rarity, 1.0), 1.0)
        rarity_rows.append((rarity, max(0.001, weight)))
    rarity = _weighted_choice(rng, rarity_rows)
    fish_base = dict(rng.choice(FISH_BY_RARITY[rarity]))
    meta = _as_dict(RARITY_META[rarity])

    size_mult = max(0.4, _as_float(state.get("size_mult", 1.0), 1.0))
    size_roll = max(0.0, min(1.0, rng.random() ** (1.0 / size_mult)))
    length_min, length_max = fish_base.get("length", (6.0, 12.0))
    weight_min, weight_max = fish_base.get("weight", (0.3, 1.0))
    length_in = round(float(length_min) + ((float(length_max) - float(length_min)) * size_roll), 1)
    weight_lb = round(float(weight_min) + ((float(weight_max) - float(weight_min)) * (size_roll ** 1.35)), 1)
    size_label, size_reward_mult = _roll_size_tier(size_roll)

    pct_min, pct_max = meta.get("reward_pct", (1.0, 2.0))
    reward_pct = rng.uniform(float(pct_min), float(pct_max))
    reward_pct *= size_reward_mult
    reward_pct *= max(0.7, _as_float(state.get("xp_mult", 1.0), 1.0))

    prestige_level = max(0, int(_udict(gid, uid).get("prestige", 0)))
    prestige_base = max(1, int(prestige_cost(prestige_level)))
    reward_xp = max(1, int(round(prestige_base * (reward_pct / 100.0))))

    required = max(2, _as_int(meta.get("required", 2), 2))
    if size_roll >= 0.82:
        required += 1
    max_errors = max(1, _as_int(meta.get("max_errors", 2), 2))

    return {
        "name": str(fish_base.get("name", "Fish")),
        "rarity": rarity,
        "rarity_label": str(meta.get("label", rarity.title())),
        "rarity_emoji": str(meta.get("emoji", "")),
        "length_in": float(length_in),
        "weight_lb": float(weight_lb),
        "size_label": size_label,
        "reward_pct": float(reward_pct),
        "reward_xp": int(reward_xp),
        "required": int(required),
        "max_errors": int(max_errors),
    }


async def _grant_bonus_spins(gid: int, uid: int, amount: int) -> int:
    wheel_state = _wheel_state(gid)
    h, m = _sanitize_reset_time(
        wheel_state.get("reset_hour", 0),
        wheel_state.get("reset_minute", 0),
    )
    ust = _spin_user_state(gid, uid)
    _sync_spin_cycle_state(ust, _cycle_key(h, m))
    return _add_bonus_spins(ust, max(1, int(amount)))


class FishCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _remove_user_reaction(
        self,
        channel: discord.TextChannel,
        message_id: int,
        emoji: discord.PartialEmoji,
        member: discord.Member,
    ) -> None:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.remove_reaction(emoji, member)
        except Exception:
            return

    async def _fetch_message(self, channel: discord.TextChannel, message_id: int) -> Optional[discord.Message]:
        if message_id <= 0:
            return None
        try:
            return await channel.fetch_message(message_id)
        except Exception:
            return None

    async def _set_reactions(self, msg: discord.Message, emojis: list[str]) -> None:
        try:
            await msg.clear_reactions()
        except Exception:
            pass
        for emoji in emojis:
            try:
                await msg.add_reaction(emoji)
            except Exception:
                continue

    def _render_controls_message(self) -> str:
        return _join_lines(
            [
                "**Fishing Controls**",
                f"Start a cast: react {DOCK_CAST_EMOJI} on the dock or use {_command_text('fish cast')}.",
                f"Stop fishing: react {DOCK_STOP_EMOJI} on the dock or your fishing message, or use {_command_text('fish stop')}.",
                f"Reel when a bite hits: react {REEL_EMOJI} or use {_command_text('fish reel')}.",
                "",
                "**Fight Mechanics**",
                "Each fish needs enough clean reads before it hits the escape limit.",
                f"Soft hesitation -> `lift` {LIFT_EMOJI}",
                f"Screaming run -> `give` {GIVE_EMOJI}",
                f"Sharp strike -> `set` {SET_EMOJI}",
                "",
                "**Bait**",
                f"Buy **Bait Crate x25** in {_command_text('shop')}.",
                "Casting loads 1 bait, and stopping early recovers the bait still on the line.",
            ]
        )

    async def _ensure_controls_message(self, guild: discord.Guild) -> bool:
        channel = get_fishing_channel(guild)
        if channel is None:
            return False

        st = _state_root(guild.id)
        content = self._render_controls_message()
        msg = await self._fetch_message(channel, _as_int(st.get("controls_message_id", 0), 0))

        if msg is None:
            msg = await channel.send(content)
            st["controls_message_id"] = int(msg.id)
            return True

        if msg.content != content:
            try:
                await msg.edit(content=content)
            except Exception:
                return False
            return True

        return False

    def _render_dock(self, guild: discord.Guild) -> str:
        st = _state_root(guild.id)
        state_changed = _roll_water_state(guild.id)
        if state_changed or not str(st.get("state_key", "")).strip():
            st = _state_root(guild.id)
        water = _water_state(st)
        active_lines = sum(1 for _, sess in _active_sessions(guild.id) if bool(sess.get("active", False)))
        timer_minutes = _dock_timer_minutes(_as_int(st.get("state_expires_at", 0), 0))
        return _join_lines(
            [
                "**Paragon Fishing Dock**",
                f"Water now: **{water.get('name', 'Unknown Water')}**",
                f"{str(st.get('state_flavor', water.get('read', 'The water shifts.')))}",
                f"Next turn: **{_fmt_dock_timer(timer_minutes)}**",
                f"Active lines: **{active_lines}**",
            ]
        )

    async def _ensure_dock_message(
        self,
        guild: discord.Guild,
        *,
        force_refresh: bool = False,
        move_to_latest: bool = False,
    ) -> bool:
        channel = get_fishing_channel(guild)
        if channel is None:
            return False

        st = _state_root(guild.id)
        msg = await self._fetch_message(channel, _as_int(st.get("dock_message_id", 0), 0))
        content = self._render_dock(guild)
        st = _state_root(guild.id)
        timer_minutes = _dock_timer_minutes(_as_int(st.get("state_expires_at", 0), 0))
        changed = False

        if msg is None:
            msg = await channel.send(content)
            st["dock_message_id"] = int(msg.id)
            st["dock_timer_minutes"] = int(timer_minutes)
            await self._set_reactions(msg, [DOCK_CAST_EMOJI, DOCK_STOP_EMOJI])
            return True

        if move_to_latest:
            try:
                new_msg = await channel.send(content)
            except Exception:
                return False
            st["dock_message_id"] = int(new_msg.id)
            st["dock_timer_minutes"] = int(timer_minutes)
            await self._set_reactions(new_msg, [DOCK_CAST_EMOJI, DOCK_STOP_EMOJI])
            try:
                await msg.delete()
            except Exception:
                pass
            return True

        if force_refresh or msg.content != content or _as_int(st.get("dock_timer_minutes", -1), -1) != timer_minutes:
            try:
                if msg.content != content:
                    await msg.edit(content=content)
                changed = True
            except Exception:
                return False
            st["dock_timer_minutes"] = int(timer_minutes)
            await self._set_reactions(msg, [DOCK_CAST_EMOJI, DOCK_STOP_EMOJI])

        return changed

    def _bite_delay(self, state_key: str) -> int:
        state = WATER_STATES.get(state_key, WATER_STATES["empty_reach"])
        low, high = state.get("bite_range", (30, 60))
        return random.randint(int(low), int(high))

    def _begin_cast(self, guild: discord.Guild, member: discord.Member, session: dict, *, carry_text: str = "") -> bool:
        gid = guild.id
        uid = member.id
        state_root = _state_root(gid)
        state_changed = _roll_water_state(gid)
        if state_changed or not str(state_root.get("state_key", "")).strip():
            state_root = _state_root(gid)
        state_key = str(state_root.get("state_key", "empty_reach")).strip().lower() or "empty_reach"
        if not consume_bait(gid, uid, amount=1):
            session["active"] = False
            session["phase"] = "idle"
            session["loaded_bait"] = 0
            session["last_result_text"] = carry_text
            return False

        session["active"] = True
        session["phase"] = "waiting"
        session["channel_id"] = int(get_fishing_channel_id(guild.id) or 0)
        session["loaded_bait"] = 1
        session["bite_at"] = _now_ts() + self._bite_delay(state_key)
        session["cast_state_key"] = state_key
        session["cast_state_name"] = str(WATER_STATES.get(state_key, WATER_STATES["empty_reach"]).get("name", "Unknown Water"))
        session["cast_state_flavor"] = str(state_root.get("state_flavor", "The water shifts."))
        session["fish"] = {}
        session["successes"] = 0
        session["mistakes"] = 0
        session["required"] = 0
        session["max_mistakes"] = 0
        session["current_action"] = ""
        session["current_cue"] = ""
        session["last_result_text"] = str(carry_text or "").strip()
        record_game_fields(gid, uid, "fishing", casts_started=1, bait_used=1)
        return True

    def _roll_next_cue(self, session: dict) -> None:
        prev = str(session.get("current_action", "")).strip().lower()
        actions = ["lift", "give", "set"]
        if prev in actions and random.random() < 0.75:
            actions = [name for name in actions if name != prev]
        action = random.choice(actions)
        session["current_action"] = action
        session["current_cue"] = random.choice(CUE_POOLS[action])

    def _status_summary(self, guild: discord.Guild, member: discord.Member, session: dict) -> list[str]:
        bait = get_bait(guild.id, member.id)
        water = WATER_STATES.get(str(_state_root(guild.id).get("state_key", "")).strip().lower(), WATER_STATES["empty_reach"])
        lines = [
            f"Water now: **{water.get('name', 'Unknown Water')}**",
            f"Bait in tackle box: **{bait}**",
        ]
        if bool(session.get("active", False)):
            phase = str(session.get("phase", "waiting")).strip().lower()
            if phase == "waiting":
                lines.append("Line status: **Waiting for a bite**.")
            elif phase == "bite":
                lines.append(f"Line status: **Bite up**. Use {_command_text('fish reel')}.")
            elif phase == "fight":
                lines.append(
                    f"Line status: **Reeling** (**{_as_int(session.get('successes', 0), 0)}/{_as_int(session.get('required', 0), 0)}** clean reads, "
                    f"**{_as_int(session.get('mistakes', 0), 0)}/{_as_int(session.get('max_mistakes', 0), 0)}** mistakes)."
                )
        else:
            lines.append("Line status: **Packed up**.")
        return lines

    def _render_session_message(self, guild: discord.Guild, member: discord.Member, session: dict) -> str:
        lines: list[str] = []
        result_text = str(session.get("last_result_text", "")).strip()
        if result_text:
            lines.append(result_text)

        phase = str(session.get("phase", "idle")).strip().lower()
        bait = get_bait(guild.id, member.id)

        if not bool(session.get("active", False)) or phase == "idle":
            lines.extend(
                [
                    f"{DOCK_CAST_EMOJI} {member.mention} has their line out of the water.",
                    f"Bait in tackle box: **{bait}**",
                ]
            )
            return _join_lines(lines)

        state_name = str(session.get("cast_state_name", "Unknown Water")).strip() or "Unknown Water"
        if phase == "waiting":
            lines.extend(
                [
                    f"{DOCK_CAST_EMOJI} {member.mention} is fishing the **{state_name}**.",
                    "Line status: **Waiting on a bite**.",
                    f"Bait still on the line: **1** | Spare bait in tackle box: **{bait}**",
                    f"Use {_command_text('fish stop')} or react {SESSION_STOP_EMOJI} to pack it up and recover this bait.",
                ]
            )
            return _join_lines(lines)

        if phase == "bite":
            lines.extend(
                [
                    f"{DOCK_CAST_EMOJI} {member.mention} just got a bite in the **{state_name}**.",
                    "The float snaps under and the line jumps tight for a moment.",
                    f"React {REEL_EMOJI} or use {_command_text('fish reel')} to lean into it.",
                    f"You can still {_command_text('fish stop')} or react {SESSION_STOP_EMOJI} to recover your bait and call it here.",
                ]
            )
            return _join_lines(lines)

        if phase == "fight":
            lines.extend(
                [
                    f"{DOCK_CAST_EMOJI} {member.mention} is fighting something on the line.",
                    f"Catch progress: **{_as_int(session.get('successes', 0), 0)}/{_as_int(session.get('required', 0), 0)}**",
                    f"Escape pressure: **{_as_int(session.get('mistakes', 0), 0)}/{_as_int(session.get('max_mistakes', 0), 0)}**",
                    f"{session.get('current_cue', 'Read the line carefully.')}",
                ]
            )
            return _join_lines(lines)

        lines.append(f"{member.mention} has a line in the water.")
        return _join_lines(lines)

    def _session_reactions(self, session: dict) -> list[str]:
        phase = str(session.get("phase", "idle")).strip().lower()
        if phase == "waiting":
            return [SESSION_STOP_EMOJI]
        if phase == "bite":
            return [REEL_EMOJI, SESSION_STOP_EMOJI]
        if phase == "fight":
            return [LIFT_EMOJI, GIVE_EMOJI, SET_EMOJI, SESSION_STOP_EMOJI]
        return []

    async def _refresh_session_message(
        self,
        guild: discord.Guild,
        member: discord.Member,
        *,
        move_to_latest: bool = False,
    ) -> bool:
        channel = get_fishing_channel(guild)
        if channel is None:
            return False
        session = _session_state(guild.id, member.id)
        content = self._render_session_message(guild, member, session)
        msg = await self._fetch_message(channel, _as_int(session.get("session_message_id", 0), 0))
        created = False
        if move_to_latest:
            try:
                new_msg = await channel.send(content)
            except Exception:
                return False
            session["session_message_id"] = int(new_msg.id)
            await self._set_reactions(new_msg, self._session_reactions(session))
            if msg is not None:
                try:
                    await msg.delete()
                except Exception:
                    pass
            return True

        if msg is None:
            try:
                msg = await channel.send(content)
            except Exception:
                return False
            session["session_message_id"] = int(msg.id)
            created = True
        else:
            try:
                await msg.edit(content=content)
            except Exception:
                return False
        await self._set_reactions(msg, self._session_reactions(session))
        return created

    async def _start_fishing(self, guild: discord.Guild, member: discord.Member, *, channel: discord.TextChannel) -> None:
        session = _session_state(guild.id, member.id)
        phase = str(session.get("phase", "idle")).strip().lower()
        if bool(session.get("active", False)) and phase in {"waiting", "bite", "fight"}:
            await channel.send(f"{member.mention} already has a line out. Use {_command_text('fish stop')} if you want to pack it in.")
            return
        bait = get_bait(guild.id, member.id)
        if bait <= 0:
            await channel.send(
                f"{member.mention} is out of bait. Grab **Bait Crate x25** from {_command_text('shop')} first."
            )
            return

        started_new_session = not bool(session.get("active", False))
        if not self._begin_cast(guild, member, session):
            await channel.send(
                f"{member.mention} is out of bait. Grab **Bait Crate x25** from {_command_text('shop')} first."
            )
            return
        if started_new_session:
            record_game_fields(guild.id, member.id, "fishing", sessions_started=1)
        await self._refresh_session_message(guild, member, move_to_latest=True)
        await self._ensure_dock_message(guild, force_refresh=True)
        await save_data()

    async def _stop_fishing(self, guild: discord.Guild, member: discord.Member, *, channel: discord.TextChannel) -> None:
        session = _session_state(guild.id, member.id)
        if not bool(session.get("active", False)) and str(session.get("phase", "idle")).strip().lower() == "idle":
            await channel.send(f"{member.mention} does not have a line out right now.")
            return

        refunded = 0
        if _as_int(session.get("loaded_bait", 0), 0) > 0:
            refunded = 1
            refund_bait(guild.id, member.id, amount=1)
            record_game_fields(guild.id, member.id, "fishing", bait_refunded=1)

        session["active"] = False
        session["phase"] = "idle"
        session["loaded_bait"] = 0
        session["bite_at"] = 0
        session["current_action"] = ""
        session["current_cue"] = ""
        session["fish"] = {}
        session["successes"] = 0
        session["mistakes"] = 0
        session["required"] = 0
        session["max_mistakes"] = 0
        bait = get_bait(guild.id, member.id)
        session["last_result_text"] = (
            f"{DOCK_STOP_EMOJI} {member.mention} packs up the line."
            + (f" Recovered **1** bait." if refunded > 0 else "")
            + f" Bait in tackle box: **{bait}**."
        )
        await self._refresh_session_message(guild, member)
        await self._ensure_dock_message(guild, force_refresh=True)
        await save_data()

    async def _start_reel(self, guild: discord.Guild, member: discord.Member, *, channel: discord.TextChannel) -> None:
        session = _session_state(guild.id, member.id)
        if not bool(session.get("active", False)) or str(session.get("phase", "")).strip().lower() != "bite":
            await channel.send(f"{member.mention} does not have a live bite right now.")
            return

        fish = _as_dict(session.get("fish"))
        if not fish:
            fish = _roll_fish_for_state(guild.id, member.id, str(session.get("cast_state_key", "")).strip().lower())
            session["fish"] = fish
        session["phase"] = "fight"
        session["successes"] = 0
        session["mistakes"] = 0
        session["required"] = _as_int(fish.get("required", 3), 3)
        session["max_mistakes"] = _as_int(fish.get("max_errors", 2), 2)
        self._roll_next_cue(session)
        record_game_fields(guild.id, member.id, "fishing", reels_started=1)
        await self._refresh_session_message(guild, member)
        await save_data()

    async def _continue_fishing(
        self,
        guild: discord.Guild,
        member: discord.Member,
        *,
        result_text: str,
        persist_result: bool = True,
    ) -> None:
        session = _session_state(guild.id, member.id)
        session["loaded_bait"] = 0
        session["fish"] = {}
        session["successes"] = 0
        session["mistakes"] = 0
        session["required"] = 0
        session["max_mistakes"] = 0
        session["current_action"] = ""
        session["current_cue"] = ""
        carry_text = str(result_text or "").strip() if persist_result else ""

        if not bool(session.get("active", False)):
            session["phase"] = "idle"
            session["last_result_text"] = carry_text
            await self._refresh_session_message(guild, member)
            await save_data()
            return

        if self._begin_cast(guild, member, session, carry_text=carry_text):
            await self._refresh_session_message(guild, member)
            await self._ensure_dock_message(guild, force_refresh=True)
            await save_data()
            return

        session["active"] = False
        session["phase"] = "idle"
        session["last_result_text"] = _join_lines(
            [
                carry_text,
                f"Out of bait. Grab another **Bait Crate x25** from {_command_text('shop')} to keep fishing.",
            ]
        )
        await self._refresh_session_message(guild, member)
        await self._ensure_dock_message(guild, force_refresh=True)
        await save_data()

    async def _roll_chest_reward(self, guild: discord.Guild, member: discord.Member) -> str:
        rng = random.Random(f"chest:{guild.id}:{member.id}:{time.time_ns()}")
        reward_key = _weighted_choice(rng, [(str(row["key"]), _as_float(row.get("weight", 1.0), 1.0)) for row in CHEST_LOOT])

        if reward_key == "bonus_spins":
            amount = rng.choice([1, 1, 2, 2, 3])
            total = await _grant_bonus_spins(guild.id, member.id, amount)
            return f"Treasure: **Rust-Scored Wheel Token** -> **+{amount}** bonus wheel spin(s). Bank now **{total}**."
        if reward_key == "bait_cache":
            amount = rng.choice([25, 25, 50, 75])
            total = add_bait(guild.id, member.id, amount=amount)
            return f"Treasure: **Salted Bait Bundle** -> **+{amount}** bait. Tackle box now **{total}**."
        if reward_key == "boost_small":
            boost = await grant_fixed_boost(
                member,
                pct=0.25,
                minutes=90,
                source=CHEST_SOURCE,
                reward_seed_xp=2250,
            )
            return f"Treasure: **Lucky Tackle Charm** -> **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
        if reward_key == "boost_big":
            boost = await grant_fixed_boost(
                member,
                pct=0.50,
                minutes=75,
                source=CHEST_SOURCE,
                reward_seed_xp=3750,
            )
            return f"Treasure: **Captain's Thermos** -> **+{boost['percent']:.1f}% XP/min** for **{boost['minutes']}m**."
        if reward_key == "shield":
            total = add_roulette_backfire_shield(guild.id, member.id, charges=rng.choice([1, 1, 2]))
            return f"Treasure: **Kelp-Wrapped Ward** -> roulette shield charges now **{total}**."
        if reward_key == "aim":
            charges = rng.choice([1, 2])
            state = set_roulette_accuracy_bonus(guild.id, member.id, bonus=0.10, charges=charges)
            return (
                f"Treasure: **Polished Glass Sinker** -> roulette aim **+{state['bonus'] * 100.0:.1f}%** "
                f"for **{state['charges']}** use(s)."
            )
        if reward_key == "cleanse":
            total = add_cleanse_charges(guild.id, member.id, charges=rng.choice([1, 2]))
            return f"Treasure: **Saltbite Cleanser** -> Cleanse charges now **{total}**."
        if reward_key == "natural":
            total = add_blackjack_natural_charges(guild.id, member.id, charges=1)
            return f"Treasure: **Loaded Card Scale** -> blackjack natural charges now **{total}**."
        if reward_key == "mulligan":
            total = add_mulligan_charges(guild.id, member.id, charges=1)
            return f"Treasure: **Driftwood Totem** -> mulligan charges now **{total}**."
        if reward_key == "coinflip_edge":
            state = set_coinflip_win_edge(guild.id, member.id, bonus=0.08, charges=rng.choice([1, 2]))
            return (
                f"Treasure: **Tideworn Coin** -> coinflip edge **+{state['bonus'] * 100.0:.1f}%** "
                f"for **{state['charges']}** use(s)."
            )
        if reward_key == "lotto_bonus":
            state = set_lotto_bonus_tickets_pct(guild.id, member.id, pct=0.50, charges=1)
            return (
                f"Treasure: **Numbered Map Scrap** -> lotto bonus tickets **+{state['pct'] * 100.0:.0f}%** "
                f"for **{state['charges']}** buy(s)."
            )
        state = set_lotto_jackpot_boost_multiplier(guild.id, member.id, multiplier=1.50, charges=1)
        return (
            f"Treasure: **Jackpot Pearl** -> lotto jackpot amp **x{state['multiplier']:.2f}** "
            f"for **{state['charges']}** jackpot(s)."
        )

    async def _resolve_catch(self, guild: discord.Guild, member: discord.Member) -> None:
        session = _session_state(guild.id, member.id)
        fish = _as_dict(session.get("fish"))
        if not fish:
            await self._continue_fishing(guild, member, result_text=f"{member.mention} somehow reeled in nothing at all.")
            return

        reward_xp = max(1, _as_int(fish.get("reward_xp", 1), 1))
        await apply_xp_change(member, reward_xp, source=FISHING_SOURCE)

        rarity = str(fish.get("rarity", "common")).strip().lower()
        rarity_field = f"rarity_{rarity}"
        size_label = str(fish.get("size_label", "Solid")).strip().lower()
        size_field = f"size_{size_label.replace(' ', '_')}"
        fields = {
            "catches": 1,
            "xp_awarded_total": reward_xp,
            rarity_field: 1,
            size_field: 1,
        }
        if _as_int(session.get("mistakes", 0), 0) <= 0:
            fields["perfect_reels"] = 1
        record_game_fields(guild.id, member.id, "fishing", **fields)

        result_bits = [
            (
                f"**Catch:** {member.mention} lands a {fish.get('rarity_emoji', '')} "
                f"**{fish.get('rarity_label', 'Fish')} {fish.get('name', 'Fish')}** "
                f"({fish.get('size_label', 'Solid')}) at **{_as_float(fish.get('length_in', 0.0), 0.0):.1f} in** "
                f"and **{_as_float(fish.get('weight_lb', 0.0), 0.0):.1f} lb**."
            ),
            f"Reward: **+{_fmt_num(reward_xp)} XP**.",
        ]

        water = WATER_STATES.get(str(session.get("cast_state_key", "")).strip().lower(), WATER_STATES["empty_reach"])
        chest_chance = max(0.0, min(0.40, BASE_CHEST_CHANCE + _as_float(water.get("chest_bonus", 0.0), 0.0)))
        if random.random() <= chest_chance:
            chest_text = await self._roll_chest_reward(guild, member)
            record_game_fields(guild.id, member.id, "fishing", chests_found=1)
            result_bits.append(chest_text)

        channel = get_fishing_channel(guild)
        if channel is not None:
            try:
                await channel.send(_join_lines(result_bits))
            except Exception:
                pass

        await self._continue_fishing(guild, member, result_text="", persist_result=False)

    async def _resolve_escape(self, guild: discord.Guild, member: discord.Member) -> None:
        session = _session_state(guild.id, member.id)
        fish = _as_dict(session.get("fish"))
        name = str(fish.get("name", "fish")).strip() or "fish"
        rarity_label = str(fish.get("rarity_label", "shady")).strip()
        record_game_fields(guild.id, member.id, "fishing", escapes=1)
        await self._continue_fishing(
            guild,
            member,
            result_text=(
                f"**Escape:** {member.mention} loses a **{rarity_label} {name}** after the line slips out of rhythm."
            ),
        )

    async def _apply_fight_action(
        self,
        guild: discord.Guild,
        member: discord.Member,
        action: str,
        *,
        channel: discord.TextChannel,
    ) -> None:
        session = _session_state(guild.id, member.id)
        if not bool(session.get("active", False)) or str(session.get("phase", "")).strip().lower() != "fight":
            await channel.send(f"{member.mention} is not currently in a reel fight.")
            return

        choice = str(action).strip().lower()
        correct = str(session.get("current_action", "")).strip().lower()
        if choice == correct:
            session["successes"] = _as_int(session.get("successes", 0), 0) + 1
        else:
            session["mistakes"] = _as_int(session.get("mistakes", 0), 0) + 1

        if _as_int(session.get("successes", 0), 0) >= _as_int(session.get("required", 0), 0):
            await self._resolve_catch(guild, member)
            return
        if _as_int(session.get("mistakes", 0), 0) >= _as_int(session.get("max_mistakes", 0), 0):
            await self._resolve_escape(guild, member)
            return

        self._roll_next_cue(session)
        await self._refresh_session_message(guild, member)
        await save_data()

    async def _show_status(self, ctx: commands.Context) -> None:
        session = _session_state(ctx.guild.id, ctx.author.id)
        lines = [f"**Fishing Status for {ctx.author.display_name}**"]
        lines.extend(self._status_summary(ctx.guild, ctx.author, session))
        lines.append(
            "Use "
            f"{_command_text('fish cast', prefix=ctx.clean_prefix)}, "
            f"{_command_text('fish reel', prefix=ctx.clean_prefix)}, "
            f"{_command_text('fish lift', prefix=ctx.clean_prefix)}, "
            f"{_command_text('fish give', prefix=ctx.clean_prefix)}, "
            f"{_command_text('fish set', prefix=ctx.clean_prefix)}, "
            f"or {_command_text('fish stop', prefix=ctx.clean_prefix)}."
        )
        await ctx.reply("\n".join(lines))

    async def _ensure_guild_runtime(self, guild: discord.Guild, *, refresh_dock: bool = False) -> None:
        await ensure_guild_setup(guild)
        controls_changed = await self._ensure_controls_message(guild)
        state_changed = _roll_water_state(guild.id)
        dock_changed = await self._ensure_dock_message(guild, force_refresh=refresh_dock or state_changed)
        if controls_changed or state_changed or dock_changed:
            await save_data()

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                await self._ensure_guild_runtime(guild, refresh_dock=True)
            except Exception:
                continue
        if not self.fishing_loop.is_running():
            self.fishing_loop.start()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._ensure_guild_runtime(guild, refresh_dock=True)

    @tasks.loop(seconds=FISH_LOOP_SECONDS)
    async def fishing_loop(self):
        for guild in list(self.bot.guilds):
            try:
                st = _state_root(guild.id)
                state_changed = _roll_water_state(guild.id)
                guild_changed = state_changed
                timer_minutes = _dock_timer_minutes(_as_int(st.get("state_expires_at", 0), 0))
                timer_changed = timer_minutes != _as_int(st.get("dock_timer_minutes", -1), -1)
                if state_changed:
                    guild_changed = bool(await self._ensure_dock_message(guild, force_refresh=True)) or guild_changed
                elif timer_changed:
                    guild_changed = bool(
                        await self._ensure_dock_message(guild, force_refresh=True, move_to_latest=True)
                    ) or guild_changed

                for uid, session in _active_sessions(guild.id):
                    if not bool(session.get("active", False)):
                        continue
                    if str(session.get("phase", "")).strip().lower() != "waiting":
                        continue
                    if _as_int(session.get("bite_at", 0), 0) > _now_ts():
                        continue
                    member = guild.get_member(uid)
                    if member is None or member.bot:
                        continue
                    session["phase"] = "bite"
                    session["bite_at"] = 0
                    session["fish"] = _roll_fish_for_state(guild.id, uid, str(session.get("cast_state_key", "")).strip().lower())
                    session["last_result_text"] = ""
                    record_game_fields(guild.id, uid, "fishing", bites=1)
                    await self._refresh_session_message(guild, member)
                    guild_changed = True

                if guild_changed:
                    await save_data()
            except Exception:
                continue

    @fishing_loop.before_loop
    async def _before_fishing_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        guild = self.bot.get_guild(payload.guild_id or 0)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        member = payload.member if isinstance(payload.member, discord.Member) else guild.get_member(payload.user_id)
        if member is None or member.bot:
            return

        await self._remove_user_reaction(channel, payload.message_id, payload.emoji, member)
        emoji = _norm_emoji_name(payload.emoji)

        dock_id = _as_int(_state_root(guild.id).get("dock_message_id", 0), 0)
        if payload.message_id == dock_id and channel.id == get_fishing_channel_id(guild.id):
            if emoji == _norm_emoji_name(DOCK_CAST_EMOJI):
                await self._start_fishing(guild, member, channel=channel)
            elif emoji == _norm_emoji_name(DOCK_STOP_EMOJI):
                await self._stop_fishing(guild, member, channel=channel)
            return

        session = _session_state(guild.id, member.id)
        if payload.message_id != _as_int(session.get("session_message_id", 0), 0):
            return

        phase = str(session.get("phase", "idle")).strip().lower()
        if emoji == _norm_emoji_name(SESSION_STOP_EMOJI):
            await self._stop_fishing(guild, member, channel=channel)
            return
        if phase == "bite" and emoji == _norm_emoji_name(REEL_EMOJI):
            await self._start_reel(guild, member, channel=channel)
            return
        if phase == "fight":
            if emoji == _norm_emoji_name(LIFT_EMOJI):
                await self._apply_fight_action(guild, member, "lift", channel=channel)
            elif emoji == _norm_emoji_name(GIVE_EMOJI):
                await self._apply_fight_action(guild, member, "give", channel=channel)
            elif emoji == _norm_emoji_name(SET_EMOJI):
                await self._apply_fight_action(guild, member, "set", channel=channel)

    @commands.command(name="fish")
    async def fish(self, ctx: commands.Context, action: Optional[str] = None):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        fishing_channel_id = int(get_fishing_channel_id(ctx.guild.id) or 0)
        if fishing_channel_id > 0 and ctx.channel.id != fishing_channel_id:
            ch = ctx.guild.get_channel(fishing_channel_id)
            if isinstance(ch, discord.TextChannel):
                await ctx.reply(f"Run this in {ch.mention}.")
            else:
                await ctx.reply("Run this in the fishing channel.")
            return

        await self._ensure_guild_runtime(ctx.guild, refresh_dock=False)
        act = str(action or "").strip().lower()

        if not act or act in {"status", "state"}:
            await self._show_status(ctx)
            return
        if act in {"cast", "start"}:
            await self._start_fishing(ctx.guild, ctx.author, channel=ctx.channel)
            return
        if act == "stop":
            await self._stop_fishing(ctx.guild, ctx.author, channel=ctx.channel)
            return
        if act == "reel":
            await self._start_reel(ctx.guild, ctx.author, channel=ctx.channel)
            return
        if act in {"lift", "give", "set"}:
            await self._apply_fight_action(ctx.guild, ctx.author, act, channel=ctx.channel)
            return

        await ctx.reply(
            f"Usage: `{ctx.clean_prefix}fish [cast|reel|lift|give|set|stop|status]`"
        )

    @commands.command(name="fishreroll")
    @owner_only()
    async def fish_reroll(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        st = _state_root(ctx.guild.id)
        st["state_expires_at"] = 0
        _roll_water_state(ctx.guild.id)
        await self._ensure_dock_message(ctx.guild, force_refresh=True)
        await save_data()
        state = _water_state(_state_root(ctx.guild.id))
        await ctx.reply(f"Fishing water rerolled to **{state.get('name', 'Unknown Water')}**.")
