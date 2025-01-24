import discord
import yt_dlp
import asyncio
from discord.ext import commands 
from discord.errors import ClientException
import sqlite3
import logging
import traceback
import itertools
from async_timeout import timeout

# yt-dlp configuration
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}

ffmpeg_options = {
    'options': '-vn -bufsize 512k'
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
            
            if 'entries' in data:
                data = data['entries'][0]

            filename = data['url'] if stream else ytdl.prepare_filename(data)
            return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)
        except Exception as e:
            logging.error(f"Error downloading from URL: {e}")
            raise

class MusicPlayer:
    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current_song', 'volume')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.current_song = None
        self.volume = .5

        self.bot.loop.create_task(self.create_table())
        self.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Main player loop with fixes for queue handling."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout, then disconnect
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return await self.destroy(self._guild)

            # Only process if we're still connected
            if self._guild.voice_client is None:
                return

            try:
                # Convert the source if needed
                if not isinstance(source, YTDLSource):
                    try:
                        source = await YTDLSource.from_url(source.url, loop=self.bot.loop, stream=True)
                    except Exception as e:
                        await self._channel.send(f'There was an error processing your song.\n'
                                               f'```css\n[{e}]\n```')
                        continue

                source.volume = self.volume
                self.current_song = source

                # Start playing
                if self._guild.voice_client:  # Double check we're still connected
                    self._guild.voice_client.play(
                        source, 
                        after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set)
                    )
                    
                    await self.add_to_db(str(source.title), str(source.url), str(source.data.get('uploader', 'Unknown')))
                    embed = discord.Embed(title=f"Now playing: {source.title}", color=discord.Color.green())
                    await self._channel.send(embed=embed)

                    # Wait for the song to finish
                    await self.next.wait()

            except Exception as e:
                await self._channel.send(f"An error occurred while playing: {str(e)}")
                logging.error(f"Player error: {str(e)}")
                continue
            finally:
                try:
                    if source:
                        try:
                            source.cleanup()
                        except Exception as e:
                            logging.error(f"Error cleaning up source: {str(e)}")
                except Exception as e:
                    logging.error(f"Error in cleanup: {str(e)}")
                
                self.current_song = None

    async def create_table(self):
        try:
            conn = sqlite3.connect(f"music_{self._guild.id}.db")
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS music 
                         (id INTEGER PRIMARY KEY, title TEXT, url TEXT, user TEXT,
                         timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
            conn.commit()
        except Exception as e:
            logging.error(f"Database error: {e}")
        finally:
            if conn:
                conn.close()

    async def add_to_db(self, title, url, user):
        try:
            conn = sqlite3.connect(f"music_{self._guild.id}.db")
            c = conn.cursor()
            c.execute("INSERT INTO music (title, url, user) VALUES (?,?,?)", (title, url, user))
            conn.commit()
        except Exception as e:
            logging.error(f"Database error: {e}")
        finally:
            if conn:
                conn.close()

    async def destroy(self, guild):
        try:
            return await self._cog.cleanup(guild)
        except Exception as e:
            logging.error(f"Error destroying player: {e}")

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('music_bot.log'),
                logging.StreamHandler()
            ]
        )

    async def cleanup(self, guild):
        try:
            player = self.players.pop(guild.id, None)
            if player:
                try:
                    player.queue._queue.clear()
                except Exception as e:
                    logging.error(f"Error clearing queue: {str(e)}")
            
            if guild.voice_client:
                try:
                    if guild.voice_client.is_playing():
                        guild.voice_client.stop()
                    await guild.voice_client.disconnect(force=True)
                except Exception as e:
                    logging.error(f"Error disconnecting: {str(e)}")
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")
            traceback.print_exc()

    def get_player(self, ctx):
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player
        return player

    @commands.command(name='connect', aliases=['join'])
    async def connect_(self, ctx, *, channel: discord.VoiceChannel=None):
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                return await ctx.send('No channel to join. Please either specify a valid channel or join one.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                return await ctx.send(f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                return await ctx.send(f'Connecting to channel: <{channel}> timed out.')

        await ctx.send(f'Connected to: **{channel}**', delete_after=20)

    @commands.command(name='play', aliases=['queue'])
    async def play_(self, ctx, *, search: str):
        try:
            async with ctx.typing():
                vc = ctx.voice_client

                if not vc:
                    await ctx.invoke(self.connect_)

                player = self.get_player(ctx)
                source = await YTDLSource.from_url(search, loop=self.bot.loop, stream=False)
                await player.queue.put(source)
                await ctx.send(f'Added to queue: **{source.title}**')
                
        except Exception as e:
            await ctx.send(f'An error occurred while processing your request: {str(e)}')

    @commands.command(name='pause')
    async def pause_(self, ctx):
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('I am not currently playing anything!', delete_after=20)
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send(f'**`{ctx.author}`**: Paused the song!')

    @commands.command(name='resume')
    async def resume_(self, ctx):
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!', delete_after=20)
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send(f'**`{ctx.author}`**: Resumed the song!')

    @commands.command(name='skip')
    async def skip_(self, ctx):
        """Skip the current song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!', delete_after=20)

        if not vc.is_playing() and not vc.is_paused():
            return await ctx.send('Nothing is playing that could be skipped!', delete_after=20)

        try:
            vc.stop()
            await ctx.send(f'**`{ctx.author}`**: Skipped the song!')
        except Exception as e:
            await ctx.send(f'An error occurred while trying to skip: {str(e)}')

    @commands.command(name='queue_info', aliases=['q', 'playlist'])
    async def queue_info(self, ctx):
        """Show the current queue."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!', delete_after=20)

        player = self.get_player(ctx)
        if player.queue.empty():
            return await ctx.send('There are currently no more queued songs.')

        upcoming = list(itertools.islice(player.queue._queue, 0, 5))

        # Fix for the playlist display error
        fmt = '\n'.join(f'**`{_.title}`**' for _ in upcoming)
        embed = discord.Embed(title=f'Upcoming - Next {len(upcoming)}', description=fmt)

        await ctx.send(embed=embed)

    @commands.command(name='volume', aliases=['vol'])
    async def change_volume(self, ctx, *, vol: float):
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!', delete_after=20)

        if not 0 < vol < 101:
            return await ctx.send('Please enter a value between 1 and 100.')

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        await ctx.send(f'**`{ctx.author}`**: Set the volume to **{vol}%**')

    @commands.command(name='leave', aliases=['disconnect', 'dc', 'stop'])
    async def stop_(self, ctx):
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!', delete_after=20)

        await self.cleanup(ctx.guild)
        await ctx.send('Disconnected and cleared the queue.')

async def setup_yt_client(bot):
    try:
        if not bot.get_cog("Music"):
            await bot.add_cog(Music(bot))
        else:
            print("Music cog has already been added.")
    except Exception as e:
        print(f"Error setting up music client: {e}")