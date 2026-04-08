from __future__ import annotations

from difflib import get_close_matches
import math
import re
from typing import Optional

from discord.ext import commands

from .config import SPIN_RESET_HOUR, SPIN_RESET_MINUTE
from .spin import (
    _add_bonus_spins,
    _available_spins,
    _cycle_key,
    _sanitize_reset_time,
    _spin_user_state,
    _sync_spin_cycle_state,
    _wheel_state,
)
from .storage import _udict, save_data
from .xp import apply_xp_change, prestige_cost


SHOP_ITEMS: list[dict[str, object]] = [
    {
        "key": "wheel_spin",
        "name": "Wheel Spin",
        "aliases": ["wheel", "spin", "wheelspin"],
        "description": "Adds 1 bonus wheel spin. Costs 20% of your next prestige, rounded to the nearest 10 XP.",
    },
]


def _round_to_nearest_10(value: float) -> int:
    return int(max(0, 10 * math.floor((float(value) / 10.0) + 0.5)))


def _shop_item_cost(item: dict[str, object], gid: int, uid: int) -> int:
    key = str(item.get("key", "")).strip().lower()
    if key == "wheel_spin":
        u = _udict(gid, uid)
        p = int(u.get("prestige", 0))
        return _round_to_nearest_10(float(prestige_cost(p)) * 0.20)
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
            lines.append(
                f"`{idx}.` **{item['name']}** - **{cost} XP** - {item['description']}"
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

        cost_each = _shop_item_cost(item, ctx.guild.id, ctx.author.id)
        total_cost = max(0, cost_each * amount)
        u = _udict(ctx.guild.id, ctx.author.id)
        cur_xp = int(u.get("xp_f", u.get("xp", 0)))
        if cur_xp < total_cost:
            await ctx.reply(
                f"You need **{total_cost} XP** to buy **{amount}x {item['name']}**, "
                f"but you only have **{cur_xp} XP**."
            )
            return

        key = str(item.get("key", "")).strip().lower()
        if key != "wheel_spin":
            await ctx.reply(f"`{item['name']}` is not purchasable yet.")
            return

        await apply_xp_change(ctx.author, -total_cost, source="shop wheel_spin")
        wheel_state = _wheel_state(ctx.guild.id)
        h, m = _sanitize_reset_time(
            wheel_state.get("reset_hour", SPIN_RESET_HOUR),
            wheel_state.get("reset_minute", SPIN_RESET_MINUTE),
        )
        ust = _spin_user_state(ctx.guild.id, ctx.author.id)
        _sync_spin_cycle_state(ust, _cycle_key(h, m))
        bonus_total = _add_bonus_spins(ust, amount)
        await save_data()

        await ctx.reply(
            f"Bought **{amount}x {item['name']}** for **{total_cost} XP**. "
            f"Bonus spin bank: **{bonus_total}** | Total spins available now: **{_available_spins(ust)}**."
        )
