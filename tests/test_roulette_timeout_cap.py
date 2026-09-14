import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.roulette import (
    DISCORD_MAX_TIMEOUT_SECONDS,
    RouletteCog,
    _capped_timeout_with_bonus,
    _timeout_member,
)
from paragon.core import CoreCog
from paragon.spin_support import consume_roulette_timeout_bonus_seconds


class RouletteTimeoutCapTests(unittest.IsolatedAsyncioTestCase):
    def test_huge_bonus_uses_only_enough_to_reach_discord_cap(self):
        huge_bonus = 10**100

        total, used = _capped_timeout_with_bonus(60, huge_bonus)

        self.assertEqual(total, DISCORD_MAX_TIMEOUT_SECONDS)
        self.assertEqual(used, DISCORD_MAX_TIMEOUT_SECONDS - 60)

    def test_partial_bonus_consumption_preserves_remainder(self):
        buffs = {"roulette_timeout_bonus_seconds": 10**100}

        with patch("paragon.spin_support._wheel_buffs", return_value=buffs):
            consumed = consume_roulette_timeout_bonus_seconds(1, 2, seconds=1234)

        self.assertEqual(consumed, 1234)
        self.assertEqual(buffs["roulette_timeout_bonus_seconds"], (10**100) - 1234)

    async def test_timeout_api_helper_defensively_caps_huge_values(self):
        member = SimpleNamespace(timeout=AsyncMock())
        before = datetime.now(timezone.utc)

        applied = await _timeout_member(member, 10**100, "test")

        self.assertTrue(applied)
        until = member.timeout.await_args.args[0]
        self.assertGreaterEqual(until, before + timedelta(seconds=DISCORD_MAX_TIMEOUT_SECONDS - 1))
        self.assertLessEqual(until, before + timedelta(seconds=DISCORD_MAX_TIMEOUT_SECONDS + 1))

    async def test_successful_roulette_caps_timeout_and_consumes_partial_bonus(self):
        guild = SimpleNamespace(id=10)
        voice = SimpleNamespace(channel=object())
        author = SimpleNamespace(
            id=20,
            guild=guild,
            bot=False,
            voice=voice,
            mention="<@20>",
            display_name="Shooter",
        )
        target = SimpleNamespace(
            id=30,
            guild=guild,
            bot=False,
            voice=voice,
            mention="<@30>",
            display_name="Target",
        )
        ctx = SimpleNamespace(
            guild=guild,
            author=author,
            clean_prefix="!",
            reply=AsyncMock(),
        )
        users = {
            author.id: {"prestige": 0, "roulette_daily": {}},
            target.id: {"prestige": 0},
        }
        huge_bonus = 10**100

        with (
            patch("paragon.roulette._udict", side_effect=lambda _gid, uid: users[uid]),
            patch("paragon.roulette.random.random", return_value=0.0),
            patch("paragon.roulette.get_roulette_timeout_bonus_seconds", return_value=huge_bonus),
            patch("paragon.roulette.consume_roulette_accuracy_bonus", return_value=0.0),
            patch("paragon.roulette.consume_roulette_timeout_bonus_seconds") as consume_bonus,
            patch("paragon.roulette._timeout_member", new=AsyncMock(return_value=True)) as timeout_member,
            patch("paragon.roulette.record_game_fields"),
            patch("paragon.roulette.save_data", new=AsyncMock()),
        ):
            await RouletteCog.roulette.callback(RouletteCog(SimpleNamespace()), ctx, target)

        applied_seconds = timeout_member.await_args.args[1]
        used_bonus = DISCORD_MAX_TIMEOUT_SECONDS - 60
        self.assertEqual(applied_seconds, DISCORD_MAX_TIMEOUT_SECONDS)
        consume_bonus.assert_called_once_with(10, 20, seconds=used_bonus)
        reply = ctx.reply.await_args.args[0]
        self.assertIn(f"Bank remaining: **{huge_bonus - used_bonus}s**", reply)

    async def test_admin_can_reset_user_roulette_cooldown(self):
        guild = SimpleNamespace(id=40)
        member = SimpleNamespace(
            id=41,
            guild=guild,
            bot=False,
            mention="<@41>",
        )
        ctx = SimpleNamespace(guild=guild, clean_prefix="!", reply=AsyncMock())
        user = {"roulette_next_ts": 9999999999.0}

        with (
            patch("paragon.roulette._udict", return_value=user),
            patch("paragon.roulette.save_data", new=AsyncMock()) as save,
        ):
            await RouletteCog.reset_roulette.callback(RouletteCog(SimpleNamespace()), ctx, member)

        self.assertEqual(user["roulette_next_ts"], 0.0)
        save.assert_awaited_once()
        reply = ctx.reply.await_args.args[0]
        self.assertIn("They can use `!roulette` again now", reply)
        self.assertIn("Active Discord timeouts and wheel boosts were unchanged", reply)

    async def test_resetroulette_is_listed_in_admin_help(self):
        roulette_cog = RouletteCog(SimpleNamespace())
        reset_command = next(cmd for cmd in roulette_cog.get_commands() if cmd.name == "resetroulette")
        core = CoreCog(SimpleNamespace(commands=[reset_command]))
        ctx = SimpleNamespace(clean_prefix="!", reply=AsyncMock(), send=AsyncMock())

        await CoreCog.admin_help_command.callback(core, ctx)

        help_text = ctx.reply.await_args.args[0]
        self.assertIn("!resetroulette", help_text)
        self.assertIn("roulette command cooldown", help_text)


if __name__ == "__main__":
    unittest.main()
