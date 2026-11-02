
import discord
from discord.ext import commands
from curl_cffi import requests

import asyncio
import urllib.parse
import os
import logging

# ==============================
# CONFIGURATION
# ==============================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

RAW_COOKIE = os.getenv("SPIDE_COOKIE")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

COOKIE_STRING = RAW_COOKIE.strip() if RAW_COOKIE else None

SPIDE_API = "https://web.spide.ru/api/v1/spotify/search"
SPIDE_SEARCH = "https://web.spide.ru/search"

ORANGE = 0xFF8C00


# ==============================
# TRACK VIEW
# ==============================

class TrackView(discord.ui.View):

    def __init__(self, url):
        super().__init__(timeout=180)

        self.add_item(
            discord.ui.Button(
                label="Слушать в Spide",
                emoji="▶",
                style=discord.ButtonStyle.link,
                url=url
            )
        )


# ==============================
# HELPERS
# ==============================

def format_duration(duration_ms):

    try:
        duration_ms = int(duration_ms or 0)
    except (ValueError, TypeError):
        duration_ms = 0

    minutes = duration_ms // 60000
    seconds = (duration_ms % 60000) // 1000

    return f"{minutes}:{seconds:02d}"


def format_artists(artists):

    if not isinstance(artists, list):
        return "Неизвестный исполнитель"

    names = []

    for artist in artists:
        if isinstance(artist, dict):
            name = artist.get("name")
        elif isinstance(artist, str):
            name = artist
        else:
            name = None

        if name:
            names.append(str(name))

    return ", ".join(names) if names else "Неизвестный исполнитель"


def get_album_name(album):

    if isinstance(album, dict):
        return album.get("title") or album.get("name") or "Сингл"

    if isinstance(album, str):
        return album or "Сингл"

    return "Сингл"


# ==============================
# API REQUEST
# ==============================

def search_spide(query, headers):

    url = (
        f"{SPIDE_API}?"
        f"{urllib.parse.urlencode({
            'q': query,
            'type': 'all',
            'limit': 10
        })}"
    )

    response = requests.get(
        url,
        headers=headers,
        impersonate="chrome",
        timeout=20
    )

    logging.info(
        "Spide response: HTTP %s",
        response.status_code
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        raise ValueError("Неожиданный формат ответа Spide")

    if data.get("error"):
        raise ValueError(str(data["error"]))

    return data


# ==============================
# EMBED DESIGN
# ==============================

def create_track_embed(track, ctx, query):

    title = str(
        track.get("title") or "Неизвестный трек"
    )

    artists = format_artists(
        track.get("artists", [])
    )

    album_name = get_album_name(
        track.get("album")
    )

    duration = format_duration(
        track.get("durationMs", 0)
    )

    cover = track.get("cover")

    if not isinstance(cover, str) or not cover.startswith(
        ("https://", "http://")
    ):
        cover = None

    # ==========================
    # SEARCH LINK
    # ==========================

    # Ищем по названию и исполнителю
    search_query = f"{title} {artists}".strip()

    track_url = (
        f"{SPIDE_SEARCH}?"
        f"{urllib.parse.urlencode({'q': search_query})}"
    )

    # ==========================
    # EMBED
    # ==========================

    embed = discord.Embed(
        title=title[:256],
        url=track_url,
        description=f"**{artists[:1000]}**",
        color=ORANGE,
        timestamp=discord.utils.utcnow()
    )

    embed.set_author(
        name="SPIDE MUSIC  /  TRACK"
    )

    # Album cover
    if cover:
        embed.set_thumbnail(url=cover)

    # Album
    embed.add_field(
        name="АЛЬБОМ",
        value=f"*{album_name[:1024]}*",
        inline=True
    )

    # Duration
    embed.add_field(
        name="ДЛИТЕЛЬНОСТЬ",
        value=f"`{duration}`",
        inline=True
    )

    # Search query
    embed.add_field(
        name="ПОИСКОВЫЙ ЗАПРОС",
        value=f"`{query[:100]}`",
        inline=False
    )

    # Footer
    avatar = ctx.author.display_avatar.url

    embed.set_footer(
        text=f"SPIDE MUSIC  •  Запросил {ctx.author.display_name}",
        icon_url=avatar
    )

    return embed, track_url


# ==============================
# EVENTS
# ==============================

@bot.event
async def on_ready():

    logging.info(
        "Бот %s успешно запущен!",
        bot.user
    )


# ==============================
# SEARCH COMMAND
# ==============================

@bot.command(name="search")
async def search(ctx, *, query: str = None):

    if not query or not query.strip():

        await ctx.send(
            "Напиши название трека.\n"
            "Пример: `!search Despacito`"
        )

        return

    if not COOKIE_STRING:

        await ctx.send(
            "Ошибка: Cookie Spide не настроены."
        )

        return

    query = query.strip()

    if len(query) > 200:

        await ctx.send(
            "Слишком длинный поисковый запрос."
        )

        return

    msg = await ctx.send(
        "🔎 Ищу треки в Spide..."
    )

    logging.info(
        "Пользователь %s ищет: %s",
        ctx.author.name,
        query
    )

    headers = {
        "accept": "application/json",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": "https://web.spide.ru/search",
        "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "x-audio-fallback": "2",
        "Cookie": COOKIE_STRING
    }

    try:

        data = await asyncio.to_thread(
            search_spide,
            query,
            headers
        )

        tracks = data.get("tracks", [])

        if not isinstance(tracks, list):
            tracks = []

        if not tracks:

            await msg.edit(
                content=f"По запросу **{query}** ничего не найдено."
            )

            return

        track = tracks[0]

        if not isinstance(track, dict):
            raise ValueError("Некорректные данные трека")

        embed, track_url = create_track_embed(
            track,
            ctx,
            query
        )

        view = TrackView(track_url)

        await msg.edit(
            content=None,
            embed=embed,
            view=view
        )

        logging.info(
            "Найден трек: %s — %s",
            format_artists(track.get("artists", [])),
            track.get("title")
        )

    except Exception as e:

        logging.exception("Ошибка поиска Spide")

        await msg.edit(
            content=f"Ошибка поиска: `{str(e)[:500]}`",
            embed=None,
            view=None
        )


# ==============================
# START
# ==============================

if __name__ == "__main__":

    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        logging.error(
            "ОШИБКА: Не задан DISCORD_TOKEN!"
        )
