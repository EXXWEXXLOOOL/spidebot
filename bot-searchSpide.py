import discord
from discord.ext import commands, tasks
from curl_cffi import requests

import asyncio
import urllib.parse
import os
import logging
import datetime

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
SPIDE_TOP_SUMMARY = "https://web.spide.ru/api/v1/tops/summary?tz=Europe/Moscow"

ORANGE = 0xFF8C00

# 🚨 ВСТАВЬ СЮДА ID КАНАЛА, КУДА БОТ БУДЕТ ПРИСЫЛАТЬ АВТО-ТОП
TOP_CHANNEL_ID = 123456789012345678 

# ==============================
# HELPERS & VIEWS
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

def format_duration(duration_ms):
    try:
        duration_ms = int(duration_ms or 0)
    except (ValueError, TypeError):
        duration_ms = 0
    minutes = duration_ms // 60000
    seconds = (duration_ms % 60000) // 1000
    return str(minutes) + ":" + str(seconds).zfill(2)

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

def search_spide(query, headers):
    params = urllib.parse.urlencode({"q": query, "type": "all", "limit": 10})
    url = SPIDE_API + "?" + params
    response = requests.get(url, headers=headers, impersonate="chrome", timeout=20)
    logging.info("Spide response: HTTP %s", response.status_code)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Неожиданный формат ответа Spide")
    if data.get("error"):
        raise ValueError(str(data["error"]))
    return data

def create_track_embed(track, ctx, query):
    title = str(track.get("title") or "Неизвестный трек")
    artists = format_artists(track.get("artists", []))
    album_name = get_album_name(track.get("album"))
    duration = format_duration(track.get("durationMs", 0))
    
    cover = track.get("cover")
    if not isinstance(cover, str) or not cover.startswith(("https://", "http://")):
        cover = None

    search_query = (title + " " + artists).strip()
    search_params = urllib.parse.urlencode({"q": search_query})
    track_url = SPIDE_SEARCH + "?" + search_params

    embed = discord.Embed(
        title=title[:256], url=track_url, description="**" + artists[:1000] + "**",
        color=ORANGE, timestamp=discord.utils.utcnow()
    )
    embed.set_author(name="SPIDE MUSIC  /  TRACK")
    if cover:
        embed.set_thumbnail(url=cover)
    embed.add_field(name="АЛЬБОМ", value="*" + album_name[:1024] + "*", inline=True)
    embed.add_field(name="ДЛИТЕЛЬНОСТЬ", value="`" + duration + "`", inline=True)
    embed.add_field(name="ПОИСКОВЫЙ ЗАПРОС", value="`" + query[:100] + "`", inline=False)
    embed.set_footer(text="SPIDE MUSIC  •  Запросил " + ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    return embed, track_url

# ==============================
# AUTO TOP TASK (03:22 МСК)
# ==============================

# 00:22 по UTC = 03:22 по Москве
@tasks.loop(time=datetime.time(hour=0, minute=22, tzinfo=datetime.timezone.utc))
async def daily_top_task():
    channel = bot.get_channel(TOP_CHANNEL_ID)
    if not channel:
        logging.error("Не найден канал для отправки топа (проверь TOP_CHANNEL_ID).")
        return

    if not COOKIE_STRING:
        logging.error("Нет куки для запроса топа.")
        return

    headers = {
        "accept": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Cookie": COOKIE_STRING
    }

    try:
        response = requests.get(SPIDE_TOP_SUMMARY, headers=headers, impersonate="chrome", timeout=20)
        data = response.json()
        
        # Ищем доску слушателей
        listeners_board = next((b for b in data.get("boards", []) if b.get("board") == "listeners"), None)
        if not listeners_board:
            return

        leaders = listeners_board.get("leaders", [])
        total = listeners_board.get("total", 0)
        date_str = data.get("day", "Сегодня")

        embed = discord.Embed(
            title="🏆 Топ слушателей Spide",
            description=f"Обновлено за **{date_str}**\nВсего участников: `{total}`",
            color=ORANGE,
            timestamp=discord.utils.utcnow()
        )

        # Настраиваем пьедестал
        medals = ["🥇", "🥈", "🥉"]
        for i, leader in enumerate(leaders[:3]):
            name = leader.get("displayName", "Неизвестно")
            avatar = leader.get("photoUrl")
            # Примечание: в этом JSON нет минут для 2 и 3 места, поэтому выводим только имена
            embed.add_field(name=f"{medals[i]} {i+1} место", value=f"**{name}**", inline=True)
            
            # Если это первое место, ставим его аватарку как картинку
            if i == 0 and avatar:
                embed.set_thumbnail(url=avatar)

        embed.set_footer(text="SPIDE MUSIC  •  Автоматическое обновление")
        
        await channel.send(embed=embed)
        logging.info("Ежедневный топ успешно отправлен!")

    except Exception as e:
        logging.error(f"Ошибка при отправке авто-топа: {e}")

# ==============================
# EVENTS & COMMANDS
# ==============================

@bot.event
async def on_ready():
    logging.info("Бот %s успешно запущен!", bot.user)
    # Запускаем фоновую задачу (если она еще не запущена)
    if not daily_top_task.is_running():
        daily_top_task.start()

@bot.command(name="search")
async def search(ctx, *, query: str = None):
    if not query or not query.strip():
        await ctx.send("Напиши название трека.\nПример: `!search Despacito`")
        return
    if not COOKIE_STRING:
        await ctx.send("Ошибка: Cookie Spide не настроены.")
        return

    query = query.strip()
    msg = await ctx.send("🔎 Ищу треки в Spide...")
    
    headers = {
        "accept": "application/json",
        "referer": "https://web.spide.ru/search",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Cookie": COOKIE_STRING
    }

    try:
        data = await asyncio.to_thread(search_spide, query, headers)
        tracks = data.get("tracks", [])
        if not tracks:
            await msg.edit(content="По запросу **" + query + "** ничего не найдено.")
            return
        track = tracks[0]
        embed, track_url = create_track_embed(track, ctx, query)
        await msg.edit(content=None, embed=embed, view=TrackView(track_url))
    except Exception as e:
        logging.exception("Ошибка поиска Spide")
        await msg.edit(content="Ошибка поиска: `" + str(e)[:500] + "`")

# Команда для ручного вызова топа прямо сейчас (для теста)
@bot.command(name="top")
async def top_now(ctx):
    if ctx.channel.id != TOP_CHANNEL_ID:
        await ctx.send("Топ отправляется в специальный канал, но я продублирую его здесь для теста!")
    # Принудительно вызываем задачу
    await daily_top_task()

# ==============================
# START
# ==============================

if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        logging.error("ОШИБКА: Не задан DISCORD_TOKEN!")
