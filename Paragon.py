import discord
from discord.ext import commands

from paragon.admin import AdminCog
from paragon.anagram import AnagramCog
from paragon.blackjack import BlackjackCog
from paragon.coinflip import CoinFlipCog
from paragon.config import AUTHOR_USER_ID, COMMAND_PREFIX, TOKEN
from paragon.core import CoreCog
from paragon.game_stats import StatsCog
from paragon.lotto import LottoCog
from paragon.prestige import PrestigeCog
from paragon.roulette import RouletteCog
from paragon.storage import load_data
from paragon.surprise import SurpriseCog
from paragon.thanks import ThanksCog
from paragon.voice import VoiceCog
from paragon.wordle import WordleCog


def create_bot() -> commands.Bot:
    intents = discord.Intents(
        guilds=True,
        members=True,
        messages=True,
        message_content=True,
        voice_states=True,
        reactions=True,
    )
    return commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)


bot = create_bot()


@bot.check_once
async def _global_check(_ctx):
    return True


def setup_cogs_sync() -> None:
    bot.add_cog(CoreCog(bot))
    bot.add_cog(WordleCog(bot))
    bot.add_cog(CoinFlipCog(bot))
    bot.add_cog(RouletteCog(bot))
    bot.add_cog(SurpriseCog(bot))
    bot.add_cog(AnagramCog(bot))
    bot.add_cog(ThanksCog(bot))
    bot.add_cog(LottoCog(bot))
    bot.add_cog(PrestigeCog(bot))
    bot.add_cog(BlackjackCog(bot))
    bot.add_cog(VoiceCog(bot))
    bot.add_cog(StatsCog(bot))
    bot.add_cog(AdminCog(bot))


@bot.event
async def on_ready():
    author_info = f"{AUTHOR_USER_ID}" if AUTHOR_USER_ID else "unset"
    print(f"Bot online as {bot.user} | author={author_info}")


def _bootstrap_storage() -> None:
    load_data()


if __name__ == "__main__":
    _bootstrap_storage()
    setup_cogs_sync()
    bot.run(TOKEN)
