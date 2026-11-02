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
import threading
from flask import Flask

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

# НАСТРОЙКИ TELEGRAM (твой тестовый канал)
TG_CHANNEL_NAME = "dsdedmak0193" 
TG_URL = f"https://t.me/s/{TG_CHANNEL_NAME}"

ORANGE = 0xFF8C00

# 🚨 ID КАНАЛОВ DISCORD
TOP_CHANNEL_ID = 1422240878105198734      # Канал для ежедневного топа (03:22)
UPDATE_CHANNEL_ID = 1566767775001616506   # Канал для постов из Telegram

DATA_DIR = os.getenv("DATA_DIR", ".")
LAST_UPDATE_FILE = os.path.join(DATA_DIR, "last_update_id.txt")

# ==============================
# WEB SERVER (FLASK)
# ==============================

web_app = Flask(__name__)

@web_app.route('/')
def index():
    return "<h1>Панель управления SpideBot</h1><p>Бот работает и готов к управлению!</p>"

@web_app.route('/health')
def health():
    return "OK", 200

def run_web_server():
    port = int(os.getenv("PORT", 3000))
    web_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

def start_web_panel():
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    logging.info(f"Веб-панель запущена на порту {os.getenv('PORT', 3000)}")

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
    try: duration_ms = int(duration_ms or 0)
    except (ValueError, TypeError): duration_ms = 0
    minutes = duration_ms // 60000
    seconds = (duration_ms % 60000) // 1000
    return f"{minutes}:{seconds:02d}"

def format_artists(artists):
    if not isinstance(artists, list): return "Неизвестный исполнитель"
    names = [str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")]
    return ", ".join(names) if names else "Неизвестный исполнитель"

def get_album_name(album):
    if isinstance(album, dict): return album.get("title") or album.get("name") or "Сингл"
    return album if isinstance(album, str) and album else "Сингл"

def search_spide(query, headers):
    params = urllib.parse.urlencode({"q": query, "type": "all", "limit": 10})
    url = f"{SPIDE_API}?{params}"
    response = requests.get(url, headers=headers, impersonate="chrome", timeout=20)
    response.raise_for_status()
    data = response.json()
    if data.get("error"): raise ValueError(str(data["error"]))
    return data

def create_track_embed(track, ctx, query):
    title = str(track.get("title") or "Неизвестный трек")
    artists = format_artists(track.get("artists", []))
    album_name = get_album_name(track.get("album"))
    duration = format_duration(track.get("durationMs", 0))
    cover = track.get("cover") if isinstance(track.get("cover"), str) and track["cover"].startswith("http") else None
    
    search_query = f"{title} {artists}".strip()
    track_url = f"{SPIDE_SEARCH}?{urllib.parse.urlencode({'q': search_query})}"

    embed = discord.Embed(title=title[:256], url=track_url, description=f"**{artists[:1000]}**", color=ORANGE, timestamp=discord.utils.utcnow())
    embed.set_author(name="SPIDE MUSIC  /  TRACK")
    if cover: embed.set_thumbnail(url=cover)
    embed.add_field(name="АЛЬБОМ", value=f"*{album_name[:1024]}*", inline=True)
    embed.add_field(name="ДЛИТЕЛЬНОСТЬ", value=f"`{duration}`", inline=True)
    embed.add_field(name="ПОИСКОВЫЙ ЗАПРОС", value=f"`{query[:100]}`", inline=False)
    embed.set_footer(text=f"SPIDE MUSIC  •  Запросил {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
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
            post_id_str = msg.get('data-post')
            if not post_id_str: continue
            
            try: post_id = int(post_id_str.split('/')[-1])
            except ValueError: continue
                
            text_div = msg.find('div', class_='tgme_widget_message_text')
            text = text_div.get_text(separator='\n', strip=True) if text_div else ""
            
            image_url = None
            photo_wrap = msg.find('a', class_='tgme_widget_message_photo_wrap')
            if photo_wrap and 'style' in photo_wrap.attrs:
                match = re.search(r"url\('([^']+)'\)", photo_wrap['style'])
                if match:
                    image_url = match.group(1)

            if text or image_url:
                posts.append({"id": post_id, "text": text, "image_url": image_url})
                
        return posts
    except Exception as e:
        logging.error(f"Ошибка парсинга Telegram: {e}")
        return []

# ==============================
# TASKS
# ==============================

@tasks.loop(minutes=10)
async def check_updates_task():
    channel = bot.get_channel(UPDATE_CHANNEL_ID)
    if not channel: return

    posts = await asyncio.to_thread(fetch_all_tg_posts)
    if not posts: return

    last_id = get_last_saved_id()

    if last_id == 0:
        max_id = max(p['id'] for p in posts)
        save_last_id(max_id)
        logging.info(f"Первый запуск парсера. Запомнили ID поста: {max_id}")
        return

    new_posts = [p for p in posts if p['id'] > last_id]
    new_posts.sort(key=lambda x: x['id'])

    for post in new_posts:
        raw_text = post["text"][:4000] if post["text"] else "*Пост с изображением*"
        quoted_text = "\n".join(f"> {line}" for line in raw_text.split("\n"))

        embed = discord.Embed(
            title="📢 Новый пост в Telegram-канале",
            description=quoted_text,
            color=ORANGE,
            timestamp=discord.utils.utcnow()
        )
        
        if post.get("image_url"):
            embed.set_image(url=post["image_url"])

        embed.add_field(name="Ссылка на пост", value=f"[Открыть в Telegram](https://t.me/{TG_CHANNEL_NAME}/{post['id']})", inline=False)
        embed.set_footer(text="SPIDE MUSIC  •  Автоматическая рассылка")
        
        try:
            await channel.send(embed=embed)
            await asyncio.sleep(2) 
        except Exception as e:
            logging.error(f"Не удалось отправить пост {post['id']}: {e}")

    if new_posts:
        save_last_id(new_posts[-1]['id'])
        logging.info(f"Отправлено {len(new_posts)} новых постов.")

async def send_top_to_channel(channel):
    if not COOKIE_STRING: return
    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0", "Cookie": COOKIE_STRING}
    try:
        response = await asyncio.to_thread(requests.get, SPIDE_TOP_SUMMARY, headers=headers, impersonate="chrome", timeout=20)
        response.raise_for_status()
        data = response.json()
        listeners_board = next((b for b in data.get("boards", []) if b.get("board") == "listeners"), None)
        if not listeners_board: return

        leaders = listeners_board.get("leaders", [])[:3]
        total = listeners_board.get("total", 0)
        embed = discord.Embed(title="🏆 Топ слушателей Spide", description=f"Обновлено за **{data.get('day', 'Сегодня')}**\nВсего участников: `{total}`", color=ORANGE)
        for i, leader in enumerate(leaders):
            medals = ["🥇", "🥈", "🥉"]
            embed.add_field(name=f"{medals[i]} {i+1} место", value=f"**{leader.get('displayName', 'Неизвестно')}**", inline=True)
            if i == 0 and leader.get("photoUrl"): embed.set_thumbnail(url=leader["photoUrl"])
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
    logging.info(f"Бот {bot.user} успешно запущен!")
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

@bot.command(name="checktg")
async def checktg_now(ctx):
    await ctx.send(f"🔍 Проверяю Telegram-канал... (посты уйдут в <#{UPDATE_CHANNEL_ID}>)")
    await check_updates_task()
    await ctx.send("✅ Проверка завершена!")

@bot.command(name='links')
async def links(ctx):
    embed = discord.Embed(
        title="🔗 Официальные ресурсы Spide",
        color=0xFF8C00,
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url="https://i.ibb.co/kjjqxXY/c92505b4-73cf-4058-b280-6cf3e3fd4b78.png")

    embed.add_field(
        name="Основное",
        value="👨‍💻 **Разраб:** @spidelond\n"
              "💬 **Телеграм канал:** [t.me/Spidedev](https://t.me/Spidedev)\n"
              "🌐 **Веб версия:** [web.spide.ru](https://web.spide.ru)",
        inline=False
    )

    embed.add_field(
        name="Тестеры\\Хелперы",
        value="**Нимфоманка** — @Bloodthister \\\\ dedmak0193\n"
              "**Ucatatsu** — @Ucatatsu \\\\ ucatatsu\n"
              "**Валерий Александрович** — @prpavshiy \\\\ propavshiysnimok\n"
              "**Нефильтрованный** — @Q_ko1dick \\\\ prosto_mishka19\n"
              "**Смайл** — @Sm1Le_1337 \\\\ sm1le_1337",
        inline=False
    )

    embed.set_footer(text="Spide Music | Запросил " + ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

# ==============================
# START
# ==============================

if __name__ == "__main__":
    if DISCORD_TOKEN:
        start_web_panel()
        bot.run(DISCORD_TOKEN)
    else:
        logging.error("ОШИБКА: Не задан DISCORD_TOKEN!")
