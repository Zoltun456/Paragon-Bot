import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.admin import AdminCog
from paragon.guild_state import effective_utcnow, guild_settings, is_guild_enabled, mark_guild_disabled, mark_guild_enabled
from paragon.storage import load_data


class GuildStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()

    async def test_disable_then_enable_realigns_effective_time_to_current_time(self):
        guild = SimpleNamespace(id=101)
        t0 = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(hours=1)
        t2 = t0 + timedelta(hours=2)
        t3 = t2 + timedelta(minutes=30)

        with patch("paragon.guild_state.save_data", new=AsyncMock()):
            with patch("paragon.guild_state._utcnow", return_value=t0):
                changed = await mark_guild_disabled(guild)
            self.assertTrue(changed)
            self.assertFalse(is_guild_enabled(guild.id))

            with patch("paragon.guild_state._utcnow", side_effect=[t1, t1]):
                self.assertEqual(effective_utcnow(guild.id), t0)

            with patch("paragon.guild_state._utcnow", return_value=t2):
                elapsed = await mark_guild_enabled(guild)
            self.assertEqual(elapsed, 2 * 60 * 60)
            self.assertTrue(is_guild_enabled(guild.id))
            self.assertEqual(int(guild_settings(guild.id)["bot_paused_seconds"]), 0)

            with patch("paragon.guild_state._utcnow", return_value=t3):
                self.assertEqual(effective_utcnow(guild.id), t3)

    async def test_enabled_guild_ignores_legacy_paused_offset(self):
        guild = SimpleNamespace(id=102)
        settings = guild_settings(guild.id)
        settings["bot_enabled"] = True
        settings["bot_disabled_at"] = ""
        settings["bot_paused_seconds"] = 30 * 24 * 60 * 60

        now = datetime(2026, 6, 14, 0, 15, tzinfo=timezone.utc)
        with patch("paragon.guild_state._utcnow", return_value=now):
            self.assertEqual(effective_utcnow(guild.id), now)
        self.assertEqual(guild_settings(guild.id)["bot_paused_seconds"], 0.0)


class AdminToggleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()

    async def test_bottoggle_requires_second_confirmation_before_disabling(self):
        bot = SimpleNamespace(cogs={})
        cog = AdminCog(bot)
        cog._pause_guild_runtime = AsyncMock()

        guild = SimpleNamespace(id=202, name="Paragon Test")
        author = SimpleNamespace(
            id=303,
            guild_permissions=SimpleNamespace(administrator=True, manage_guild=True),
        )
        ctx = SimpleNamespace(
            guild=guild,
            author=author,
            clean_prefix="!",
            reply=AsyncMock(),
        )

        with (
            patch("paragon.admin.time.monotonic", side_effect=[100.0, 101.0]),
            patch("paragon.admin.is_guild_enabled", return_value=True),
            patch("paragon.admin.ensure_guild_setup", new=AsyncMock()) as ensure_setup,
            patch("paragon.admin.mark_guild_disabled", new=AsyncMock()) as mark_disabled,
            patch("paragon.admin.hide_managed_channels", new=AsyncMock(return_value=2)) as hide_channels,
        ):
            await AdminCog.bottoggle.callback(cog, ctx)
            ensure_setup.assert_not_awaited()
            mark_disabled.assert_not_awaited()
            hide_channels.assert_not_awaited()
            self.assertIn("armed", ctx.reply.await_args_list[0].args[0])

            await AdminCog.bottoggle.callback(cog, ctx)
            ensure_setup.assert_awaited_once_with(guild)
            cog._pause_guild_runtime.assert_awaited_once_with(guild.id)
            mark_disabled.assert_awaited_once_with(guild)
            hide_channels.assert_awaited_once_with(guild)
            self.assertIn("DISABLED", ctx.reply.await_args_list[-1].args[0])

    async def test_bottoggle_reenable_realigns_clock_without_extra_command(self):
        bot = SimpleNamespace(cogs={})
        cog = AdminCog(bot)
        cog._resume_guild_runtime = AsyncMock()

        guild = SimpleNamespace(id=204, name="Paragon Test")
        author = SimpleNamespace(
            id=305,
            guild_permissions=SimpleNamespace(administrator=True, manage_guild=True),
        )
        ctx = SimpleNamespace(
            guild=guild,
            author=author,
            clean_prefix="!",
            reply=AsyncMock(),
        )

        with (
            patch("paragon.admin.time.monotonic", side_effect=[200.0, 201.0]),
            patch("paragon.admin.is_guild_enabled", return_value=False),
            patch("paragon.admin.current_disabled_elapsed_seconds", return_value=2515725),
            patch("paragon.admin.mark_guild_enabled", new=AsyncMock(return_value=2515725)) as mark_enabled,
            patch("paragon.admin.restore_managed_channels", new=AsyncMock(return_value=4)) as restore_channels,
        ):
            await AdminCog.bottoggle.callback(cog, ctx)
            mark_enabled.assert_not_awaited()
            restore_channels.assert_not_awaited()
            self.assertIn("armed", ctx.reply.await_args_list[0].args[0])

            await AdminCog.bottoggle.callback(cog, ctx)
            mark_enabled.assert_awaited_once_with(guild)
            restore_channels.assert_awaited_once_with(guild)
            cog._resume_guild_runtime.assert_awaited_once_with(guild.id)
            self.assertIn("realigned to the current date", ctx.reply.await_args_list[-1].args[0])
