import discord
from discord.ext import commands
import urllib.parse
import subprocess
import json

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ВСТАВЬТЕ ВАШУ СТРОКУ COOKIE ЦЕЛИКОМ (всё, что в кавычках после -b)
COOKIE_STRING = "_ym_uid=1792670703870255289; _ym_d=1792670703; __Host-spide_did=0juwzzuQh9gROqnDBED8CAQKEk04OdKU; spide_rail=0; spide_sidebar_w=320; g_state={\"i_l\":0,\"i_ll\":1793543720166,\"i_e\":{\"enable_itp_optimization\":24},\"i_et\":1793543720166}; _ym_isad=1; mdd=0; __Host-spide_at=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6IjI0NzNiMmE4ZWRjM2JkMjIifQ.eyJ0eXAiOiJhY2Nlc3MiLCJzaWQiOiI5YmRkMWQ4Yi03ZWYwLTQzMzItYjYxNi1lMjc0MzQ3ZjFkYjUiLCJpYXQiOjE3OTM2Mzc0OTksImV4cCI6MTc5MzYzODM5OSwiYXVkIjoic3BpZGUtYW5kcm9pZCIsImlzcyI6Imh0dHBzOi8vYXBpLnNwaWRlLnJ1Iiwic3ViIjoiNWE0Y2I2MDEtOTYwOS00ZTNlLTgyZjQtZDhmMmY2MzRkYWIyIiwianRpIjoiNzVhMDhhM2MtZjA4Ni00ZDJlLTlkNWUtMjVhMDhiMDBjYTY0In0.gAJTaGGZZH7lz7iFUKMIBQqv0rwBRk4qk4yZrds3TNc; __Host-spide_rt=8T2XtaelP-qQJGDgsQV7Dadh81_wEZP00OIakmUveROrgWqFtz4n0ynsWgd_Hwd2"

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

    msg = await ctx.send("🔎 Ищу трек...")

    encoded_query = urllib.parse.quote(query, safe='')
    url = f"https://web.spide.ru/api/v1/spotify/search?q={encoded_query}&type=all&limit=1"

    # Собираем команду curl точь-в-точь как в браузере
    cmd = [
        "curl", "-s", "-X", "GET", url,
        "-H", "accept: application/json",
        "-H", "accept-language: ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "-H", "cache-control: no-cache",
        "-H", f"cookie: {COOKIE_STRING}", # Передаем куки
        "-H", "pragma: no-cache",
        "-H", "priority: u=1, i",
        "-H", "referer: https://web.spide.ru/search",
        "-H", 'sec-ch-ua: "Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
        "-H", "sec-ch-ua-mobile: ?0",
        "-H", 'sec-ch-ua-platform: "Windows"',
        "-H", "sec-fetch-dest: empty",
        "-H", "sec-fetch-mode: cors",
        "-H", "sec-fetch-site: same-origin",
        "-H", "user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "-H", "x-audio-fallback: 2"
    ]

    try:
        # Запускаем curl и получаем ответ
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        
        if result.returncode != 0:
            await msg.edit(content=f"❌ Ошибка выполнения curl: {result.stderr}")
            return
            
        # Парсим JSON, который вернул сервер
        data = json.loads(result.stdout)
        
    except json.JSONDecodeError:
        await msg.edit(content=f"❌ Ошибка сервера: не удалось прочитать ответ. Проверьте куки.")
        print(f"Ответ сервера: {result.stdout}")
        return
    except Exception as e:
        await msg.edit(content=f"❌ Неизвестная ошибка: {e}")
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

bot.run("MTU2NjgxNjc2NTk2MTQ0NTQ4Ng.GY75rX.t7nRIIGO8ezH7ax-RVZhy9tYC9Zcmw2MFKvM94")