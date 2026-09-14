import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.shop import SHOP_ITEMS, ShopCog, _max_affordable_shop_amount


class ShopMaxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = {"xp_f": 90, "prestige": 0}
        self.shop_state = {"cycle_key": "test-cycle", "bait_crate_buys": 0}

    def test_max_amount_includes_a_free_purchase_and_exact_budget(self):
        bait_crate = SHOP_ITEMS[1]
        with (
            patch("paragon.shop._udict", return_value=self.user),
            patch("paragon.shop._shop_state", return_value=self.shop_state),
            patch("paragon.shop._shop_cycle", return_value="test-cycle"),
            patch("paragon.shop.prestige_passive_rate", return_value=1.0),
        ):
            amount = _max_affordable_shop_amount(bait_crate, 1, 2, 90)

        self.assertEqual(amount, 3)

    async def test_buy_max_purchases_the_affordable_amount(self):
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=2),
            clean_prefix="!",
            reply=AsyncMock(),
        )
        cog = ShopCog(SimpleNamespace())

        with (
            patch("paragon.shop._udict", return_value=self.user),
            patch("paragon.shop._shop_state", return_value=self.shop_state),
            patch("paragon.shop._shop_cycle", return_value="test-cycle"),
            patch("paragon.shop.prestige_passive_rate", return_value=1.0),
            patch("paragon.shop.apply_xp_change", new=AsyncMock()) as apply_xp_change,
            patch("paragon.shop.add_bait", return_value=75) as add_bait,
            patch("paragon.shop.record_game_fields") as record_game_fields,
            patch("paragon.shop.save_data", new=AsyncMock()),
        ):
            await ShopCog.buy.callback(cog, ctx, "2", "max")

        apply_xp_change.assert_awaited_once_with(
            ctx.author, -90, source="shop bait_crate", persist=False
        )
        add_bait.assert_called_once_with(1, 2, amount=75)
        record_game_fields.assert_called_once_with(
            1,
            2,
            "shop",
            purchases=3,
            spent_total=90,
            buy_commands=1,
        )
        self.assertEqual(self.shop_state["bait_crate_buys"], 3)
        self.assertIn("Bought **3x Bait Crate x25** for **90 XP**", ctx.reply.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
