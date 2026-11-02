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
import json
import collections
from flask import Flask, render_template, request, redirect, url_for

# ==============================
# LOGGING & MEMORY BUFFER
# ==============================
log_buffer = collections.deque(maxlen=50)

class MemoryLogHandler(logging.Handler):
    def emit(self, record):
        log_buffer.append(self.format(record))

logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
handler = MemoryLogHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# ==============================
# CONFIGURATION & STORAGE
# ==============================
DATA_DIR = os.getenv("DATA_DIR", ".")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

DEFAULT_CONFIG = {
    "SPIDE_COOKIE": os.getenv("SPIDE_COOKIE", ""),
    "tg_channels": ["dsdedmak0193"],
    "last_ids": {"dsdedmak0193": 0}
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for key in DEFAULT_CONFIG:
                    if key not in data:
                        data[key] = DEFAULT_CONFIG[key]
                return data
        except Exception as e:
            logging.error(f"Ошибка чтения конфига: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.error(f"Ошибка сохранения конфига: {e}")

app_config = load_config()

# ==============================
# BOT SETUP
# ==============================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

SPIDE_API = "https://web.spide.ru/api/v1/spotify/search"
SPIDE_SEARCH = "https://web.spide.ru/search"
# Эндпоинт для топ-10 (список слушателей)
SPIDE_TOP_LISTENERS = "https://web.spide.ru/api/v1/tops/listeners?tz=Europe/Moscow"

ORANGE = 0xFF8C00

# 🚨 ОБНОВЛЕННЫЕ ID КАНАЛОВ
TOP_CHANNEL_ID = 1566895795654107217      # Твой новый ID для авто-топа
UPDATE_CHANNEL_ID = 1566767775001616506   # Канал для постов из Telegram

# ==============================
# WEB SERVER (FLASK)
# ==============================
web_app = Flask(__name__)
web_app.secret_key = 'spide_super_secret_key'

@web_app.route('/')
def index():
    is_ready = bot.is_ready()
    guild_count = len(bot.guilds) if is_ready else 0
    ping = round(bot.latency * 1000) if is_ready else 0
    logs = list(log_buffer)[-20:]
    
    return render_template(
        'index.html', 
        guilds=guild_count, latency=ping, status=is_ready,
        channels=app_config.get("tg_channels", []),
        cookie=app_config.get("SPIDE_COOKIE", ""),
        logs=logs
    )

@web_app.route('/add_channel', methods=['POST'])
def add_channel():
    global app_config
    raw_name = request.form.get('channel_name', '')
    clean_name = raw_name.strip()
    clean_name = re.sub(r'^(https?://)?(t\.me/)?', '', clean_name)
    clean_name = clean_name.replace('@', '').replace('/', '').strip()

    if clean_name and clean_name not in app_config["tg_channels"]:
        app_config["tg_channels"].append(clean_name)
        app_config["last_ids"][clean_name] = 0
        save_config(app_config)
        logging.info(f"Добавлен новый ТГ канал: {clean_name}")
    return redirect(url_for('index'))

@web_app.route('/delete_channel/<path:channel_name>', methods=['POST'])
def delete_channel(channel_name):
    global app_config
    if channel_name in app_config["tg_channels"]:
        app_config["tg_channels"].remove(channel_name)
        if channel_name in app_config["last_ids"]:
            del app_config["last_ids"][channel_name]
        save_config(app_config)
        logging.info(f"Удален ТГ канал: {channel_name}")
    return redirect(url_for('index'))

@web_app.route('/update_cookie', methods=['POST'])
def update_cookie():
    global app_config
    new_cookie = request.form.get('cookie_value', '').strip()
    if new_cookie:
        app_config["SPIDE_COOKIE"] = new_cookie
        save_config(app_config)
        logging.info("Куки Spide обновлены через веб-панель!")
    return redirect(url_for('index'))

@web_app.route('/trigger_checktg')
def trigger_checktg():
    if bot.loop:
        asyncio.run_coroutine_threadsafe(force_checktg(), bot.loop)
        logging.info("Запущена ручная проверка Telegram.")
    return redirect(url_for('index'))

@web_app.route('/trigger_top')
def trigger_top():
    # Для кнопки на сайте отправляем в канал по умолчанию
    channel = bot.get_channel(TOP_CHANNEL_ID)
    if bot.loop and channel:
        asyncio.run_coroutine_threadsafe(force_top(channel), bot.loop)
        logging.info("Запущена ручная отправка топа в канал по умолчанию.")
    return redirect(url_for('index'))

@web_app.route('/test_cookie')
def test_cookie():
    if bot.loop:
        asyncio.run_coroutine_threadsafe(test_spide_cookie(), bot.loop)
    return redirect(url_for('index'))

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
        self.add_item(discord.ui.Button(label="Слушать в Spide", emoji="▶", style=discord.ButtonStyle.link, url=url))

def format_duration(duration_ms):
    try: duration_ms = int(duration_ms or 0)
    except: duration_ms = 0
    return f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"

def format_artists(artists):
    if not isinstance(artists, list): return "Неизвестный исполнитель"
    names = [str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")]
    return ", ".join(names) if names else "Неизвестный исполнитель"

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
    album_name = track.get("album", {}).get("title", "Сингл")
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
# TELEGRAM PARSER & TASKS
# ==============================
def fetch_all_tg_posts(channel_name):
    headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    url = f"https://t.me/s/{channel_name}"
    try:
        response = requests.get(url, headers=headers, impersonate="chrome", timeout=20)
        if response.status_code != 200: return []
        
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
                if match: image_url = match.group(1)

            if text or image_url:
                posts.append({"id": post_id, "text": text, "image_url": image_url, "channel": channel_name})
        return posts
    except Exception as e:
        logging.error(f"Ошибка парсинга {channel_name}: {e}")
        return []

async def force_checktg():
    global app_config
    app_config = load_config() 
    channel = bot.get_channel(UPDATE_CHANNEL_ID)
    if not channel: return

    for tg_channel in app_config.get("tg_channels", []):
        posts = await asyncio.to_thread(fetch_all_tg_posts, tg_channel)
        if not posts: continue

        last_id = app_config["last_ids"].get(tg_channel, 0)
        if last_id == 0:
            max_id = max(p['id'] for p in posts)
            app_config["last_ids"][tg_channel] = max_id
            save_config(app_config)
            continue

        new_posts = [p for p in posts if p['id'] > last_id]
        new_posts.sort(key=lambda x: x['id'])

        for post in new_posts:
            raw_text = post["text"][:4000] if post["text"] else "*Пост с изображением*"
            quoted_text = "\n".join(f"> {line}" for line in raw_text.split("\n"))
            embed = discord.Embed(title=f"📢 Новый пост в @{tg_channel}", description=quoted_text, color=ORANGE, timestamp=discord.utils.utcnow())
            if post.get("image_url"): embed.set_image(url=post["image_url"])
            embed.add_field(name="Ссылка на пост", value=f"[Открыть в Telegram](https://t.me/{tg_channel}/{post['id']})", inline=False)
            embed.set_footer(text="SPIDE MUSIC  •  Автоматическая рассылка")
            
            try:
                await channel.send(embed=embed)
                await asyncio.sleep(2) 
            except Exception as e:
                logging.error(f"Не удалось отправить пост: {e}")

        if new_posts:
            app_config["last_ids"][tg_channel] = new_posts[-1]['id']
            save_config(app_config)

@tasks.loop(minutes=10)
async def check_updates_task():
    await force_checktg()

async def force_top(target_channel):
    global app_config
    app_config = load_config()
    cookie = app_config.get("SPIDE_COOKIE", "")
    if not cookie or not target_channel: return

    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0", "Cookie": cookie}
    
    try:
        # Используем эндпоинт tops/listeners для получения списка
        response = await asyncio.to_thread(requests.get, SPIDE_TOP_LISTENERS, headers=headers, impersonate="chrome", timeout=20)
        response.raise_for_status()
        data = response.json()
        
        # Ищем массив с лидерами (может называться leaders или listeners)
        leaders = data.get("leaders", [])
        if not leaders and "listeners" in data:
            leaders = data.get("listeners", [])
            
        if not leaders:
            await target_channel.send("⚠️ Не удалось получить список слушателей (возможно, изменился API).")
            return

        # Берем топ-10
        top_10 = leaders[:10]
        total = data.get("total", 0)
        date_str = data.get("day", "Сегодня")

        embed = discord.Embed(
            title="🏆 Топ-10 слушателей Spide", 
            description=f"Обновлено за **{date_str}**\nВсего участников: `{total}`", 
            color=ORANGE,
            timestamp=discord.utils.utcnow()
        )

        # Формируем пьедестал для топ-3
        medals = ["🥇", "🥈", "🥉"]
        for i, leader in enumerate(top_10[:3]):
            name = leader.get("displayName", "Неизвестно")
            # Пытаемся достать очки (score, minutes или listenedMs)
            score = leader.get("score", leader.get("minutes", leader.get("listenedMs", 0)))
            embed.add_field(name=f"{medals[i]} {i+1} место", value=f"**{name}**\n`{score:,}` мин".replace(",", " "), inline=True)
            
            if i == 0 and leader.get("photoUrl"): 
                embed.set_thumbnail(url=leader["photoUrl"])

        # Формируем список для 4-10 места
        if len(top_10) > 3:
            other_places = ""
            for i, leader in enumerate(top_10[3:], 4):
                name = leader.get("displayName", "Неизвестно")
                score = leader.get("score", leader.get("minutes", leader.get("listenedMs", 0)))
                other_places += f"**{i}.** {name} — `{score:,}` мин\n".replace(",", " ")
            
            embed.add_field(name="Остальные", value=other_places, inline=False)

        embed.set_footer(text="SPIDE MUSIC  •  Автоматическое обновление")
        await target_channel.send(embed=embed)
        
        if target_channel.id == TOP_CHANNEL_ID:
            logging.info("Автоматический топ-10 успешно отправлен в канал.")

    except Exception as e:
        logging.error(f"Ошибка получения топа: {e}")

@tasks.loop(time=datetime.time(hour=0, minute=22, tzinfo=datetime.timezone.utc))
async def daily_top_task():
    channel = bot.get_channel(TOP_CHANNEL_ID)
    if channel: 
        await force_top(channel)

async def test_spide_cookie():
    cookie = app_config.get("SPIDE_COOKIE", "")
    if not cookie:
        logging.warning("Тест куки: Куки не заданы!")
        return
    headers = {"accept": "application/json", "Cookie": cookie}
    try:
        response = await asyncio.to_thread(requests.get, f"{SPIDE_API}?q=test&type=all&limit=1", headers=headers, impersonate="chrome", timeout=10)
        if response.status_code == 200:
            logging.info("✅ Тест куки: УСПЕШНО! Куки рабочие.")
        else:
            logging.error(f"❌ Тест куки: ОШИБКА {response.status_code}. Куки протухли.")
    except Exception as e:
        logging.error(f"❌ Тест куки: Ошибка соединения - {e}")

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
    global app_config
    app_config = load_config()
    cookie = app_config.get("SPIDE_COOKIE", "")
    
    if not query or not query.strip():
        await ctx.send("Напиши название трека.\nПример: `!search Despacito`")
        return
    if not cookie:
        await ctx.send("Ошибка: Куки Spide не настроены. Зайдите в веб-панель.")
        return
        
    msg = await ctx.send("🔎 Ищу треки в Spide...")
    headers = {"accept": "application/json", "referer": "https://web.spide.ru/search", "user-agent": "Mozilla/5.0", "Cookie": cookie}
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
    msg = await ctx.send("⏳ Собираю топ-10...")
    # 🚨 ВАЖНО: Передаем ctx.channel, чтобы ответить там, где вызвали команду
    await force_top(ctx.channel)
    await msg.delete()

@bot.command(name="checktg")
async def checktg_now(ctx):
    await ctx.send(f"🔍 Проверяю все Telegram-каналы...")
    await force_checktg()
    await ctx.send("✅ Проверка завершена!")

@bot.command(name='links')
async def links(ctx):
    embed = discord.Embed(title="🔗 Официальные ресурсы Spide", color=0xFF8C00, timestamp=discord.utils.utcnow())
    embed.set_thumbnail(url="https://i.ibb.co/kjjqxXY/c92505b4-73cf-4058-b280-6cf3e3fd4b78.png")
    embed.add_field(name="Основное", value="👨‍💻 **Разраб:** @spidelond\n💬 **Телеграм канал:** [t.me/Spidedev](https://t.me/Spidedev)\n🌐 **Веб версия:** [web.spide.ru](https://web.spide.ru)", inline=False)
    embed.add_field(name="Тестеры\\Хелперы", value="**Нимфоманка** — @Bloodthister \\\\ dedmak0193\n**Ucatatsu** — @Ucatatsu \\\\ ucatatsu\n**Валерий Александрович** — @prpavshiy \\\\ propavshiysnimok\n**Нефильтрованный** — @Q_ko1dick \\\\ prosto_mishka19\n**Смайл** — @Sm1Le_1337 \\\\ sm1le_1337", inline=False)
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
