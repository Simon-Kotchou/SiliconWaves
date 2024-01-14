import discord
from YTClient import setup_yt_client
from discord.ext import commands
import os

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix='/', intents=intents)
    @bot.event
    async def on_ready():
        await setup_yt_client(bot)
    token = os.getenv("DISCORD_TOKEN")
    bot.run(token)