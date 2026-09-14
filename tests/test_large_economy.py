import os
import time
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.prestige import PrestigeCog
from paragon.shop import ShopCog, _shop_bulk_cost
from paragon.spin import SpinCog
from paragon.xp import apply_xp_delta_to_user, prestige_bulk_cost


EXAMPLE_XP = int(
    "468167635465783376305833339612081930418764142391856540212072426891826706299986116691886080"
)


class LargeEconomyTests(unittest.IsolatedAsyncioTestCase):
    def test_integer_balance_deduction_is_exact(self):
        user = {"xp": EXAMPLE_XP, "xp_f": float(EXAMPLE_XP)}
        cost = _shop_bulk_cost("wheel_spin", 0, 100_000_000, 73_056.0)

        applied = apply_xp_delta_to_user(user, -cost)

        self.assertEqual(applied, -cost)
        self.assertEqual(user["xp"], EXAMPLE_XP - cost)
        self.assertIsInstance(user["xp_f"], int)

    def test_hundred_million_shop_cost_is_constant_time(self):
        started = time.perf_counter()
        cost = _shop_bulk_cost("wheel_spin", 0, 100_000_000, 73_056.0)
        elapsed = time.perf_counter() - started

        self.assertEqual(cost, 730560032875201351536000000000)
        self.assertLess(elapsed, 1.0)

    async def test_hundred_million_buy_command_updates_balance_and_inventory(self):
        guild = SimpleNamespace(id=101)
        author = SimpleNamespace(id=202, guild=guild)
        ctx = SimpleNamespace(guild=guild, author=author, clean_prefix="?", reply=AsyncMock())
        user = {"xp": EXAMPLE_XP, "xp_f": EXAMPLE_XP, "prestige": 3800, "stats": {}}
        shop_state = {"cycle_key": "cycle", "wheel_spin_buys": 0}
        spin_state = {"daily_spins_remaining": 0, "bonus_spins": 0}
        expected_cost = _shop_bulk_cost("wheel_spin", 0, 100_000_000, 73_056.0)

        with (
            patch("paragon.shop._udict", return_value=user),
            patch("paragon.xp._udict", return_value=user),
            patch("paragon.shop._shop_state", return_value=shop_state),
            patch("paragon.shop._shop_cycle", return_value="cycle"),
            patch("paragon.shop._spin_user_state", return_value=spin_state),
            patch("paragon.shop.prestige_passive_rate", return_value=73_056.0),
            patch("paragon.shop.record_game_fields"),
            patch("paragon.shop.save_data", new=AsyncMock()),
        ):
            started = time.perf_counter()
            await ShopCog.buy.callback(ShopCog(SimpleNamespace()), ctx, "1", "100000000")
            elapsed = time.perf_counter() - started

        self.assertEqual(user["xp"], EXAMPLE_XP - expected_cost)
        self.assertEqual(spin_state["bonus_spins"], 100_000_000)
        self.assertEqual(shop_state["wheel_spin_buys"], 100_000_000)
        self.assertLess(elapsed, 1.0)

    def test_hundred_million_rolls_are_aggregated(self):
        cog = SpinCog(SimpleNamespace())
        state = {"reward_overrides": {}}
        started = time.perf_counter()

        counts = cog._draw_reward_counts(state, 100_000_000)

        self.assertEqual(sum(counts.values()), 100_000_000)
        self.assertLessEqual(len(counts), 22)
        self.assertLess(time.perf_counter() - started, 1.0)

    async def test_bonus_spin_reward_does_not_extend_current_sweep(self):
        cog = SpinCog(SimpleNamespace())
        state = {"daily_spins_remaining": 0, "bonus_spins": 10}
        consumed = cog._consume_spin_count(state, 10)
        self.assertEqual(consumed, 10)
        self.assertEqual(state["bonus_spins"], 0)

        guild = SimpleNamespace(id=5)
        author = SimpleNamespace(id=6, guild=guild)
        ctx = SimpleNamespace(guild=guild, author=author)
        user = {"xp": 0, "xp_f": 0, "prestige": 0, "stats": {}}
        with (
            patch("paragon.spin._udict", return_value=user),
            patch("paragon.spin.record_game_fields"),
        ):
            totals = await cog._apply_reward_counts(
                ctx, Counter({"bonus_spins_2": 10}), state, "cycle"
            )

        self.assertEqual(totals["bonus_spins_gained"], 20)
        self.assertEqual(state["bonus_spins"], 20)

    def test_prestige_all_handles_astronomical_balance(self):
        cog = PrestigeCog(SimpleNamespace())
        started = time.perf_counter()
        count, spent, remaining = cog._max_affordable_prestiges(EXAMPLE_XP, 3800)

        self.assertGreater(count, 0)
        self.assertLessEqual(spent, EXAMPLE_XP)
        self.assertEqual(remaining, EXAMPLE_XP - spent)
        self.assertGreater(prestige_bulk_cost(3800, count + 1), EXAMPLE_XP)
        self.assertLess(time.perf_counter() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
