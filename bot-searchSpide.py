import discord
from discord.ext import commands, tasks
from curl_cffi import requests
from bs4 import BeautifulSoup

import asyncio
import urllib.parse
import os
import logging
import datetime
import re

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

# НАСТРОЙКИ TELEGRAM
TG_CHANNEL_NAME = "dsdedmak0193" 
TG_URL = f"https://t.me/s/{TG_CHANNEL_NAME}"

ORANGE = 0xFF8C00

# ID каналов
TOP_CHANNEL_ID = 1422240878105198734 
UPDATE_CHANNEL_ID = 1422240878105198734 # Сюда будут прилетать посты из ТГК

# Файл для хранения ID последнего поста
DATA_DIR = os.getenv("DATA_DIR", ".")
LAST_UPDATE_FILE = os.path.join(DATA_DIR, "last_update_id.txt")

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
# TELEGRAM PARSER
# ==============================

def get_last_saved_id():
    if os.path.exists(LAST_UPDATE_FILE):
        with open(LAST_UPDATE_FILE, "r", encoding="utf-8") as f:
            val = f.read().strip()
            return int(val) if val.isdigit() else 0
    return 0

def save_last_id(post_id):
    with open(LAST_UPDATE_FILE, "w", encoding="utf-8") as f:
        f.write(str(post_id))

def fetch_all_tg_posts():
    headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"}
    try:
        response = requests.get(TG_URL, headers=headers, impersonate="chrome", timeout=20)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        messages = soup.find_all('div', class_='tgme_widget_message')
        
        posts = []
        for msg in messages:
            post_id_str = msg.get('data-post') # Например, "dsdedmak0193/5"
            if not post_id_str:
                continue
            
            try:
                post_id = int(post_id_str.split('/')[-1])
            except ValueError:
                continue
                
            text_div = msg.find('div', class_='tgme_widget_message_text')
            text = text_div.get_text(separator='\n', strip=True) if text_div else ""
            
            if text:
                posts.append({"id": post_id, "text": text})
                
        return posts
    except Exception as e:
        logging.error(f"Ошибка парсинга Telegram: {e}")
        return []

# ==============================
# TASKS
# ==============================

@tasks.loop(minutes=30)
async def check_updates_task():
    channel = bot.get_channel(UPDATE_CHANNEL_ID)
    if not channel:
        return

    posts = await asyncio.to_thread(fetch_all_tg_posts)
    if not posts:
        return

    last_id = get_last_saved_id()

    # Если это первый запуск (файла нет), просто запоминаем последний пост
    if last_id == 0:
        max_id = max(p['id'] for p in posts)
        save_last_id(max_id)
        logging.info(f"Первый запуск парсера. Запомнили ID поста: {max_id}")
        return

    # Отбираем только новые посты
    new_posts = [p for p in posts if p['id'] > last_id]
    new_posts.sort(key=lambda x: x['id']) # Сортируем от старых к новым

    for post in new_posts:
        embed = discord.Embed(
            title="📢 Новый пост в Telegram-канале",
            description=post["text"][:4000],
            color=ORANGE,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Ссылка на пост", value=f"[Открыть в Telegram](https://t.me/{TG_CHANNEL_NAME}/{post['id']})")
        embed.set_footer(text="SPIDE MUSIC  •  Автоматическая рассылка")
        
        try:
            await channel.send(embed=embed)
            await asyncio.sleep(2) # Пауза, чтобы Discord не выдал бан за спам
        except Exception as e:
            logging.error(f"Не удалось отправить пост {post['id']}: {e}")

    # Запоминаем ID самого нового поста
    if new_posts:
        save_last_id(new_posts[-1]['id'])
        logging.info(f"Отправлено {len(new_posts)} новых постов. Новый last_id: {new_posts[-1]['id']}")

async def send_top_to_channel(channel):
    if not COOKIE_STRING: return
    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0", "Cookie": COOKIE_STRING}
    try:
        response = await asyncio.to_thread(requests.get, SPIDE_TOP_SUMMARY, headers=headers, impersonate="chrome", timeout=20)
        response.raise_for_status()
        data = response.json()
        listeners_board = next((b for b in data.get("boards", []) if b.get("board") == "listeners"), None)
        if not listeners_board: return

        leaders = listeners_board.get("leaders", [])
        total = listeners_board.get("total", 0)
        embed = discord.Embed(title="🏆 Топ слушателей Spide", description=f"Обновлено за **{data.get('day', 'Сегодня')}**\nВсего участников: `{total}`", color=ORANGE)
        for i, leader in enumerate(leaders[:3]):
            medals = ["🥇", "🥈", "🥉"]
            embed.add_field(name=f"{medals[i]} {i+1} место", value=f"**{leader.get('displayName', 'Неизвестно')}**", inline=True)
            if i == 0 and leader.get("photoUrl"):
                embed.set_thumbnail(url=leader["photoUrl"])
        embed.set_footer(text="SPIDE MUSIC  •  Автоматическое обновление")
        await channel.send(embed=embed)
    except Exception as e:
        logging.error(f"Ошибка топа: {e}")

@tasks.loop(time=datetime.time(hour=0, minute=22, tzinfo=datetime.timezone.utc))
async def daily_top_task():
    channel = bot.get_channel(TOP_CHANNEL_ID)
    if channel: await send_top_to_channel(channel)

# ==============================
# EVENTS & COMMANDS
# ==============================

@bot.event
async def on_ready():
    logging.info("Бот %s успешно запущен!", bot.user)
    if not daily_top_task.is_running(): daily_top_task.start()
    if not check_updates_task.is_running(): check_updates_task.start()

@bot.command(name="search")
async def search(ctx, *, query: str = None):
    if not query or not query.strip():
        await ctx.send("Напиши название трека.\nПример: `!search Despacito`")
        return
    if not COOKIE_STRING:
        await ctx.send("Ошибка: Cookie Spide не настроены.")
        return
    msg = await ctx.send("🔎 Ищу треки в Spide...")
    headers = {"accept": "application/json", "referer": "https://web.spide.ru/search", "user-agent": "Mozilla/5.0", "Cookie": COOKIE_STRING}
    try:
        data = await asyncio.to_thread(search_spide, query.strip(), headers)
        tracks = data.get("tracks", [])
        if not tracks:
            await msg.edit(content=f"По запросу **{query}** ничего не найдено.")
            return
        embed, track_url = create_track_embed(tracks[0], ctx, query.strip())
        await msg.edit(content=None, embed=embed, view=TrackView(track_url))
    except Exception as e:
        await msg.edit(content=f"Ошибка поиска: `{str(e)[:500]}`")

@bot.command(name="top")
async def top_now(ctx):
    msg = await ctx.send("⏳ Собираю топ...")
    await send_top_to_channel(ctx.channel)
    await msg.delete()

# КОМАНДА ДЛЯ ПРИНУДИТЕЛЬНОЙ ПРОВЕРКИ ТГК (чтобы не ждать 30 минут)
@bot.command(name="checktg")
async def checktg_now(ctx):
    await ctx.send("🔍 Проверяю Telegram-канал...")
    await check_updates_task()
    await ctx.send("✅ Проверка завершена! Если были новые посты, я их скинул.")

# ==============================
# START
# ==============================

if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        logging.error("ОШИБКА: Не задан DISCORD_TOKEN!")
