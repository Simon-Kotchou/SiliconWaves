import discord
import youtube_dl
import asyncio
from discord.ext import commands 
from discord.errors import ClientException
import sqlite3
import json
import urllib
import requests
import base64
import os
import sys
import logging
import traceback
import itertools
from async_timeout import timeout
from functools import partial

youtube_dl.utils.bug_reports_message = lambda: ''

ytdl_config = {
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
    'source_address': '0.0.0.0',
}

ffmpeg_config = {
    'before_options': '-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -bufsize 512k'
}

yt_client = youtube_dl.YoutubeDL(ytdl_config)

class VoiceConnectionError(commands.CommandError):
    """Custom Exception class for connection errors."""

class InvalidVoiceChannel(VoiceConnectionError):
    """Exception for cases of invalid Voice Channels."""

class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester, volume=1):
        super().__init__(source, volume)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, download=False):
        loop = loop or asyncio.get_event_loop()

        to_run = partial(yt_client.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        await ctx.send(f'```ini\n[Added {data["title"]} to the Queue.]\n```', delete_after=15)

        if download:
            source = yt_client.prepare_filename(data)
        else:
            return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

        return cls(discord.FFmpegPCMAudio(source, **ffmpeg_config), data=data, requester=ctx.author)

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Used for preparing a stream, instead of downloading.
        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(yt_client.extract_info, url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)

        return cls(discord.FFmpegPCMAudio(data['url'], **ffmpeg_config), data=data, requester=requester)
    

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
        self.volume = .7

        self.bot.loop.create_task(self.create_table())
        self.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Our main player loop."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                async with timeout(1800):  # 30 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                try:
                    source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self._channel.send(f'There was an error processing your song.\n'
                                             f'```css\n[{e}]\n```')
                    continue

            source.volume = self.volume
            self.current_song = source.title

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            await self.add_to_db(str(source.title), str(source.web_url), str(source.requester.display_name))
            art_url = await self.get_album_art_spotify(source.title)
            embed = discord.Embed(title=f"Now playing: {source.title}")
            if art_url:
                embed.set_image(url=art_url)
            await self._channel.send(embed=embed)
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            # try:
            #     source.cleanup()
            # except ClientException:
            #     pass
            self.current_song = None
    
    async def create_table(self):
        conn = sqlite3.connect(f"{self._guild}_music.db")
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS music 
                     (id INTEGER PRIMARY KEY, title TEXT, url TEXT, user TEXT,
                     timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        conn.commit()
        conn.close()

    async def add_to_db(self, title, url, user):
        conn = sqlite3.connect(f"{self._guild}_music.db")
        c = conn.cursor()
        c.execute("INSERT INTO music (title, url, user) VALUES (?,?,?)", (title, url, user))
        conn.commit()
        conn.close()

    async def get_past_songs(self, limit):
        conn = sqlite3.connect(f'{self._guild}_music.db')
        cursor = conn.cursor()
        cursor.execute("SELECT title, url, user, timestamp FROM music ORDER BY id DESC LIMIT ?", (limit,))
        past_songs = cursor.fetchall()
        conn.close()
        return past_songs

    async def generate_recommendations(self):
        # Get the access token
        token = self.get_client_credentials_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # Get the past 5 songs played from the database
        past_songs = await self.get_past_songs(5)
        if not past_songs:
            return None

        # Get the Spotify IDs of the past songs
        track_ids = []
        for song in past_songs:
            track_id = await self.get_track_id(song[0], token=token)
            if track_id:
                track_ids.append(track_id)

        if not track_ids:
            return None

        # Get the recommendations from Spotify
        url = f"https://api.spotify.com/v1/recommendations?seed_tracks={','.join(track_ids)}"
        response = requests.get(url, headers=headers)
        data = json.loads(response.text)
        recommendations = data.get("tracks", [])

        return recommendations

    async def get_track_id(self, title, token=None):
        # Search for the song on Spotify and return its ID
        if token == None:
            token = self.get_client_credentials_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        query = f"{title}"
        query = urllib.parse.quote(query)
        url = f"https://api.spotify.com/v1/search?q={query}&type=track"
        response = requests.get(url, headers=headers)
        data = json.loads(response.text)
        try:
            track_id = data.get("tracks", {}).get("items", [{}])[0].get("id")
        except FileNotFoundError:
            track_id = None
        return track_id

    async def get_album_art_spotify(self, song_title):
        if not song_title:
            return None
        token = self.get_client_credentials_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        query = f"{song_title}"
        query = urllib.parse.quote(query)
        url = f"https://api.spotify.com/v1/search?q={query}&type=track"

        response = requests.get(url, headers=headers)
        data = json.loads(response.text)

        try:
            art_url = data.get("tracks", {}).get("items", [{}])[0].get("album", {}).get("images", [{}])[0].get("url")
        except FileNotFoundError:
            art_url = None
        return art_url
    
    def get_client_credentials_token(self):
        client_id = os.getenv('SPOTIFY_CLIENT_ID')
        client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()}"
        }

        data = {
            "grant_type": "client_credentials"
        }

        url = "https://accounts.spotify.com/api/token"
        response = requests.post(url, headers=headers, data=data)
        return response.json()["access_token"]

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command can not be used in Private Messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to Voice Channel. '
                           'Please make sure you are in a valid channel or provide me with one')

        print('Ignoring exception in command {}:'.format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='connect', aliases=['join'])
    async def connect_(self, ctx, *, channel: discord.VoiceChannel=None):
        """Connect to voice.
        Parameters
        ------------
        channel: discord.VoiceChannel [Optional]
            The channel to connect to. If a channel is not specified, an attempt to join the voice channel you are in
            will be made.
        This command also handles moving the bot to different channels.
        """
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                raise InvalidVoiceChannel('No channel to join. Please either specify a valid channel or join one.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Connecting to channel: <{channel}> timed out.')

        await ctx.send(f'Connected to: **{channel}**', delete_after=20)

    @commands.command(name='play', aliases=['queue'])
    async def play_(self, ctx, *, search: str):
        """Request a song and add it to the queue.
        This command attempts to join a valid voice channel if the bot is not already in one.
        Uses YTDL to automatically search and retrieve a song.
        Parameters
        ------------
        search: str [Required]
            The song to search and retrieve using YTDL. This could be a simple search, an ID or URL.
        """
        async with ctx.typing():

            vc = ctx.voice_client

            if not vc:
                await ctx.invoke(self.connect_)

            player = self.get_player(ctx)

            # If download is False, source will be a dict which will be used later to regather the stream.
            # If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
            source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=False)

            await player.queue.put(source)

    @commands.command(name='pause')
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('I am not currently playing anything!', delete_after=20)
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send(f'**`{ctx.author}`**: Paused the song!')

    @commands.command(name='resume')
    async def resume_(self, ctx):
        """Resume the currently paused song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!', delete_after=20)
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send(f'**`{ctx.author}`**: Resumed the song!')

    @commands.command(name='skip')
    async def skip_(self, ctx):
        """Skip the song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!', delete_after=20)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()
        await ctx.send(f'**`{ctx.author}`**: Skipped the song!')

    @commands.command(name='status', aliases=['playlist'])
    async def queue_info(self, ctx):
        """Retrieve a basic queue of upcoming songs."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!', delete_after=20)

        player = self.get_player(ctx)
        if player.queue.empty():
            return await ctx.send('There are currently no more queued songs.')

        # Grab up to 5 entries from the queue...
        upcoming = list(itertools.islice(player.queue._queue, 0, 5))

        fmt = '\n'.join(f'**`{_["title"]}`**' for _ in upcoming)
        embed = discord.Embed(title=f'Upcoming - Next {len(upcoming)}', description=fmt)

        await ctx.send(embed=embed)

    @commands.command(name='volume', aliases=['vol'])
    async def change_volume(self, ctx, *, vol: float):
        """Change the player volume.
        Parameters
        ------------
        volume: float or int [Required]
            The volume to set the player to in percentage. This must be between 1 and 100.
        """
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

    @commands.command(name='leave')
    async def stop_(self, ctx):
        """Stop the currently playing song and destroy the player.
        !Warning!
            This will destroy the player assigned to your guild, also deleting any queued songs and settings.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!', delete_after=20)

        await self.cleanup(ctx.guild)
    
    @commands.command(name="recommend", description="generates spotify recommendations")
    async def recommend_(self, ctx):
        async with ctx.typing():
            player = self.get_player(ctx)
            recommendations = await player.generate_recommendations()
            if not recommendations:
                return await ctx.send("No recommendations could be generated.")
            message = "Here are your recommendations:\n"

            for i, track in enumerate(recommendations):
                message += f"{i+1}. {track['name']} by {track['artists'][0]['name']}\n"
            await ctx.send(message)
    
    @commands.command(name="recent", description="shows the recent 5 songs played")
    async def recent_(self, ctx):
        async with ctx.typing():
            player = self.get_player(ctx)
            recent_songs = await player.get_past_songs(5)
            for i, row in enumerate(recent_songs):
                await ctx.send(f"{i+1}. {row[0]} - {row[1]} - {row[2]} - {row[3]}")


async def setup_yt_client(bot):
    if not bot.get_cog("Music"):
        await bot.add_cog(Music(bot))
    else:
        print("Music cog has already been added.")