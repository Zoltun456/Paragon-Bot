import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from paragon.contracts import ContractsCog
from paragon.storage import load_data


class ContractsCogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        load_data()

    async def test_quest_replies_with_fast_clear_status_for_holder(self):
        cog = ContractsCog(SimpleNamespace())

        guild = SimpleNamespace(id=404, name="Quest Test")
        member = SimpleNamespace(
            id=505,
            bot=False,
            display_name="Alice",
            mention="<@505>",
        )
        member.guild = guild
        guild.members = [member]

        ctx = SimpleNamespace(
            guild=guild,
            author=member,
            clean_prefix="?",
            reply=AsyncMock(),
        )

        with patch("paragon.contracts.save_data", new=AsyncMock()):
            await ContractsCog.quest.callback(cog, ctx)

        self.assertEqual(ctx.reply.await_count, 1)
        reply_text = ctx.reply.await_args.args[0]
        self.assertIn("Daily Contract for Alice", reply_text)
        self.assertIn("Fast clear bonus active:", reply_text)
