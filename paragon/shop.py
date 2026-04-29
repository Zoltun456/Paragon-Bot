from __future__ import annotations

from difflib import get_close_matches
import math
import re
from typing import Optional

from discord.ext import commands

from .config import (
    SHOP_CLEANSE_START_PCT,
    SHOP_CLEANSE_STEP_GROWTH_PCT,
    SHOP_CLEANSE_STEP_PCT,
    SHOP_COST_ROUND_STEP,
    SHOP_ROULETTE_ACCURACY_BONUS_CHANCE,
    SHOP_ROULETTE_ACCURACY_START_PCT,
    SHOP_ROULETTE_ACCURACY_STEP_GROWTH_PCT,
    SHOP_ROULETTE_ACCURACY_STEP_PCT,
    SHOP_ROULETTE_SHIELD_START_PCT,
    SHOP_ROULETTE_SHIELD_STEP_GROWTH_PCT,
    SHOP_ROULETTE_SHIELD_STEP_PCT,
    SHOP_WHEEL_SPIN_START_PCT,
    SHOP_WHEEL_SPIN_STEP_GROWTH_PCT,
    SHOP_WHEEL_SPIN_STEP_PCT,
    SPIN_RESET_HOUR,
    SPIN_RESET_MINUTE,
)
from .spin import (
    _add_bonus_spins,
    _available_spins,
    _cycle_key,
    _sanitize_reset_time,
    _spin_user_state,
    _sync_spin_cycle_state,
    _wheel_state,
)
from .spin_support import (
    add_cleanse_charges,
    add_roulette_backfire_shield,
    set_roulette_accuracy_bonus,
)
from .stats_store import record_game_fields
from .storage import _udict, save_data
from .xp import apply_xp_change, prestige_cost


def _fmt_pct(value: float) -> str:
    pct = float(value)
    if abs(pct - round(pct)) < 1e-9:
        return f"{int(round(pct))}%"
    return f"{pct:.1f}%"


SHOP_ITEMS: list[dict[str, object]] = [
    {
        "key": "wheel_spin",
        "name": "Wheel Spin",
        "aliases": ["wheel", "spin", "wheelspin"],
        "description": (
            f"Adds 1 bonus wheel spin. Starts at {_fmt_pct(SHOP_WHEEL_SPIN_START_PCT)} "
            f"of your next prestige and ramps harder each purchase every reset, "
            f"rounded to the nearest {max(1, int(SHOP_COST_ROUND_STEP)):,} XP."
        ),
    },
    {
        "key": "cleanse",
        "name": "Cleanse",
        "aliases": ["debuff_cleanse", "debuffs", "cleanse_item"],
        "description": (
            "Adds 1 Cleanse charge. Use `!cleanse` to remove all current debuffs. "
            f"Starts at {_fmt_pct(SHOP_CLEANSE_START_PCT)} of your next prestige and "
            "ramps each purchase every reset."
        ),
    },
    {
        "key": "roulette_shield",
        "name": "Roulette Shield",
        "aliases": ["shield", "roulette_backfire_shield", "backfire_shield"],
        "description": (
            "Adds 1 roulette backfire shield. Starts at "
            f"{_fmt_pct(SHOP_ROULETTE_SHIELD_START_PCT)} of your next prestige and "
            "ramps each purchase every reset."
        ),
    },
    {
        "key": "roulette_accuracy",
        "name": "Roulette Accuracy",
        "aliases": ["aim", "roulette_aim", "accuracy"],
        "description": (
            "Adds 1 roulette aim charge for "
            f"+{_fmt_pct(SHOP_ROULETTE_ACCURACY_BONUS_CHANCE * 100.0)} absolute "
            "success chance on your next roulette shot. Starts at "
            f"{_fmt_pct(SHOP_ROULETTE_ACCURACY_START_PCT)} of your next prestige "
            "and ramps each purchase every reset."
        ),
    },
]

SHOP_ITEM_CURVES: dict[str, dict[str, int]] = {
    "wheel_spin": {
        "start_pct": SHOP_WHEEL_SPIN_START_PCT,
        "step_pct": SHOP_WHEEL_SPIN_STEP_PCT,
        "step_growth_pct": SHOP_WHEEL_SPIN_STEP_GROWTH_PCT,
    },
    "cleanse": {
        "start_pct": SHOP_CLEANSE_START_PCT,
        "step_pct": SHOP_CLEANSE_STEP_PCT,
        "step_growth_pct": SHOP_CLEANSE_STEP_GROWTH_PCT,
    },
    "roulette_shield": {
        "start_pct": SHOP_ROULETTE_SHIELD_START_PCT,
        "step_pct": SHOP_ROULETTE_SHIELD_STEP_PCT,
        "step_growth_pct": SHOP_ROULETTE_SHIELD_STEP_GROWTH_PCT,
    },
    "roulette_accuracy": {
        "start_pct": SHOP_ROULETTE_ACCURACY_START_PCT,
        "step_pct": SHOP_ROULETTE_ACCURACY_STEP_PCT,
        "step_growth_pct": SHOP_ROULETTE_ACCURACY_STEP_GROWTH_PCT,
    },
}


def _round_to_shop_step(value: float) -> int:
    step = max(1, int(SHOP_COST_ROUND_STEP))
    return int(max(0, step * math.floor((float(value) / float(step)) + 0.5)))


def _shop_cycle(gid: int) -> str:
    wheel_state = _wheel_state(gid)
    h, m = _sanitize_reset_time(
        wheel_state.get("reset_hour", SPIN_RESET_HOUR),
        wheel_state.get("reset_minute", SPIN_RESET_MINUTE),
    )
    return _cycle_key(h, m)


def _shop_state(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("shop_daily")
    if not isinstance(st, dict):
        st = {}
        u["shop_daily"] = st
    st.setdefault("cycle_key", "")
    for item in SHOP_ITEMS:
        counter_key = _shop_buy_counter_key(str(item.get("key", "")).strip().lower())
        st.setdefault(counter_key, 0)
    return st


def _shop_buy_counter_key(item_key: str) -> str:
    return f"{str(item_key).strip().lower()}_buys"


def _sync_shop_cycle_state(st: dict, cycle: str) -> None:
    if str(st.get("cycle_key", "")) != cycle:
        st["cycle_key"] = cycle
        for item in SHOP_ITEMS:
            st[_shop_buy_counter_key(str(item.get("key", "")).strip().lower())] = 0
        return

    for item in SHOP_ITEMS:
        counter_key = _shop_buy_counter_key(str(item.get("key", "")).strip().lower())
        st[counter_key] = max(0, int(st.get(counter_key, 0)))


def _shop_item_cost_percent(item_key: str, purchase_number: int) -> int:
    n = max(1, int(purchase_number))
    curve = SHOP_ITEM_CURVES.get(str(item_key).strip().lower(), {})
    start_pct = max(0, int(curve.get("start_pct", 0)))
    step_pct = max(0, int(curve.get("step_pct", 0)))
    step_growth_pct = max(0, int(curve.get("step_growth_pct", 0)))
    prior_buys = max(0, n - 1)
    return start_pct + (prior_buys * step_pct) + (
        step_growth_pct * prior_buys * max(0, prior_buys - 1) // 2
    )


def _shop_item_costs(item: dict[str, object], gid: int, uid: int, amount: int = 1) -> list[int]:
    buy_count = max(0, int(amount))
    if buy_count <= 0:
        return []

    item_key = str(item.get("key", "")).strip().lower()
    u = _udict(gid, uid)
    p = int(u.get("prestige", 0))
    base_cost = max(1, int(prestige_cost(p)))

    shop_state = _shop_state(gid, uid)
    _sync_shop_cycle_state(shop_state, _shop_cycle(gid))
    counter_key = _shop_buy_counter_key(item_key)
    bought_this_cycle = max(0, int(shop_state.get(counter_key, 0)))

    costs: list[int] = []
    for offset in range(buy_count):
        purchase_number = bought_this_cycle + offset + 1
        pct = _shop_item_cost_percent(item_key, purchase_number) / 100.0
        costs.append(_round_to_shop_step(float(base_cost) * pct))
    return costs


def _shop_item_cost(item: dict[str, object], gid: int, uid: int) -> int:
    key = str(item.get("key", "")).strip().lower()
    if key in SHOP_ITEM_CURVES:
        return sum(_shop_item_costs(item, gid, uid, 1))
    return max(0, int(item.get("cost", 0)))


def _normalize_shop_text(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(raw or "").strip().lower()).strip()


def _resolve_shop_item(query: str) -> Optional[dict[str, object]]:
    q = _normalize_shop_text(query)
    if not q:
        return None
    if q.isdigit():
        idx = int(q) - 1
        if 0 <= idx < len(SHOP_ITEMS):
            return SHOP_ITEMS[idx]
        return None

    exact_map: dict[str, dict[str, object]] = {}
    fuzzy_labels: list[str] = []
    fuzzy_map: dict[str, dict[str, object]] = {}
    for item in SHOP_ITEMS:
        labels = [
            _normalize_shop_text(str(item.get("key", ""))),
            _normalize_shop_text(str(item.get("name", ""))),
        ]
        for alias in item.get("aliases", []) or []:
            labels.append(_normalize_shop_text(str(alias)))
        labels = [label for label in labels if label]
        for label in labels:
            exact_map.setdefault(label, item)
            fuzzy_labels.append(label)
            fuzzy_map[label] = item

    if q in exact_map:
        return exact_map[q]

    substring_matches = []
    q_tokens = q.split()
    for label, item in fuzzy_map.items():
        label_tokens = label.split()
        if q in label or label.startswith(q) or all(token in label_tokens for token in q_tokens):
            substring_matches.append((len(label), label, item))
    if substring_matches:
        substring_matches.sort(key=lambda row: (row[0], row[1]))
        return substring_matches[0][2]

    close = get_close_matches(q, fuzzy_labels, n=1, cutoff=0.55)
    if close:
        return fuzzy_map.get(close[0])
    return None


class ShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="shop")
    async def shop(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return

        lines = ["**Shop**"]
        for idx, item in enumerate(SHOP_ITEMS, start=1):
            cost = _shop_item_cost(item, ctx.guild.id, ctx.author.id)
            extra = ""
            key = str(item.get("key", "")).strip().lower()
            if key in SHOP_ITEM_CURVES:
                shop_state = _shop_state(ctx.guild.id, ctx.author.id)
                _sync_shop_cycle_state(shop_state, _shop_cycle(ctx.guild.id))
                counter_key = _shop_buy_counter_key(key)
                next_buy = max(0, int(shop_state.get(counter_key, 0))) + 1
                next_pct = _shop_item_cost_percent(key, next_buy)
                extra = f" Next buy: **#{next_buy}** at **{next_pct}%**."
            lines.append(
                f"`{idx}.` **{item['name']}** - **{cost} XP** - {item['description']}{extra}"
            )
        lines.append(f"Buy with `{ctx.clean_prefix}buy <index|name> [amount]`.")
        await ctx.reply("\n".join(lines))

    @commands.command(name="buy")
    async def buy(self, ctx: commands.Context, *args: str):
        if ctx.guild is None:
            await ctx.reply("This command can only be used in a server.")
            return
        if not args:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}buy <index|name> [amount]`")
            return

        tokens = [str(arg).strip() for arg in args if str(arg).strip()]
        if not tokens:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}buy <index|name> [amount]`")
            return

        amount = 1
        query_tokens = list(tokens)
        if len(tokens) >= 2:
            try:
                amount = int(tokens[-1])
                query_tokens = tokens[:-1]
            except ValueError:
                amount = 1
                query_tokens = list(tokens)

        if amount <= 0:
            await ctx.reply("Buy amount must be at least 1.")
            return

        query = " ".join(query_tokens).strip()
        if not query:
            await ctx.reply(f"Usage: `{ctx.clean_prefix}buy <index|name> [amount]`")
            return

        item = _resolve_shop_item(query)
        if not item:
            await ctx.reply(f"I couldn't find a shop item matching `{query}`.")
            return

        key = str(item.get("key", "")).strip().lower()
        if key in SHOP_ITEM_CURVES:
            per_item_costs = _shop_item_costs(item, ctx.guild.id, ctx.author.id, amount)
        else:
            per_item_costs = [_shop_item_cost(item, ctx.guild.id, ctx.author.id)] * amount

        total_cost = max(0, sum(per_item_costs))
        u = _udict(ctx.guild.id, ctx.author.id)
        cur_xp = int(u.get("xp_f", u.get("xp", 0)))
        if cur_xp < total_cost:
            await ctx.reply(
                f"You need **{total_cost} XP** to buy **{amount}x {item['name']}**, "
                f"but you only have **{cur_xp} XP**."
            )
            return

        if key not in SHOP_ITEM_CURVES:
            await ctx.reply(f"`{item['name']}` is not purchasable yet.")
            return

        shop_state = _shop_state(ctx.guild.id, ctx.author.id)
        _sync_shop_cycle_state(shop_state, _shop_cycle(ctx.guild.id))
        await apply_xp_change(ctx.author, -total_cost, source=f"shop {key}")

        effect_text = ""
        if key == "wheel_spin":
            wheel_state = _wheel_state(ctx.guild.id)
            h, m = _sanitize_reset_time(
                wheel_state.get("reset_hour", SPIN_RESET_HOUR),
                wheel_state.get("reset_minute", SPIN_RESET_MINUTE),
            )
            ust = _spin_user_state(ctx.guild.id, ctx.author.id)
            _sync_spin_cycle_state(ust, _cycle_key(h, m))
            bonus_total = _add_bonus_spins(ust, amount)
            effect_text = (
                f"Bonus spin bank: **{bonus_total}** | Total spins available now: **{_available_spins(ust)}**."
            )
        elif key == "cleanse":
            charges = add_cleanse_charges(ctx.guild.id, ctx.author.id, charges=amount)
            effect_text = f"Cleanse charges now: **{charges}**."
        elif key == "roulette_shield":
            charges = add_roulette_backfire_shield(ctx.guild.id, ctx.author.id, charges=amount)
            effect_text = f"Roulette shield charges now: **{charges}**."
        elif key == "roulette_accuracy":
            state = set_roulette_accuracy_bonus(
                ctx.guild.id,
                ctx.author.id,
                bonus=SHOP_ROULETTE_ACCURACY_BONUS_CHANCE,
                charges=amount,
            )
            effect_text = (
                f"Roulette aim bonus queued: **+{state['bonus'] * 100.0:.1f}%** "
                f"for **{state['charges']}** use(s)."
            )

        counter_key = _shop_buy_counter_key(key)
        shop_state[counter_key] = max(0, int(shop_state.get(counter_key, 0))) + amount
        record_game_fields(
            ctx.guild.id,
            ctx.author.id,
            "shop",
            purchases=amount,
            spent_total=total_cost,
            buy_commands=1,
        )
        await save_data()

        first_cost = per_item_costs[0] if per_item_costs else 0
        last_cost = per_item_costs[-1] if per_item_costs else 0
        next_buy = max(0, int(shop_state.get(counter_key, 0))) + 1
        next_cost = sum(_shop_item_costs(item, ctx.guild.id, ctx.author.id, 1))
        await ctx.reply(
            f"Bought **{amount}x {item['name']}** for **{total_cost} XP**. "
            f"Cost curve this purchase: **{first_cost} -> {last_cost} XP**. "
            f"Next buy: **#{next_buy}** for **{next_cost} XP**. "
            f"{effect_text}"
        )
