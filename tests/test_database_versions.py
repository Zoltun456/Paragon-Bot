import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.admin import AdminCog
from paragon.storage import (
    _gdict,
    _udict,
    active_database_version,
    available_database_versions,
    create_database_version,
    database_version_user_count,
    has_database_version,
    load_data,
    set_active_database_version,
)


class DatabaseVersionStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()

    async def test_create_and_switch_versions_preserve_shared_server_state(self):
        gid = 9101
        g = _gdict(gid)
        g["settings"]["inactive_loss_enabled"] = False
        g["channels"]["log_channel_id"] = 777
        g["spin_wheel"] = {
            "reset_hour": 5,
            "reset_minute": 30,
            "reward_overrides": {"xp_small": False},
        }
        user_one = _udict(gid, 101)
        user_one["xp_f"] = 123.0
        user_one["xp"] = 123

        with patch("paragon.storage.save_data", new=AsyncMock()):
            new_id = await create_database_version(gid)
            self.assertEqual(new_id, 2)
            self.assertEqual(active_database_version(gid), 2)
            self.assertEqual(available_database_versions(gid), [1, 2])
            self.assertEqual(database_version_user_count(gid, 1), 1)
            self.assertTrue(has_database_version(gid, 2))

            active = _gdict(gid)
            self.assertEqual(len(active.get("users", {})), 0)
            self.assertFalse(active["settings"]["inactive_loss_enabled"])
            self.assertEqual(active["channels"]["log_channel_id"], 777)
            self.assertEqual(active.get("spin_wheel", {}).get("reset_hour"), 5)
            self.assertEqual(active.get("spin_wheel", {}).get("reset_minute"), 30)

            user_two = _udict(gid, 202)
            user_two["xp_f"] = 55.0
            user_two["xp"] = 55

            await set_active_database_version(gid, 1)
            active = _gdict(gid)
            self.assertEqual(active_database_version(gid), 1)
            self.assertIn("101", active.get("users", {}))
            self.assertNotIn("202", active.get("users", {}))
            self.assertFalse(active["settings"]["inactive_loss_enabled"])
            self.assertEqual(active["channels"]["log_channel_id"], 777)
            self.assertEqual(active.get("spin_wheel", {}).get("reset_hour"), 5)

            await set_active_database_version(gid, 2)
            active = _gdict(gid)
            self.assertEqual(active_database_version(gid), 2)
            self.assertIn("202", active.get("users", {}))
            self.assertNotIn("101", active.get("users", {}))
            self.assertEqual(database_version_user_count(gid, 2), 1)
            self.assertFalse(active["settings"]["inactive_loss_enabled"])
            self.assertEqual(active["channels"]["log_channel_id"], 777)
            self.assertEqual(active.get("spin_wheel", {}).get("reset_minute"), 30)


class AdminDatabaseCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()

    async def test_database_new_requires_confirmation(self):
        bot = SimpleNamespace(cogs={})
        cog = AdminCog(bot)
        cog._pause_guild_runtime = AsyncMock()
        cog._resume_guild_runtime = AsyncMock()

        guild = SimpleNamespace(id=9201, name="Paragon Test")
        author = SimpleNamespace(
            id=9202,
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
            patch("paragon.admin.next_database_version", return_value=2),
            patch("paragon.admin.create_database_version", new=AsyncMock(return_value=2)) as create_db,
            patch("paragon.admin.available_database_versions", return_value=[1, 2]),
            patch("paragon.admin.database_version_user_count", side_effect=lambda _gid, vid: 0 if vid == 2 else 4),
            patch("paragon.admin.enforce_level6_exclusive", new=AsyncMock()) as sync_roles,
        ):
            await AdminCog.database.callback(cog, ctx, "new")
            create_db.assert_not_awaited()
            self.assertIn("armed", ctx.reply.await_args_list[0].args[0])

            await AdminCog.database.callback(cog, ctx, "new")
            cog._pause_guild_runtime.assert_awaited_once_with(guild.id)
            cog._resume_guild_runtime.assert_awaited_once_with(guild.id)
            create_db.assert_awaited_once_with(guild.id)
            sync_roles.assert_awaited_once_with(guild)
            self.assertIn("Database **2** is now active", ctx.reply.await_args_list[-1].args[0])

    async def test_database_set_requires_confirmation(self):
        bot = SimpleNamespace(cogs={})
        cog = AdminCog(bot)
        cog._pause_guild_runtime = AsyncMock()
        cog._resume_guild_runtime = AsyncMock()

        guild = SimpleNamespace(id=9301, name="Paragon Test")
        author = SimpleNamespace(
            id=9302,
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
            patch("paragon.admin.is_guild_enabled", return_value=True),
            patch("paragon.admin.has_database_version", return_value=True),
            patch("paragon.admin.active_database_version", return_value=1),
            patch("paragon.admin.set_active_database_version", new=AsyncMock(return_value=True)) as set_db,
            patch("paragon.admin.database_version_user_count", return_value=3),
            patch("paragon.admin.enforce_level6_exclusive", new=AsyncMock()) as sync_roles,
        ):
            await AdminCog.database.callback(cog, ctx, "set", "2")
            set_db.assert_not_awaited()
            self.assertIn("armed", ctx.reply.await_args_list[0].args[0])

            await AdminCog.database.callback(cog, ctx, "set", "2")
            cog._pause_guild_runtime.assert_awaited_once_with(guild.id)
            cog._resume_guild_runtime.assert_awaited_once_with(guild.id)
            set_db.assert_awaited_once_with(guild.id, 2)
            sync_roles.assert_awaited_once_with(guild)
            self.assertIn("Database **2** is now active", ctx.reply.await_args_list[-1].args[0])
