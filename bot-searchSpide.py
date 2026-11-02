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
from flask import Flask, render_template, request, redirect, url_for, jsonify

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
    "last_ids": {"dsdedmak0193": 0},
    "permissions": {}
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
intents.members = True # Обязательно для приветствий и ролей
bot = commands.Bot(command_prefix="!", intents=intents)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

SPIDE_API = "https://web.spide.ru/api/v1/spotify/search"
SPIDE_SEARCH = "https://web.spide.ru/search"
SPIDE_TOP_LISTENERS = "https://web.spide.ru/api/v1/tops/listeners?tz=Europe/Moscow"

ORANGE = 0xFF8C00

# 🚨 ID КАНАЛОВ
TOP_CHANNEL_ID = 1566895795654107217
UPDATE_CHANNEL_ID = 1566767775001616506
WELCOME_CHANNEL_ID = 1566767398588125194 # 🚨 ТВОЙ НОВЫЙ ID ДЛЯ ПРИВЕТСТВИЙ

# 🚨 ГЛОБАЛЬНАЯ ПРОВЕРКА ПРАВ
@bot.check
async def global_permission_check(ctx):
    if ctx.author.guild_permissions.administrator or await bot.is_owner(ctx.author):
        return True

    cmd_name = ctx.command.name
    guild_id = str(ctx.guild.id)

    perms = app_config.get("permissions", {}).get(guild_id, {})
    allowed_roles = perms.get(cmd_name)

    if not allowed_roles:
        return True

    user_role_ids = [str(role.id) for role in ctx.author.roles]
    if any(role_id in allowed_roles for role_id in user_role_ids):
        return True

    await ctx.send(f"❌ У вас нет прав на использование команды `!{cmd_name}`.")
    return False

# ==============================
# WELCOME MESSAGE & ROLES
# ==============================
class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def handle_role(self, interaction: discord.Interaction, role_name: str):
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if not role:
            await interaction.response.send_message(f"❌ Роль **{role_name}** не найдена.", ephemeral=True)
            return

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(f"✅ Роль **{role_name}** снята.", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ Роль **{role_name}** выдана!", ephemeral=True)

    @discord.ui.button(label="Android", style=discord.ButtonStyle.primary, emoji="🤖", custom_id="role_android")
    async def android_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "Android")

    @discord.ui.button(label="Web", style=discord.ButtonStyle.success, emoji="🌐", custom_id="role_web")
    async def web_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "Web")

    @discord.ui.button(label="Desktop", style=discord.ButtonStyle.secondary, emoji="💻", custom_id="role_desktop")
    async def desktop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "Desktop")

@bot.event
async def on_member_join(member):
    # 🚨 1. ВЫДАЕМ РОЛЬ "🕷 Участник"
    role = discord.utils.get(member.guild.roles, name="🕷 Участник")
    if role:
        try:
            await member.add_roles(role)
            logging.info(f"Выдана роль '🕷 Участник' новому участнику {member.name}")
        except discord.Forbidden:
            logging.error(f"Не удалось выдать роль {member.name}. Проверь иерархию ролей!")
    else:
        logging.warning("Роль '🕷 Участник' не найдена на сервере!")

    # 🚨 2. ОТПРАВЛЯЕМ ПРИВЕТСТВИЕ
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if not channel: return

    welcome_text = (
        "🕷 **Добро пожаловать в Spide!**\n"
        "Рады видеть тебя в нашем сообществе!\n\n"
        "Здесь мы объединяем людей, которые любят музыку, открывают новых исполнителей, делятся плейлистами и вместе развивают Spide.\n\n"
        "**Для начала:**\n"
        "・Ознакомься с правилами.\n"
        "・Выбери интересующие тебя роли.\n"
        "・Загляни в общий чат и познакомься с участниками.\n\n"
        "Устраивайся поудобнее. Приятного прослушивания! 🎧"
    )

    embed = discord.Embed(description=welcome_text, color=ORANGE)
    embed.set_image(url="https://i.ibb.co/cKF3rkLp/04d6bc14-b948-4156-b679-d10c4c0bf67b.png")
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Spide Music • {member.name}")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Слушать Spide", url="https://web.spide.ru", emoji="🎵"))

    await channel.send(content=f"Привет, {member.mention}! 👋", embed=embed, view=view)

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
    
    guilds = [{"id": g.id, "name": g.name} for g in bot.guilds] if is_ready else []
    restricted_commands = ["search", "top", "links", "checktg", "rules", "setup_roles"]

    return render_template(
        'index.html', 
        guilds_count=guild_count, latency=ping, status=is_ready,
        channels=app_config.get("tg_channels", []),
        cookie=app_config.get("SPIDE_COOKIE", ""),
        logs=logs,
        guilds=guilds,
        commands=restricted_commands,
        permissions=app_config.get("permissions", {})
    )

@web_app.route('/api/get_roles/<int:guild_id>')
def get_roles(guild_id):
    guild = bot.get_guild(guild_id)
    if not guild:
        return jsonify([])
    roles = [{"id": str(r.id), "name": r.name} for r in guild.roles if not r.is_default() and not r.managed]
    return jsonify(roles)

@web_app.route('/update_permissions', methods=['POST'])
def update_permissions():
    global app_config
    guild_id = request.form.get('guild_id')
    command = request.form.get('command_name')
    roles = request.form.getlist('roles[]')

    if guild_id and command:
        if "permissions" not in app_config:
            app_config["permissions"] = {}
        if guild_id not in app_config["permissions"]:
            app_config["permissions"][guild_id] = {}

        if roles:
            app_config["permissions"][guild_id][command] = roles
            logging.info(f"Обновлены права для {command} на сервере {guild_id}")
        else:
            if command in app_config["permissions"][guild_id]:
                del app_config["permissions"][guild_id][command]
                logging.info(f"Сняты ограничения для {command} на сервере {guild_id}")
        save_config(app_config)
    return redirect(url_for('index'))

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
    channel = bot.get_channel(TOP_CHANNEL_ID)
    if bot.loop and channel:
        asyncio.run_coroutine_threadsafe(force_top(channel), bot.loop)
        logging.info("Запущена ручная отправка топа.")
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
    
    if not cookie:
        await target_channel.send("❌ Ошибка: Куки Spide не заданы.")
        return False
    if not target_channel: 
        return False

    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0", "Cookie": cookie}
    
    try:
        response = await asyncio.to_thread(requests.get, SPIDE_TOP_LISTENERS, headers=headers, impersonate="chrome", timeout=20)
        
        if response.status_code != 200:
            err_msg = f"❌ Ошибка Spide: код {response.status_code}. Куки протухли."
            logging.error(err_msg)
            await target_channel.send(err_msg)
            return False

        data = response.json()
        leaders = data.get("rows", [])
        
        if not leaders:
            logging.error(f"Не удалось найти список слушателей. Ответ: {str(data)[:300]}")
            await target_channel.send("⚠️ Ошибка: API Spide вернул данные в неизвестном формате.")
            return False

        top_10 = leaders[:10]
        total = data.get("total", "Неизвестно")
        date_str = data.get("day", "Сегодня")

        embed = discord.Embed(
            title="🏆 Топ-10 слушателей Spide", 
            description=f"**Обновлено за:** {date_str}\n**Всего участников:** `{total}`", 
            color=ORANGE,
            timestamp=discord.utils.utcnow()
        )

        medals = ["🥇", "🥈", "🥉"]
        
        for i, row in enumerate(top_10[:3]):
            user = row.get("user", {})
            name = user.get("displayName") or "Неизвестно"
            score = row.get("score", 0)
            formatted_score = f"{score:,}".replace(",", " ")
            embed.add_field(name=f"{medals[i]} {i+1} место", value=f"**{name}**\n`{formatted_score}` мин", inline=True)
            if i == 0 and user.get("photoUrl"): embed.set_thumbnail(url=user["photoUrl"])

        if len(top_10) > 3:
            other_places = ""
            for i, row in enumerate(top_10[3:], 4):
                user = row.get("user", {})
                name = user.get("displayName") or "Неизвестно"
                score = row.get("score", 0)
                formatted_score = f"{score:,}".replace(",", " ")
                other_places += f"`{i:02d}.` **{name}** — `{formatted_score}` мин\n"
            embed.add_field(name="Остальные", value=other_places, inline=False)

        embed.set_footer(text="SPIDE MUSIC  •  Автоматическое обновление")
        await target_channel.send(embed=embed)
        return True
    except Exception as e:
            err_msg = f"❌ Ошибка получения топа: `{e}`"
            logging.error(err_msg)
            await target_channel.send(err_msg)
            return False

@tasks.loop(time=datetime.time(hour=0, minute=22, tzinfo=datetime.timezone.utc))
async def daily_top_task():
    channel = bot.get_channel(TOP_CHANNEL_ID)
    if channel: await force_top(channel)

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
            logging.error(f"❌ Тест куки: ОШИБКА {response.status_code}.")
    except Exception as e:
        logging.error(f"❌ Тест куки: Ошибка - {e}")

# ==============================
# EVENTS & COMMANDS
# ==============================
@bot.event
async def on_ready():
    logging.info(f"Бот {bot.user} успешно запущен!")
    if not daily_top_task.is_running(): daily_top_task.start()
    if not check_updates_task.is_running(): check_updates_task.start()

@bot.command(name="setup_roles")
@commands.has_permissions(administrator=True)
async def setup_roles(ctx):
    embed = discord.Embed(
        title="🎭 Выбор платформы",
        description="Нажмите на кнопку ниже, чтобы получить роль своей платформы.\n\n🤖 **Android**\n🌐 **Web**\n💻 **Desktop**",
        color=ORANGE
    )
    await ctx.send(embed=embed, view=RoleSelectView())
    await ctx.message.delete()

@bot.command(name='rules')
async def rules(ctx):
    await ctx.message.delete()
    embed = discord.Embed(
        title="📜 ПРАВИЛА СООБЩЕСТВА SPIDE",
        description="Добро пожаловать! Для комфортного общения на сервере просим ознакомиться с основными правилами.",
        color=ORANGE,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="⚖️ Основы общения",
        value="**01. Уважение**\nЗапрещены травля, угрозы, дискриминация.\n\n**02. Без токсичности**\nКонструктивные споры приветствуются.\n\n**03. Против спама**\nБез флуда и массовых упоминаний.",
        inline=False
    )
    embed.add_field(
        name="📂 Контент и порядок",
        value="**04. Реклама**\nТолько с разрешения администрации.\n\n**05. Материалы**\nЗапрещены шок-контент и чужие личные данные.\n\n**06. Тематика**\nСоблюдай тематику каналов.",
        inline=False
    )
    embed.add_field(
        name="🎙️ Голос и Администрация",
        value="**07. Голос**\nНе мешай другим.\n\n**08. Обход ограничений**\nЗапрещен.\n\n**09. Жалобы**\nСпокойно и по делу.\n\n**10. Решения админов**\nОбязательны к исполнению.",
        inline=False
    )
    embed.set_footer(text="Spide Music | Пожалуйста, соблюдайте правила", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="search")
async def search(ctx, *, query: str = None):
    global app_config
    app_config = load_config()
    cookie = app_config.get("SPIDE_COOKIE", "")
    if not query or not query.strip():
        await ctx.send("Напиши название трека.\nПример: `!search Despacito`")
        return
    if not cookie:
        await ctx.send("Ошибка: Куки Spide не настроены.")
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
    success = await force_top(ctx.channel)
    if success: await msg.delete()
    else: await msg.edit(content="❌ Не удалось получить топ.")

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
