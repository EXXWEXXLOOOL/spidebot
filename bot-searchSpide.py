import discord
from discord.ext import commands
from curl_cffi import requests
import urllib.parse
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

RAW_COOKIE = os.getenv("SPIDE_COOKIE")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
COOKIE_STRING = RAW_COOKIE.strip() if RAW_COOKIE else None

class TrackView(discord.ui.View):
    def __init__(self, url):
        super().__init__()
        self.add_item(discord.ui.Button(label="Слушать в Spide", url=url, emoji="🎵", style=discord.ButtonStyle.link))

@bot.event
async def on_ready():
    logging.info(f'Бот {bot.user} успешно запущен!')

@bot.command(name='search')
async def search(ctx, *, query: str = None):
    if not query:
        await ctx.send("❌ Напишите название трека. Пример: `!search Despacito`")
        return

    if not COOKIE_STRING:
        await ctx.send("❌ Ошибка: Куки Spide не настроены на сервере.")
        return

    msg = await ctx.send("🔎 Ищу...")
    logging.info(f"Пользователь {ctx.author.name} ищет: {query}")

    encoded_query = urllib.parse.quote(query, safe='')
    # ИЗМЕНЕНО: limit=10 вместо 1 (как в рабочем curl)
    url = f"https://web.spide.ru/api/v1/spotify/search?q={encoded_query}&type=all&limit=10"

    # ИЗМЕНЕНО: Добавлены ВСЕ заголовки из твоего рабочего curl
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
        response = requests.get(url, headers=headers, impersonate="chrome")
        logging.info(f"Ответ Spide: Статус {response.status_code}.")
        data = response.json()
        
        if "error" in data:
            await msg.edit(content=f"❌ Ошибка Spide: `{data['error']}`. Куки устарели.")
            return
            
    except Exception as e:
        logging.error(f"Ошибка соединения: {e}")
        await msg.edit(content=f"❌ Ошибка соединения: {e}")
        return

    tracks = data.get("tracks", [])
    
    if not tracks:
        await msg.edit(content=f"🔎 По запросу **{query}** ничего не найдено.")
        return
    
    track = tracks[0]
    title = track.get("title", "Неизвестно")
    cover = track.get("cover")
    duration_ms = track.get("durationMs", 0)
    track_id = track.get("id")
    
    artists = ", ".join([artist.get("name", "") for artist in track.get("artists", [])])
    album_name = track.get("album", {}).get("title", "Сингл")
    
    minutes = duration_ms // 60000
    seconds = (duration_ms % 60000) // 1000
    duration_str = f"{minutes}:{seconds:02d}"
    
    track_url = f"https://web.spide.ru/track/{track_id}"

    embed = discord.Embed(title=f"🎵 {title}", url=track_url, color=0xFF8C00)
    if cover:
        embed.set_thumbnail(url=cover)
        
    embed.add_field(name="👤 Исполнитель", value=f"**{artists}**", inline=True)
    embed.add_field(name="💿 Альбом", value=f"*{album_name}*", inline=True)
    embed.add_field(name="⏱️ Длительность", value=f"`{duration_str}`", inline=True)
    embed.set_footer(text=f"Spide Music | Запросил {ctx.author.name}", icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

    view = TrackView(track_url)
    await msg.edit(content=None, embed=embed, view=view)

if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    logging.error("ОШИБКА: Не задан DISCORD_TOKEN!")
