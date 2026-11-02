import discord
from discord.ext import commands
from curl_cffi import requests # Используем библиотеку Python
import urllib.parse
import os

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Берем данные из переменных окружения Bothost
RAW_COOKIE = os.getenv("SPIDE_COOKIE")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Очищаем куки от невидимых символов (защита от ошибки latin-1)
if RAW_COOKIE:
    COOKIE_STRING = RAW_COOKIE.encode('latin-1', errors='ignore').decode('latin-1')
else:
    COOKIE_STRING = None

class TrackView(discord.ui.View):
    def __init__(self, url):
        super().__init__()
        self.add_item(discord.ui.Button(label="Слушать в Spide", url=url, emoji="🎵", style=discord.ButtonStyle.link))

@bot.event
async def on_ready():
    print(f'Бот {bot.user} успешно запущен!')

@bot.command(name='search')
async def search(ctx, *, query: str = None):
    if not query:
        await ctx.send("❌ Напишите название трека. Пример: `!search Despacito`")
        return

    if not COOKIE_STRING:
        await ctx.send("❌ Ошибка: Куки Spide не настроены на сервере.")
        return

    msg = await ctx.send("🔎 Ищу трек...")

    encoded_query = urllib.parse.quote(query, safe='')
    url = f"https://web.spide.ru/api/v1/spotify/search?q={encoded_query}&type=all&limit=1"

    # Заголовки для запроса
    headers = {
        "accept": "application/json",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "referer": "https://web.spide.ru/search",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Cookie": COOKIE_STRING
    }

    try:
        # Делаем запрос через curl_cffi, притворяясь браузером Chrome
        response = requests.get(url, headers=headers, impersonate="chrome")
        
        if response.status_code != 200:
            print(f"Ошибка API: {response.status_code} - {response.text}")
            await msg.edit(content=f"❌ Ошибка API Spide (код {response.status_code}). Возможно, устарели куки.")
            return
            
        data = response.json()
        
    except Exception as e:
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

    embed = discord.Embed(
        title=f"🎵 {title}",
        url=track_url,
        color=0xFF8C00
    )
    
    if cover:
        embed.set_thumbnail(url=cover)
        
    embed.add_field(name="👤 Исполнитель", value=f"**{artists}**", inline=True)
    embed.add_field(name="💿 Альбом", value=f"*{album_name}*", inline=True)
    embed.add_field(name="⏱️ Длительность", value=f"`{duration_str}`", inline=True)
    
    embed.set_footer(
        text=f"Spide Music | Запросил {ctx.author.name}", 
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None
    )

    view = TrackView(track_url)
    await msg.edit(content=None, embed=embed, view=view)

# Запуск бота
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("ОШИБКА: Не задан DISCORD_TOKEN в переменных окружения!")
