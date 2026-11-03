import discord
from discord import app_commands
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
from dashboard.admin_panel import register_admin_panel

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
    "permissions": {},
    "automation": {
        "telegram_enabled": True,
        "telegram_interval_minutes": 10,
        "daily_top_enabled": True,
        "daily_top_hour_utc": 0,
        "daily_top_minute_utc": 22
    },
    "automation_state": {},
    "notifications": {
        "enabled": False,
        "channel_id": 0,
        "notify_startup": True,
        "notify_disconnect": True,
        "notify_errors": False
    }
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
WELCOME_CHANNEL_ID = 1566767398588125194 # Твой новый ID для приветствий

# 🚨 ГЛОБАЛЬНАЯ ПРОВЕРКА ПРАВ
@bot.check
async def global_permission_check(ctx):
    if ctx.guild is None:
        return True
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
def _role_assignment_error(guild, role):
    """Return a readable reason when the bot cannot assign the requested role."""
    if role is None:
        return "Эта роль не найдена на сервере. Попросите администратора проверить роли."
    if role.is_default() or role.managed:
        return "Эту роль нельзя выдать через меню. Выберите обычную роль сервера."

    bot_member = getattr(guild, "me", None)
    if bot_member is None and bot.user is not None:
        bot_member = guild.get_member(bot.user.id)
    if bot_member is None:
        return "Не удалось определить участника SpideBot на сервере."

    if not bot_member.guild_permissions.manage_roles:
        return "У SpideBot нет права **Управлять ролями**. Администратору нужно включить его в настройках роли бота."
    if role >= bot_member.top_role:
        return (
            f"Роль **{role.name}** стоит выше или на одном уровне с высшей ролью SpideBot. "
            "Переместите роль бота выше неё в настройках сервера."
        )
    return None


class RoleSelectView(discord.ui.View):
    """Persistent self-service platform-role selector."""

    def __init__(self):
        super().__init__(timeout=None)

    async def handle_role(self, interaction: discord.Interaction, role_name: str):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Выбор ролей доступен только на Discord-сервере.",
                ephemeral=True,
            )
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return

        member = interaction.user
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        problem = _role_assignment_error(interaction.guild, role)
        if problem:
            await interaction.followup.send(f"❌ {problem}", ephemeral=True)
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Самостоятельный выбор роли через SpideBot")
                await interaction.followup.send(f"✅ Роль **{role_name}** снята.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Самостоятельный выбор роли через SpideBot")
                await interaction.followup.send(f"✅ Роль **{role_name}** выдана!", ephemeral=True)
        except discord.Forbidden:
            logging.exception(
                "Discord запретил изменение роли %s для пользователя %s",
                role_name,
                getattr(member, "id", "unknown"),
            )
            await interaction.followup.send(
                "❌ Discord отклонил изменение роли. Проверьте право **Управлять ролями** "
                "и положение высшей роли SpideBot.",
                ephemeral=True,
            )
        except discord.HTTPException:
            logging.exception("Discord API вернул ошибку при изменении роли %s", role_name)
            await interaction.followup.send(
                "❌ Discord не смог изменить роль из-за ошибки API. Попробуй ещё раз позже.",
                ephemeral=True,
            )

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
    role = discord.utils.get(member.guild.roles, name="🕷 Участник")
    if role:
        try:
            await member.add_roles(role)
            logging.info(f"Выдана роль '🕷 Участник' новому участнику {member.name}")
        except discord.Forbidden:
            logging.error(f"Не удалось выдать роль {member.name}. Проверь иерархию ролей!")
    else:
        logging.warning("Роль '🕷 Участник' не найдена на сервере!")

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
# Session secret is initialized by dashboard.admin_panel using SECRET_KEY or a local key file.

@web_app.route('/')
def index():
    is_ready = bot.is_ready()
    guild_count = len(bot.guilds) if is_ready else 0
    ping = round(bot.latency * 1000) if is_ready else 0
    logs = list(log_buffer)[-20:]
    
    guilds = [{"id": g.id, "name": g.name} for g in bot.guilds] if is_ready else []
    # Добавили commands в список для админки
    restricted_commands = ["search", "top", "links", "checktg", "rules", "setup_roles", "commands"]

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
# SPIDEBOT ADMIN V2 INTEGRATION
# ==============================
def _admin_queue_checktg():
    if not bot.loop or not bot.loop.is_running():
        raise RuntimeError("Discord event loop is not running")
    asyncio.run_coroutine_threadsafe(force_checktg(), bot.loop)

def _admin_queue_top():
    if not bot.loop or not bot.loop.is_running():
        raise RuntimeError("Discord event loop is not running")
    channel = bot.get_channel(TOP_CHANNEL_ID)
    if channel is None:
        raise RuntimeError("TOP_CHANNEL_ID is not available to the bot")
    asyncio.run_coroutine_threadsafe(force_top(channel), bot.loop)

def _admin_queue_cookie_test():
    if not bot.loop or not bot.loop.is_running():
        raise RuntimeError("Discord event loop is not running")
    asyncio.run_coroutine_threadsafe(test_spide_cookie(), bot.loop)

def _admin_get_top_data():
    cookie = app_config.get("SPIDE_COOKIE", "")
    if not cookie:
        raise RuntimeError("Spide cookie is not configured")
    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0", "Cookie": cookie}
    response = requests.get(SPIDE_TOP_LISTENERS, headers=headers, impersonate="chrome", timeout=20)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Unexpected Spide API response")
    return data

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

def create_track_embed(track, author, query):
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
    embed.set_footer(text=f"SPIDE MUSIC  •  Запросил {author.display_name}", icon_url=author.display_avatar.url)
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

def _build_listener_top_embed(data):
    """Build the Spide listener ranking embed with medals reserved for places 1–3."""
    leaders = (data.get("rows") or [])[:10]
    total = data.get("total", "Неизвестно")
    date_str = str(data.get("day") or "Сегодня")[:80]

    def safe_score(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def format_score(value):
        return f"{safe_score(value):,}".replace(",", " ")

    def clean_name(user, limit=70):
        name = str((user or {}).get("displayName") or "Неизвестно")
        return name.replace("\n", " ").replace("\r", " ").strip()[:limit] or "Неизвестно"

    embed = discord.Embed(
        title="🏆 ТОП-10 СЛУШАТЕЛЕЙ SPIDE",
        description=(
            f"**{date_str}**  ·  Всего участников: **{total}**\n"
            "Рейтинг самых активных слушателей Spide Music"
        ),
        color=0xFF8738,
        timestamp=discord.utils.utcnow()
    )
    embed.set_author(name="SPIDE MUSIC  /  LISTENER RANKING")

    medals = ("🥇", "🥈", "🥉")
    place_labels = ("1 МЕСТО", "2 МЕСТО", "3 МЕСТО")

    # Medals are reserved exclusively for the podium.
    for index, row in enumerate(leaders[:3]):
        user = row.get("user") or {}
        embed.add_field(
            name=f"{medals[index]}  {place_labels[index]}",
            value=f"**{clean_name(user)}**\n**{format_score(row.get('score'))} минут**",
            inline=True
        )

        if index == 0:
            photo = user.get("photoUrl")
            if isinstance(photo, str) and photo.startswith(("https://", "http://")):
                embed.set_thumbnail(url=photo)

    if len(leaders) > 3:
        other_lines = []
        for place, row in enumerate(leaders[3:10], start=4):
            user = row.get("user") or {}
            name = clean_name(user, limit=65)
            score = format_score(row.get("score"))
            # Plain rank numbers only: no medals or rank emojis after third place.
            other_lines.append(f"**{place:02d}.**  {name}  ·  **{score} мин**")

        embed.add_field(
            name="ОСТАЛЬНЫЕ УЧАСТНИКИ",
            value="\n".join(other_lines),
            inline=False
        )

    embed.set_footer(text="SPIDE MUSIC  •  Ежедневный рейтинг")
    return embed


async def force_top(target_channel):
    global app_config
    if target_channel is None:
        return False

    app_config = load_config()
    cookie = app_config.get("SPIDE_COOKIE", "")

    if not cookie:
        await target_channel.send("❌ Ошибка: Куки Spide не заданы.")
        return False

    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0", "Cookie": cookie}

    try:
        response = await asyncio.to_thread(
            requests.get,
            SPIDE_TOP_LISTENERS,
            headers=headers,
            impersonate="chrome",
            timeout=20
        )

        if response.status_code != 200:
            err_msg = f"❌ Ошибка Spide: код {response.status_code}. Куки могли устареть."
            logging.error(err_msg)
            await target_channel.send(err_msg)
            return False

        data = response.json()
        if not isinstance(data, dict) or not data.get("rows"):
            logging.error("Spide API вернул пустой или некорректный рейтинг: %s", str(data)[:300])
            await target_channel.send("⚠️ Не удалось получить список слушателей Spide.")
            return False

        embed = _build_listener_top_embed(data)
        await target_channel.send(embed=embed)
        return True

    except Exception as exc:
        logging.exception("Ошибка получения топа Spide")
        await target_channel.send(f"❌ Ошибка получения топа: {str(exc)[:500]}")
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
# ==============================
# AUTOMATION, NOTIFICATIONS & MODERATION
# ==============================
_ready_notification_sent = False
_slash_commands_synced = False
_persistent_role_view_registered = False

async def send_admin_notification(message, force=False):
    settings = app_config.get("notifications", {})
    if not force and not settings.get("enabled", False):
        return False
    channel_id = int(settings.get("channel_id") or 0)
    if not channel_id:
        logging.warning("Уведомления SpideBot: не выбран Discord-канал.")
        return False
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            logging.exception("Не удалось получить канал уведомлений %s", channel_id)
            return False
    try:
        embed = discord.Embed(
            description=str(message)[:4000],
            color=ORANGE,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name="SPIDEBOT • SYSTEM NOTIFICATION")
        await channel.send(embed=embed)
        return True
    except Exception:
        logging.exception("Не удалось отправить системное уведомление")
        return False

def _queue_admin_notification(message, force=False):
    if not bot.loop or not bot.loop.is_running():
        raise RuntimeError("Discord event loop is not running")
    return asyncio.run_coroutine_threadsafe(send_admin_notification(message, force=force), bot.loop)

def _admin_queue_notification_test():
    settings = app_config.get("notifications", {})
    if not settings.get("channel_id"):
        raise RuntimeError("Сначала выберите канал уведомлений")
    _queue_admin_notification("✅ Тестовое уведомление SpideBot. Канал настроен правильно.", force=True)

async def _moderate_member_async(action, guild_id, user_id, reason, duration_minutes):
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError("Бот не состоит на выбранном Discord-сервере")
    bot_member = guild.me
    if bot_member is None:
        raise RuntimeError("Не удалось определить участника-бота на сервере")
    permissions = bot_member.guild_permissions

    if action == "unban":
        if not permissions.ban_members:
            raise PermissionError("Боту необходимо право Ban Members")
        await guild.unban(discord.Object(id=user_id), reason=reason)
        logging.info("MODERATION: unban user=%s guild=%s reason=%s", user_id, guild_id, reason)
        return

    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound as exc:
        raise RuntimeError("Пользователь не найден на этом сервере") from exc

    if member.id == guild.owner_id:
        raise PermissionError("Нельзя применять модерацию к владельцу сервера")
    if member.top_role >= bot_member.top_role:
        raise PermissionError("Роль пользователя выше или равна высшей роли бота")

    if action == "timeout":
        if not permissions.moderate_members:
            raise PermissionError("Боту необходимо право Moderate Members")
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=duration_minutes)
        await member.timeout(until, reason=reason)
        logging.info("MODERATION: timeout user=%s guild=%s minutes=%s reason=%s", user_id, guild_id, duration_minutes, reason)
    elif action == "kick":
        if not permissions.kick_members:
            raise PermissionError("Боту необходимо право Kick Members")
        await member.kick(reason=reason)
        logging.info("MODERATION: kick user=%s guild=%s reason=%s", user_id, guild_id, reason)
    elif action == "ban":
        if not permissions.ban_members:
            raise PermissionError("Боту необходимо право Ban Members")
        await guild.ban(member, reason=reason, delete_message_seconds=0)
        logging.info("MODERATION: ban user=%s guild=%s reason=%s", user_id, guild_id, reason)
    else:
        raise ValueError("Неизвестное действие модерации")

def _admin_moderation_action(action, guild_id, user_id, reason, duration_minutes):
    if not bot.loop or not bot.loop.is_running():
        raise RuntimeError("Discord event loop is not running")
    future = asyncio.run_coroutine_threadsafe(
        _moderate_member_async(action, guild_id, user_id, reason, duration_minutes),
        bot.loop
    )
    def report_result(done):
        try:
            done.result()
        except Exception as exc:
            logging.error("MODERATION FAILED action=%s user=%s guild=%s: %s", action, user_id, guild_id, exc)
    future.add_done_callback(report_result)
    return future

@tasks.loop(minutes=1)
async def automation_scheduler():
    now = datetime.datetime.now(datetime.timezone.utc)
    settings = app_config.get("automation", {})
    state = app_config.setdefault("automation_state", {})

    if settings.get("telegram_enabled", True):
        interval = max(1, min(1440, int(settings.get("telegram_interval_minutes", 10))))
        last_raw = state.get("last_telegram_check")
        due = True
        if last_raw:
            try:
                last = datetime.datetime.fromisoformat(last_raw)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=datetime.timezone.utc)
                due = now - last >= datetime.timedelta(minutes=interval)
            except (TypeError, ValueError):
                due = True
        if due:
            state["last_telegram_check"] = now.isoformat()
            save_config(app_config)
            try:
                await force_checktg()
            except Exception:
                logging.exception("Ошибка автоматической проверки Telegram")

    if settings.get("daily_top_enabled", True):
        try:
            hour = int(settings.get("daily_top_hour_utc", 0))
            minute = int(settings.get("daily_top_minute_utc", 22))
        except (TypeError, ValueError):
            hour, minute = 0, 22
        today = now.date().isoformat()
        if now.hour == hour and now.minute == minute and state.get("last_daily_top_date") != today:
            state["last_daily_top_date"] = today
            save_config(app_config)
            channel = bot.get_channel(TOP_CHANNEL_ID)
            if channel is not None:
                await force_top(channel)
            else:
                logging.error("Автоматическая публикация топа: канал %s недоступен", TOP_CHANNEL_ID)

register_admin_panel(
    web_app,
    bot,
    get_config=lambda: app_config,
    save_config=save_config,
    get_logs=lambda: list(log_buffer),
    actions={
        "checktg": _admin_queue_checktg,
        "top": _admin_queue_top,
        "cookie": _admin_queue_cookie_test,
        "notification_test": _admin_queue_notification_test,
    },
    get_top_data=_admin_get_top_data,
    data_dir=DATA_DIR,
    url_prefix="/admin",
    moderation_action=_admin_moderation_action,
)

@web_app.before_request
def require_admin_panel_for_legacy_routes():
    # All management now goes through authenticated, CSRF-protected /admin routes.
    if not request.path.startswith("/admin"):
        return redirect("/admin/login")
    return None

# EVENTS & COMMANDS
# ==============================
@bot.event
async def on_ready():
    global _ready_notification_sent, _slash_commands_synced, _persistent_role_view_registered
    logging.info(f"Бот {bot.user} успешно запущен!")

    if not _persistent_role_view_registered:
        bot.add_view(RoleSelectView())
        _persistent_role_view_registered = True
        logging.info("Постоянное меню выбора ролей зарегистрировано")

    if not _slash_commands_synced:
        try:
            synced = await bot.tree.sync()
            logging.info("Синхронизировано slash-команд: %s", len(synced))
            _slash_commands_synced = True
        except Exception:
            logging.exception("Не удалось синхронизировать slash-команды")

    if not automation_scheduler.is_running():
        automation_scheduler.start()
    notification_settings = app_config.get("notifications", {})
    if not _ready_notification_sent and notification_settings.get("notify_startup", True):
        await send_admin_notification("🟢 SpideBot запущен и подключён к Discord.")
        _ready_notification_sent = True

@bot.event
async def on_disconnect():
    notification_settings = app_config.get("notifications", {})
    if notification_settings.get("notify_disconnect", True):
        await send_admin_notification("🔴 SpideBot потерял соединение с Discord Gateway.")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    logging.error("Ошибка Discord-команды %s: %s", getattr(ctx.command, "qualified_name", "unknown"), error)
    settings = app_config.get("notifications", {})
    if settings.get("enabled") and settings.get("notify_errors"):
        await send_admin_notification(
            f"Ошибка команды {getattr(ctx.command, 'qualified_name', 'unknown')} на сервере "
            f"{ctx.guild.id if ctx.guild else 'DM'}: {str(error)[:900]}"
        )

async def _slash_permission_allowed(interaction: discord.Interaction, command_key: str) -> bool:
    """Apply the same role restrictions as prefix commands to slash aliases."""
    if interaction.guild is None:
        return True

    member = interaction.user
    if getattr(member, "guild_permissions", None) and member.guild_permissions.administrator:
        return True
    if await bot.is_owner(member):
        return True

    guild_permissions = app_config.get("permissions", {}).get(str(interaction.guild.id), {})
    allowed_roles = guild_permissions.get(command_key)
    if not allowed_roles:
        return True

    member_role_ids = {str(role.id) for role in getattr(member, "roles", [])}
    if member_role_ids.intersection(str(role_id) for role_id in allowed_roles):
        return True

    await interaction.response.send_message(
        f"❌ У вас нет прав на использование команды {command_key}.",
        ephemeral=True,
    )
    return False


def _build_role_select_embed():
    return discord.Embed(
        title="🎭 Выбор платформы",
        description=(
            "Нажмите на кнопку ниже, чтобы получить роль своей платформы.\n\n"
            "🤖 **Android**\n🌐 **Web**\n💻 **Desktop**"
        ),
        color=ORANGE,
    )


def _build_rules_embed(author):
    embed = discord.Embed(
        title="📜 ПРАВИЛА СООБЩЕСТВА SPIDE",
        description=(
            "Для поддержания дружелюбной и комфортной атмосферы в сообществе, "
            "пожалуйста, ознакомьтесь с нашими правилами. Незнание правил не освобождает от ответственности."
        ),
        color=ORANGE,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url="https://i.ibb.co/FLxXZwvv/199d4da6-4399-4d85-a447-7ed7c90129bf.png")
    embed.add_field(
        name="⚖️ Общие положения",
        value=(
            "**01. Уважение к участникам**\n"
            "Относись к другим участникам с уважением. Запрещены травля, угрозы, дискриминация, намеренные провокации и преследование других пользователей.\n\n"
            "**02. Общение без токсичности**\n"
            "Не устраивай конфликты ради конфликтов, не провоцируй ссоры и не переходи на личности. Конструктивные споры и разные мнения приветствуются.\n\n"
            "**03. Запрет на спам**\n"
            "Не засоряй чаты повторяющимися сообщениями, бессмысленными символами, чрезмерными упоминаниями, рекламой и нежелательными ссылками. Флуд допускается только в предназначенных для него каналах."
        ),
        inline=False,
    )
    embed.add_field(
        name="📂 Контент и порядок",
        value=(
            "**04. Реклама и самопродвижение**\n"
            "Реклама сторонних проектов, серверов, каналов и услуг разрешена только с согласия администрации. Не используй личные сообщения участников для массовой рассылки рекламы.\n\n"
            "**05. Контент и материалы**\n"
            "Запрещены вредоносные файлы и ссылки, шокирующий контент, а также материалы, нарушающие законодательство или правила Discord. Не публикуй чужую личную информацию без согласия её владельца.\n\n"
            "**06. Использование каналов**\n"
            "Соблюдай тематику каналов. Технические вопросы, музыкальные обсуждения, предложения и обычное общение должны находиться в соответствующих разделах."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎙️ Голос и Администрация",
        value=(
            "**07. Голосовые каналы**\n"
            "Не мешай другим участникам общаться: избегай намеренного шума, криков, включения громких звуков и использования звуковых эффектов для помех окружающим.\n\n"
            "**08. Запрет на обход ограничений**\n"
            "Запрещено обходить блокировки и наказания с помощью дополнительных аккаунтов, а также намеренно мешать работе сервера или его ботов.\n\n"
            "**09. Общение с администрацией**\n"
            "Не выдавай себя за представителя команды Spide. Не злоупотребляй жалобами и обращениями. Если не согласен с действиями модератора, обратись к администрации спокойно и с объяснением ситуации.\n\n"
            "**10. Решения администрации**\n"
            "Администрация вправе удалять сообщения и применять меры наказания за нарушения правил. Степень наказания определяется с учётом тяжести нарушения, его последствий и повторяемости."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Spide Music | Пожалуйста, соблюдайте правила",
        icon_url=author.display_avatar.url,
    )
    return embed


def _build_links_embed(author):
    embed = discord.Embed(
        title="🔗 Официальные ресурсы Spide",
        color=0xFF8C00,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url="https://i.ibb.co/kjjqxXY/c92505b4-73cf-4058-b280-6cf3e3fd4b78.png")
    embed.add_field(
        name="Основное",
        value=(
            "👨‍💻 **Разраб:** @spidelond\n"
            "💬 **Телеграм канал:** [t.me/Spidedev](https://t.me/Spidedev)\n"
            "🌐 **Веб версия:** [web.spide.ru](https://web.spide.ru)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Тестеры\\Хелперы",
        value=(
            "**Нимфоманка** — @Bloodthister \\\\ dedmak0193\n"
            "**Ucatatsu** — @Ucatatsu \\\\ ucatatsu\n"
            "**Валерий Александрович** — @prpavshiy \\\\ propavshiysnimok\n"
            "**Нефильтрованный** — @Q_ko1dick \\\\ prosto_mishka19\n"
            "**Смайл** — @Sm1Le_1337 \\\\ sm1le_1337"
        ),
        inline=False,
    )
    embed.set_footer(
        text="Spide Music | Запросил " + author.display_name,
        icon_url=author.display_avatar.url,
    )
    return embed


def _build_commands_embed(author):
    embed = discord.Embed(
        title="📋 Команды SpideBot",
        description=(
            "Все команды вызываются через меню Discord slash-команд. "
            "Доступны русские и английские названия."
        ),
        color=ORANGE,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="🎵 Поиск музыки",
        value="`/search` или `/поиск` — найти трек, альбом или исполнителя. Введи название в поле запроса.",
        inline=False,
    )
    embed.add_field(
        name="🏆 Топ слушателей",
        value="`/top` или `/топ` — опубликовать актуальный топ слушателей в этом канале.",
        inline=False,
    )
    embed.add_field(
        name="🔗 Полезные ссылки",
        value="`/links` или `/ссылки` — официальные ресурсы Spide.",
        inline=False,
    )
    embed.add_field(
        name="📜 Сообщество",
        value="`/rules` или `/правила` — правила сообщества.\n`/commands` или `/команды` — эта справка.",
        inline=False,
    )
    embed.set_footer(
        text="Spide Music | Запросил " + author.display_name,
        icon_url=author.display_avatar.url,
    )
    return embed

async def _handle_slash_search(interaction: discord.Interaction, query: str):
    if not await _slash_permission_allowed(interaction, "search"):
        return

    await interaction.response.defer(thinking=True)
    global app_config
    app_config = load_config()
    cookie = app_config.get("SPIDE_COOKIE", "")
    if not query or not query.strip():
        await interaction.followup.send("Напиши название трека или исполнителя.")
        return
    if not cookie:
        await interaction.followup.send("❌ Куки Spide не настроены.")
        return

    headers = {
        "accept": "application/json",
        "referer": "https://web.spide.ru/search",
        "user-agent": "Mozilla/5.0",
        "Cookie": cookie,
    }
    try:
        data = await asyncio.to_thread(search_spide, query.strip(), headers)
        tracks = data.get("tracks", [])
        if not tracks:
            await interaction.followup.send(f"По запросу {query[:150]} ничего не найдено.")
            return
        embed, track_url = create_track_embed(tracks[0], interaction.user, query.strip())
        await interaction.followup.send(embed=embed, view=TrackView(track_url))
    except Exception:
        logging.exception("Ошибка slash-поиска Spide")
        await interaction.followup.send("❌ Не удалось выполнить поиск. Попробуй ещё раз позже.")


@bot.tree.command(name="search", description="Найти трек, альбом или исполнителя")
@app_commands.describe(query="Название трека или исполнителя")
async def slash_search(interaction: discord.Interaction, query: str):
    await _handle_slash_search(interaction, query)


@bot.tree.command(name="top", description="Показать топ слушателей Spide")
async def slash_top(interaction: discord.Interaction):
    logging.info(
        "Slash /top вызвана пользователем %s в канале %s",
        interaction.user.id,
        getattr(interaction.channel, "id", None),
    )
    if not await _slash_permission_allowed(interaction, "top"):
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    success = await force_top(interaction.channel)
    if success:
        await interaction.followup.send("✅ Топ опубликован в этом канале.", ephemeral=True)
    else:
        await interaction.followup.send(
            "❌ Не удалось получить топ. Проверь SPIDE_COOKIE и логи бота.",
            ephemeral=True,
        )


@bot.tree.command(name="links", description="Официальные ссылки Spide")
async def slash_links(interaction: discord.Interaction):
    if not await _slash_permission_allowed(interaction, "links"):
        return
    await interaction.response.send_message(embed=_build_links_embed(interaction.user))


@bot.tree.command(name="checktg", description="Проверить новые публикации Telegram")
async def slash_checktg(interaction: discord.Interaction):
    if not await _slash_permission_allowed(interaction, "checktg"):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await force_checktg()
        await interaction.followup.send("✅ Проверка Telegram завершена.", ephemeral=True)
    except Exception:
        logging.exception("Ошибка slash-команды checktg")
        await interaction.followup.send("❌ Не удалось проверить Telegram. Посмотри логи бота.", ephemeral=True)


@bot.tree.command(name="rules", description="Показать правила сообщества Spide")
async def slash_rules(interaction: discord.Interaction):
    if not await _slash_permission_allowed(interaction, "rules"):
        return
    await interaction.response.send_message(embed=_build_rules_embed(interaction.user))


@bot.tree.command(name="setup_roles", description="Опубликовать кнопки выбора платформы")
async def slash_setup_roles(interaction: discord.Interaction):
    if not await _slash_permission_allowed(interaction, "setup_roles"):
        return
    if interaction.guild is None or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ Команда доступна только администраторам сервера.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(embed=_build_role_select_embed(), view=RoleSelectView())


@bot.tree.command(name="commands", description="Показать список команд SpideBot")
async def slash_commands(interaction: discord.Interaction):
    if not await _slash_permission_allowed(interaction, "commands"):
        return
    await interaction.response.send_message(embed=_build_commands_embed(interaction.user))


@bot.tree.command(name="поиск", description="Найти трек, альбом или исполнителя")
@app_commands.describe(запрос="Название трека или исполнителя")
async def slash_search_ru(interaction: discord.Interaction, запрос: str):
    await _handle_slash_search(interaction, запрос)


@bot.tree.command(name="топ", description="Показать топ слушателей Spide")
async def slash_top_ru(interaction: discord.Interaction):
    await slash_top(interaction)


@bot.tree.command(name="ссылки", description="Официальные ссылки Spide")
async def slash_links_ru(interaction: discord.Interaction):
    await slash_links.callback(interaction)


@bot.tree.command(name="проверить_тг", description="Проверить новые публикации Telegram")
async def slash_checktg_ru(interaction: discord.Interaction):
    await slash_checktg.callback(interaction)


@bot.tree.command(name="правила", description="Показать правила сообщества Spide")
async def slash_rules_ru(interaction: discord.Interaction):
    await slash_rules.callback(interaction)


@bot.tree.command(name="роли", description="Опубликовать кнопки выбора платформы")
async def slash_setup_roles_ru(interaction: discord.Interaction):
    await slash_setup_roles.callback(interaction)


@bot.tree.command(name="команды", description="Показать список команд SpideBot")
async def slash_commands_ru(interaction: discord.Interaction):
    await slash_commands.callback(interaction)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logging.error(
        "Ошибка slash-команды %s: %s",
        getattr(interaction.command, "name", "unknown"),
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    message = "❌ Произошла ошибка при выполнении команды. Попробуй ещё раз позже."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logging.exception("Не удалось отправить сообщение об ошибке slash-команды")



# ==============================
# START
# ==============================
if __name__ == "__main__":
    if DISCORD_TOKEN:
        start_web_panel()
        bot.run(DISCORD_TOKEN)
    else:
        logging.error("ОШИБКА: Не задан DISCORD_TOKEN!")
