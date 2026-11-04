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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import secrets
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
GIVEAWAYS_FILE = os.path.join(DATA_DIR, "giveaways.json")
DAILY_TOP_HISTORY_FILE = os.path.join(DATA_DIR, "daily_top_history.json")
CUSTOM_ROLE_FILE = os.path.join(DATA_DIR, "custom_roles.json")
CUSTOM_ROLE_STORAGE_LOCK = threading.RLock()
CUSTOM_ROLE_CREATION_LOCK = asyncio.Lock()
DAILY_TOP_HISTORY_LIMIT = 100
DAILY_TOP_HISTORY_LOCK = threading.RLock()
GIVEAWAY_STORAGE_LOCK = threading.RLock()

DEFAULT_CONFIG = {
    "config_version": 2,
    "SPIDE_COOKIE": os.getenv("SPIDE_COOKIE", ""),
    "tg_channels": ["dsdedmak0193"],
    "last_ids": {"dsdedmak0193": 0},
    "permissions": {},
    "automation": {
        "telegram_enabled": True,
        "telegram_interval_minutes": 10,
        "daily_top_enabled": True,
        "daily_top_hour_utc": 3,
        "daily_top_minute_utc": 22,
        "daily_top_hour": 3,
        "daily_top_minute": 22,
        "daily_top_timezone": "UTC",
        "daily_top_channel_id": 1566895795654107217
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

            existing_automation = data.get("automation", {})
            if not isinstance(existing_automation, dict):
                existing_automation = {}
            had_local_top_hour = "daily_top_hour" in existing_automation
            had_local_top_minute = "daily_top_minute" in existing_automation
            had_top_timezone = "daily_top_timezone" in existing_automation
            had_top_channel = "daily_top_channel_id" in existing_automation
            legacy_top_hour = existing_automation.get("daily_top_hour_utc", 3)
            legacy_top_minute = existing_automation.get("daily_top_minute_utc", 22)

            changed = False
            try:
                config_version = int(data.get("config_version", 1))
            except (TypeError, ValueError):
                config_version = 1

            for key, default_value in DEFAULT_CONFIG.items():
                if key not in data:
                    data[key] = json.loads(json.dumps(default_value))
                    changed = True
                elif isinstance(default_value, dict) and isinstance(data.get(key), dict):
                    for nested_key, nested_value in default_value.items():
                        if nested_key not in data[key]:
                            data[key][nested_key] = nested_value
                            changed = True

            # Previous releases defaulted to 00:22 UTC. Move only that old
            # untouched default to the requested 03:22 UTC; preserve custom times.
            if config_version < 2:
                automation = data.setdefault("automation", {})
                if (
                    automation.get("daily_top_hour_utc") == 0
                    and automation.get("daily_top_minute_utc") == 22
                ):
                    automation["daily_top_hour_utc"] = 3
                    changed = True
                data["config_version"] = 2
                changed = True

            automation = data.setdefault("automation", {})
            if not had_local_top_hour:
                try:
                    automation["daily_top_hour"] = int(
                        automation.get("daily_top_hour_utc", legacy_top_hour)
                    )
                except (TypeError, ValueError):
                    automation["daily_top_hour"] = 3
                changed = True
            if not had_local_top_minute:
                try:
                    automation["daily_top_minute"] = int(
                        automation.get("daily_top_minute_utc", legacy_top_minute)
                    )
                except (TypeError, ValueError):
                    automation["daily_top_minute"] = 22
                changed = True
            if not had_top_timezone:
                automation["daily_top_timezone"] = "UTC"
                changed = True
            if not had_top_channel:
                automation["daily_top_channel_id"] = 1566895795654107217
                changed = True

            if changed:
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
            return data
        except Exception as e:
            logging.error(f"Ошибка чтения конфига: {e}")
    return json.loads(json.dumps(DEFAULT_CONFIG))

def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.error(f"Ошибка сохранения конфига: {e}")

app_config = load_config()

def _load_giveaways():
    """Load active and completed giveaways from persistent storage."""
    with GIVEAWAY_STORAGE_LOCK:
        if not os.path.exists(GIVEAWAYS_FILE):
            return {}
        try:
            with open(GIVEAWAYS_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            logging.exception("Не удалось прочитать файл розыгрышей")
            return {}


def _save_giveaways(giveaways):
    """Atomically persist giveaway state so entries survive bot restarts."""
    with GIVEAWAY_STORAGE_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        temporary_path = GIVEAWAYS_FILE + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(giveaways, file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, GIVEAWAYS_FILE)


def _parse_giveaway_draw_at(value, timezone_name="Europe/Moscow"):
    """Parse a giveaway date/time in the supplied IANA timezone unless it has an explicit offset."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        selected_timezone = ZoneInfo(str(timezone_name or ""))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None

    normalized = raw[:-4].strip() + "+00:00" if raw.upper().endswith(" UTC") else raw
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.datetime.strptime(raw, "%d.%m.%Y %H:%M")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=selected_timezone)
    return parsed.astimezone(datetime.timezone.utc)


def _parse_giveaway_local_draw_at(value, timezone_name):
    """Parse a wall-clock time strictly in the selected zone; reject UTC/offset suffixes."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        selected_timezone = ZoneInfo(str(timezone_name or ""))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None

    parsed = None
    for date_format in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            parsed = datetime.datetime.strptime(raw, date_format)
            break
        except ValueError:
            continue
    if parsed is None:
        return None

    local_time = parsed.replace(tzinfo=selected_timezone)
    return local_time.astimezone(datetime.timezone.utc)


def _register_giveaway_participant(giveaway_id, guild_id, user):
    """Register a human member from the giveaway's own server only."""
    giveaways = _load_giveaways()
    giveaway = giveaways.get(str(giveaway_id))
    if giveaway is None:
        return "missing", None
    if str(giveaway.get("guild_id")) != str(guild_id):
        return "wrong_guild", giveaway
    if giveaway.get("status") != "active":
        return "ended", giveaway
    if getattr(user, "bot", False):
        return "bot", giveaway

    try:
        user_id = int(user.id)
    except (AttributeError, TypeError, ValueError):
        return "invalid_user", giveaway

    participants = [int(value) for value in giveaway.get("participants", [])]
    if user_id in participants:
        return "duplicate", giveaway
    participants.append(user_id)
    giveaway["participants"] = participants
    _save_giveaways(giveaways)
    return "added", giveaway


def _get_giveaway_prizes(giveaway):
    """Read per-place prizes while remaining compatible with older single-prize entries."""
    prizes = giveaway.get("prizes")
    if isinstance(prizes, list) and prizes:
        return [str(prize).strip() for prize in prizes if str(prize).strip()]
    legacy_prize = str(giveaway.get("prize") or "Приз").strip()
    return [legacy_prize or "Приз"]


def _parse_giveaway_prizes(places_value, prizes_value):
    """Validate one non-empty prize line for each of 1-10 prize places."""
    try:
        places = int(str(places_value).strip())
    except (TypeError, ValueError):
        return None, None, "Укажи количество мест числом от 1 до 10."

    if places < 1 or places > 10:
        return None, None, "Количество призовых мест должно быть от 1 до 10."

    prizes = [line.strip() for line in str(prizes_value or "").splitlines() if line.strip()]
    if len(prizes) != places:
        return None, None, (
            f"Указано мест: {places}, а призов: {len(prizes)}. "
            f"Добавь ровно {places} строк(и) с призами — по одной на каждое место."
        )
    if any(len(prize) > 120 for prize in prizes):
        return None, None, "Каждый приз должен быть не длиннее 120 символов."
    return places, prizes, None


def _build_giveaway_embed(giveaway):
    """Build the public giveaway card and its multi-winner result."""
    draw_at = _parse_giveaway_draw_at(giveaway.get("draw_at"))
    timestamp = int(draw_at.timestamp()) if draw_at else 0
    participants = giveaway.get("participants", [])
    prizes = _get_giveaway_prizes(giveaway)
    places_count = len(prizes)

    prize_lines = []
    for index, prize in enumerate(prizes, start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"**{index}.**")
        prize_lines.append(f"{medal} {prize}")

    embed = discord.Embed(
        title=f"🎉 РОЗЫГРЫШ • {places_count} мест",
        description="**Призы:**\n" + "\n".join(prize_lines),
        color=ORANGE,
    )
    embed.add_field(
        name="📜 Условия участия",
        value=(str(giveaway.get("conditions") or "").strip()[:1024] or "Без дополнительных условий."),
        inline=False,
    )
    if giveaway.get("status") == "active":
        local_draw_at = draw_at.astimezone(
            ZoneInfo(giveaway.get("timezone", "UTC"))
        ) if draw_at else None
        embed.add_field(
            name="⏰ Когда итоги",
            value=(
                f"<t:{timestamp}:F>\n<t:{timestamp}:R>\n"
                f"Время организатора: **{local_draw_at.strftime('%d.%m.%Y %H:%M')}**\n"
                f"Часовой пояс: {giveaway.get('timezone', 'UTC')}"
                if timestamp and local_draw_at else "Время не задано"
            ),
            inline=True,
        )
        embed.add_field(name="👥 Участников", value=str(len(participants)), inline=True)
        embed.set_footer(text=f"Организатор: {giveaway.get('creator_name', 'Администратор')} • Нажми кнопку, чтобы участвовать")
    else:
        winners = giveaway.get("winner_ids")
        if not isinstance(winners, list):
            legacy_winner = giveaway.get("winner_id")
            winners = [legacy_winner] if legacy_winner else []
        result_lines = []
        for index, prize in enumerate(prizes):
            winner_id = winners[index] if index < len(winners) else None
            medal = {0: "🥇", 1: "🥈", 2: "🥉"}.get(index, f"**{index + 1}.**")
            if winner_id:
                result_lines.append(f"{medal} **{prize}** — <@{winner_id}>")
            else:
                result_lines.append(f"{medal} **{prize}** — победителя нет (не хватило участников)")
        # Discord limits an embed field to 1024 characters; split long result lists.
        for start in range(0, len(result_lines), 4):
            group = result_lines[start:start + 4]
            first_place = start + 1
            last_place = start + len(group)
            embed.add_field(
                name=f"🏆 Результаты • места {first_place}–{last_place}",
                value="\n".join(group),
                inline=False,
            )
        embed.add_field(name="👥 Участников", value=str(len(participants)), inline=True)
        embed.set_footer(text=f"Розыгрыш завершён • Организатор: {giveaway.get('creator_name', 'Администратор')}")
    return embed

class GiveawayJoinView(discord.ui.View):
    """Persistent entry button for a giveaway announcement."""

    def __init__(self, giveaway_id, participant_count=0, disabled=False):
        super().__init__(timeout=None)
        self.giveaway_id = str(giveaway_id)
        self.join_button = discord.ui.Button(
            label="Розыгрыш завершён" if disabled else f"Участвовать ({participant_count})",
            emoji="🎉",
            style=discord.ButtonStyle.secondary if disabled else discord.ButtonStyle.success,
            custom_id=f"spide_giveaway_join:{self.giveaway_id}",
            disabled=disabled,
        )
        self.join_button.callback = self._join
        self.add_item(self.join_button)

    async def _join(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Участвовать можно только на сервере, где опубликован розыгрыш.", ephemeral=True)
            return
        status, giveaway = _register_giveaway_participant(
            self.giveaway_id,
            interaction.guild.id,
            interaction.user,
        )
        messages = {
            "missing": "❌ Розыгрыш не найден.",
            "wrong_guild": "❌ Этот розыгрыш проводится на другом сервере.",
            "ended": "⏰ Этот розыгрыш уже завершён.",
            "bot": "🤖 Боты не могут участвовать в розыгрышах.",
            "duplicate": "Ты уже участвуешь в этом розыгрыше 🎉",
            "invalid_user": "❌ Не удалось определить твой аккаунт.",
        }
        if status != "added":
            await interaction.response.send_message(messages.get(status, "❌ Не удалось присоединиться."), ephemeral=True)
            return

        self.join_button.label = f"Участвовать ({len(giveaway.get('participants', []))})"
        await interaction.response.edit_message(
            embed=_build_giveaway_embed(giveaway),
            view=self,
        )


_registered_giveaway_views = set()


def _register_persistent_giveaway_view(giveaway_id, message_id, participant_count=0, disabled=False):
    key = str(giveaway_id)
    if key in _registered_giveaway_views:
        return
    bot.add_view(
        GiveawayJoinView(key, participant_count=participant_count, disabled=disabled),
        message_id=int(message_id),
    )
    _registered_giveaway_views.add(key)


async def _restore_giveaway_views():
    """Re-register giveaway buttons after reconnects or restarts."""
    for giveaway_id, giveaway in _load_giveaways().items():
        status = giveaway.get("status")
        needs_view = status == "active" or (status == "drawn" and not giveaway.get("result_published"))
        if needs_view and giveaway.get("message_id"):
            try:
                _register_persistent_giveaway_view(
                    giveaway_id,
                    giveaway["message_id"],
                    participant_count=len(giveaway.get("participants", [])),
                    disabled=status != "active",
                )
            except (ValueError, TypeError):
                logging.exception("Не удалось восстановить кнопку розыгрыша %s", giveaway_id)


async def _process_due_giveaways(now=None):
    """Draw winners for due giveaways and retry publishing results after restarts."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    giveaways = _load_giveaways()
    for giveaway_id, giveaway in giveaways.items():
        if giveaway.get("status") == "active":
            draw_at = _parse_giveaway_draw_at(giveaway.get("draw_at"))
            if draw_at is None or draw_at > now:
                continue

            entrants = [
                int(value) for value in giveaway.get("participants", [])
                if str(value).isdigit()
            ]
            prizes = _get_giveaway_prizes(giveaway)
            winners = secrets.SystemRandom().sample(entrants, k=min(len(entrants), len(prizes)))
            giveaway["status"] = "drawn"
            giveaway["winner_ids"] = winners
            giveaway["winner_id"] = winners[0] if winners else None
            giveaway["drawn_at"] = now.astimezone(datetime.timezone.utc).isoformat()
            giveaway["result_published"] = False
            _save_giveaways(giveaways)
        elif giveaway.get("status") != "drawn" or giveaway.get("result_published"):
            continue

        entrants = [
            int(value) for value in giveaway.get("participants", [])
            if str(value).isdigit()
        ]
        winner_id = giveaway.get("winner_id")
        try:
            channel_id = int(giveaway.get("channel_id", 0))
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            message = await channel.fetch_message(int(giveaway.get("message_id", 0)))
            await message.edit(
                embed=_build_giveaway_embed(giveaway),
                view=GiveawayJoinView(
                    giveaway_id,
                    participant_count=len(entrants),
                    disabled=True,
                ),
            )
            giveaway["result_published"] = True
            _save_giveaways(giveaways)
            logging.info(
                "Розыгрыш %s завершён на сервере %s; участников: %s; победитель: %s",
                giveaway_id,
                giveaway.get("guild_id"),
                len(entrants),
                ", ".join(str(value) for value in giveaway.get("winner_ids", [])) or "нет",
            )
        except Exception:
            logging.exception("Не удалось опубликовать итоги розыгрыша %s", giveaway_id)


class GiveawayCreateModal(discord.ui.Modal, title="Создание розыгрыша"):
    places = discord.ui.TextInput(
        label="Количество призовых мест (1–10)",
        placeholder="Например: 5",
        max_length=2,
        style=discord.TextStyle.short,
    )
    prizes = discord.ui.TextInput(
        label="Призы — каждый с новой строки",
        placeholder="Discord Nitro на 1 месяц\nDiscord Nitro на 1 месяц\nDiscord Nitro на 1 месяц",
        max_length=1000,
        style=discord.TextStyle.paragraph,
    )
    draw_time = discord.ui.TextInput(
        label="Когда провести розыгрыш?",
        placeholder="2026-11-10 23:10",
        max_length=40,
        style=discord.TextStyle.short,
    )
    timezone = discord.ui.TextInput(
        label="Часовой пояс (IANA)",
        placeholder="Europe/Moscow",
        default="Europe/Moscow",
        max_length=64,
        style=discord.TextStyle.short,
    )
    conditions = discord.ui.TextInput(
        label="Условия участия",
        placeholder="Например: быть участником сервера и подписаться на канал",
        max_length=1000,
        required=False,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, guild_id, channel_id, creator_id, creator_name):
        super().__init__(timeout=300)
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.creator_id = int(creator_id)
        self.creator_name = str(creator_name)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.creator_id or interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("❌ Эту форму может отправить только создавший её администратор этого сервера.", ephemeral=True)
            return
        if not await _require_administrator(interaction):
            return

        places_count, prize_list, prize_error = _parse_giveaway_prizes(
            self.places.value,
            self.prizes.value,
        )
        timezone_name = str(self.timezone.value or "").strip() or "Europe/Moscow"
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            await interaction.response.send_message(
                "Неизвестный часовой пояс. Например: Europe/Moscow или Europe/Berlin.",
                ephemeral=True,
            )
            return
        raw_draw_time = str(self.draw_time.value or "").strip()
        draw_at = _parse_giveaway_local_draw_at(raw_draw_time, timezone_name)
        if prize_error:
            await interaction.response.send_message(f"❌ {prize_error}", ephemeral=True)
            return
        if draw_at is None:
            await interaction.response.send_message(
                "❌ Не понял дату. Введи только ГГГГ-ММ-ДД ЧЧ:ММ, например 2026-11-10 23:10. Не добавляй UTC или смещение часового пояса — оно берётся из отдельного поля.",
                ephemeral=True,
            )
            return
        if draw_at <= datetime.datetime.now(datetime.timezone.utc):
            await interaction.response.send_message("❌ Время розыгрыша должно быть в будущем.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = bot.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(self.channel_id)
            except discord.HTTPException:
                channel = None
        if channel is None or not hasattr(channel, "send"):
            await interaction.followup.send("❌ Не удалось найти канал, в котором вызвали команду.", ephemeral=True)
            return

        giveaway_id = secrets.token_hex(6)
        giveaway = {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "creator_id": self.creator_id,
            "creator_name": self.creator_name,
            "prize": prize_list[0],
            "prizes": prize_list,
            "places_count": places_count,
            "conditions": str(self.conditions.value or "").strip(),
            "draw_at": draw_at.isoformat(),
            "timezone": timezone_name,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "participants": [],
            "status": "active",
        }
        local_draw_at = draw_at.astimezone(ZoneInfo(timezone_name))
        logging.info(
            "GIVEAWAY_SCHEDULED | giveaway_id=%s | raw_time=%r | timezone=%s | draw_at_utc=%s | draw_at_local=%s",
            giveaway_id,
            raw_draw_time,
            timezone_name,
            draw_at.isoformat(),
            local_draw_at.isoformat(),
        )
        view = GiveawayJoinView(giveaway_id)
        try:
            message = await channel.send(embed=_build_giveaway_embed(giveaway), view=view)
            giveaway["message_id"] = int(message.id)
            all_giveaways = _load_giveaways()
            all_giveaways[giveaway_id] = giveaway
            _save_giveaways(all_giveaways)
            _register_persistent_giveaway_view(giveaway_id, message.id)
        except Exception:
            logging.exception("Не удалось создать розыгрыш на сервере %s", self.guild_id)
            await interaction.followup.send("❌ Не удалось опубликовать розыгрыш. Проверь права бота и попробуй ещё раз.", ephemeral=True)
            return

        draw_timestamp = int(draw_at.timestamp())
        await interaction.followup.send(
            f"✅ Розыгрыш на {places_count} мест опубликован: [перейти к сообщению]({message.jump_url}).\n"
            f"Итоги запланированы на <t:{draw_timestamp}:F> (<t:{draw_timestamp}:R>).\n"
            f"Местное время организатора: **{local_draw_at.strftime('%d.%m.%Y %H:%M')}** ({timezone_name}).",
            ephemeral=True,
        )


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

    @discord.ui.button(label="Своя роль", style=discord.ButtonStyle.secondary, emoji="🎨", custom_id="role_custom")
    async def custom_role_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Эта кнопка работает только на сервере.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Выбери цвет будущей роли. Затем укажи её название.",
            view=CustomRoleColorView(),
            ephemeral=True,
        )

    @discord.ui.button(label="Desktop", style=discord.ButtonStyle.secondary, emoji="💻", custom_id="role_desktop")
    async def desktop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_role(interaction, "Desktop")


CUSTOM_ROLE_COLORS = {
    "Красный": 0xE74C3C,
    "Оранжевый": 0xE67E22,
    "Жёлтый": 0xF1C40F,
    "Зелёный": 0x2ECC71,
    "Бирюзовый": 0x1ABC9C,
    "Голубой": 0x3498DB,
    "Синий": 0x206694,
    "Фиолетовый": 0x9B59B6,
    "Розовый": 0xE91E63,
    "Белый": 0xF2F3F5,
    "Чёрный": 0x23272A,
    "Без цвета": 0x000000,
}


def _load_custom_roles():
    with CUSTOM_ROLE_STORAGE_LOCK:
        if not os.path.exists(CUSTOM_ROLE_FILE):
            return {}
        try:
            with open(CUSTOM_ROLE_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            logging.exception("Не удалось прочитать реестр пользовательских ролей")
            return {}


def _save_custom_roles(data):
    with CUSTOM_ROLE_STORAGE_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        temporary_path = CUSTOM_ROLE_FILE + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, CUSTOM_ROLE_FILE)


class CustomRoleNameModal(discord.ui.Modal, title="Создать свою роль"):
    role_name = discord.ui.TextInput(
        label="Название роли",
        placeholder="Например, Spide Fan",
        min_length=2,
        max_length=30,
        required=True,
    )

    def __init__(self, color_name):
        super().__init__(timeout=180)
        self.color_name = color_name

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ Создание роли доступно только на сервере.", ephemeral=True)
            return

        name = str(self.role_name.value).strip()
        if len(name) < 2 or name in {"@everyone", "@here"}:
            await interaction.response.send_message("❌ Укажи корректное название роли.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        key = f"{guild.id}:{member.id}"
        async with CUSTOM_ROLE_CREATION_LOCK:
            registry = _load_custom_roles()
            existing_id = registry.get(key)
            if existing_id:
                existing_role = guild.get_role(int(existing_id))
                if existing_role is not None:
                    await interaction.followup.send(
                        f"❌ У тебя уже есть кастомная роль: **{existing_role.name}**. "
                        "Один участник может иметь только одну такую роль.",
                        ephemeral=True,
                    )
                    return
                registry.pop(key, None)
                _save_custom_roles(registry)

            participant_role = discord.utils.get(guild.roles, name="🕷 Участник")
            android_role = discord.utils.get(guild.roles, name="Android")
            if participant_role is None or android_role is None:
                await interaction.followup.send(
                    "❌ Не найдены роли **🕷 Участник** или **Android**. "
                    "Администратору нужно проверить их названия.",
                    ephemeral=True,
                )
                return
            if participant_role.position <= android_role.position:
                await interaction.followup.send(
                    "❌ Не получается определить место для кастомной роли: "
                    "роль **🕷 Участник** должна находиться выше **Android**.",
                    ephemeral=True,
                )
                return

            bot_member = guild.me or guild.get_member(bot.user.id if bot.user else 0)
            if bot_member is None or not bot_member.guild_permissions.manage_roles:
                await interaction.followup.send(
                    "❌ У SpideBot нет права **Управлять ролями**.",
                    ephemeral=True,
                )
                return
            if bot_member.top_role.position <= participant_role.position:
                await interaction.followup.send(
                    "❌ Подними высшую роль SpideBot выше роли **🕷 Участник**, "
                    "чтобы бот мог создать и разместить кастомную роль.",
                    ephemeral=True,
                )
                return

            created_role = None
            try:
                created_role = await guild.create_role(
                    name=name,
                    permissions=discord.Permissions.none(),
                    colour=discord.Colour(CUSTOM_ROLE_COLORS[self.color_name]),
                    hoist=False,
                    mentionable=False,
                    reason=f"Кастомная роль участника {member} ({member.id})",
                )
                await created_role.edit(
                    position=android_role.position + 1,
                    reason="Размещение кастомной роли между 🕷 Участник и Android",
                )
                await member.add_roles(created_role, reason="Выдача личной кастомной роли SpideBot")
                registry[key] = str(created_role.id)
                _save_custom_roles(registry)
            except discord.Forbidden:
                if created_role is not None:
                    try:
                        await created_role.delete(reason="Не удалось выдать личную кастомную роль")
                    except discord.HTTPException:
                        logging.exception("Не удалось удалить невыданную кастомную роль %s", created_role.id)
                logging.exception("Discord запретил создание/выдачу кастомной роли участнику %s", member.id)
                await interaction.followup.send(
                    "❌ Discord запретил создать или выдать роль. Проверь право **Управлять ролями** "
                    "и положение роли SpideBot.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                if created_role is not None:
                    try:
                        await created_role.delete(reason="Ошибка при выдаче личной кастомной роли")
                    except discord.HTTPException:
                        logging.exception("Не удалось удалить невыданную кастомную роль %s", created_role.id)
                logging.exception("Ошибка Discord API при создании кастомной роли для %s", member.id)
                await interaction.followup.send(
                    "❌ Не удалось создать роль из-за ошибки Discord. Попробуй ещё раз позже.",
                    ephemeral=True,
                )
                return

        await interaction.followup.send(
            f"✅ Твоя роль **{created_role.name}** создана и выдана! Цвет: **{self.color_name}**.",
            ephemeral=True,
        )


class CustomRoleColorView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        options = [
            discord.SelectOption(label=name, value=name, description=f"Цвет {name.lower()}")
            for name in CUSTOM_ROLE_COLORS
        ]
        self.color_select = discord.ui.Select(
            placeholder="Выбери цвет своей роли",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.color_select.callback = self.select_color
        self.add_item(self.color_select)

    async def select_color(self, interaction: discord.Interaction):
        color_name = self.color_select.values[0]
        await interaction.response.send_modal(CustomRoleNameModal(color_name))


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
    automation = app_config.get("automation", {})
    channel_id = int(automation.get("daily_top_channel_id") or TOP_CHANNEL_ID)
    channel = bot.get_channel(channel_id)
    if channel is None:
        raise RuntimeError(f"Daily top channel {channel_id} is not available to the bot")
    asyncio.run_coroutine_threadsafe(
        _publish_top_with_history(channel, source="manual"),
        bot.loop,
    )

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

async def _publish_top_to_channel(channel):
    """Publish the current top to exactly the supplied channel."""
    if channel is None or not hasattr(channel, "send"):
        logging.error("Публикация топа: целевой канал недоступен или не поддерживает отправку")
        return False
    return await force_top(channel)


def _load_daily_top_history():
    """Read the persistent publication history, newest entries last."""
    try:
        with open(DAILY_TOP_HISTORY_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logging.exception("Не удалось прочитать историю публикаций ежедневного топа")
    return []


def _record_daily_top_publication(channel_id, channel_name, status, source="scheduled", error=None):
    """Append one publication result to persistent history."""
    with DAILY_TOP_HISTORY_LOCK:
        history = _load_daily_top_history()
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "channel_id": int(channel_id or 0),
            "channel_name": str(channel_name or "Неизвестный канал")[:180],
            "status": "success" if status == "success" else "failed",
            "source": "manual" if source == "manual" else "scheduled",
            "error": str(error or "")[:1000],
        }
        history.append(entry)
        history = history[-DAILY_TOP_HISTORY_LIMIT:]
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            temporary_path = DAILY_TOP_HISTORY_FILE + ".tmp"
            with open(temporary_path, "w", encoding="utf-8") as file:
                json.dump(history, file, ensure_ascii=False, indent=2)
            os.replace(temporary_path, DAILY_TOP_HISTORY_FILE)
        except OSError:
            logging.exception("Не удалось сохранить историю публикаций ежедневного топа")
        return entry


def _get_daily_top_channel_name(channel):
    guild_name = getattr(getattr(channel, "guild", None), "name", None)
    channel_name = getattr(channel, "name", None)
    if guild_name and channel_name:
        return f"{guild_name} / #{channel_name}"
    if channel_name:
        return f"#{channel_name}"
    return f"Канал ID {getattr(channel, 'id', 'unknown')}"


async def _publish_top_with_history(channel, source="scheduled", channel_id=None):
    """Publish top and record success/failure for the admin dashboard."""
    resolved_channel_id = channel_id or getattr(channel, "id", 0)
    channel_name = _get_daily_top_channel_name(channel) if channel is not None else f"Канал ID {resolved_channel_id}"
    status = "failed"
    error = ""

    if channel is None or not hasattr(channel, "send"):
        error = "Канал недоступен или не поддерживает отправку сообщений."
        logging.error("Публикация топа не выполнена: %s", error)
    else:
        try:
            succeeded = await _publish_top_to_channel(channel)
            if succeeded:
                status = "success"
            else:
                error = "Не удалось получить или отправить топ. Подробности — в логах и канале Discord."
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            logging.exception("Ошибка публикации топа в %s", resolved_channel_id)

    entry = _record_daily_top_publication(
        resolved_channel_id,
        channel_name,
        status,
        source=source,
        error=error,
    )
    logging.info(
        "DAILY_TOP_PUBLICATION | status=%s | source=%s | channel_id=%s | error=%s",
        status,
        source,
        resolved_channel_id,
        error or "-",
    )
    return status == "success", entry


async def _publish_daily_top():
    """Publish scheduled top to the selected channel and record its outcome."""
    automation = app_config.get("automation", {})
    try:
        channel_id = int(automation.get("daily_top_channel_id") or TOP_CHANNEL_ID)
    except (TypeError, ValueError):
        channel_id = TOP_CHANNEL_ID

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as exc:
            logging.exception("Не удалось получить целевой канал ежедневного топа %s", channel_id)
            _record_daily_top_publication(
                channel_id,
                f"Канал ID {channel_id}",
                "failed",
                source="scheduled",
                error=f"Не удалось получить канал: {str(exc)[:800]}",
            )
            return False

    logging.info("Ежедневный топ будет опубликован в выбранном канале %s", channel_id)
    succeeded, _entry = await _publish_top_with_history(
        channel,
        source="scheduled",
        channel_id=channel_id,
    )
    return succeeded


def _get_next_daily_top_run(now=None, settings=None):
    """Return the next scheduled local datetime; timezone conversion is DST-aware."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    settings = settings or app_config.get("automation", {})
    timezone_name = str(settings.get("daily_top_timezone") or "UTC")
    try:
        schedule_timezone = ZoneInfo(timezone_name)
        hour = int(settings.get("daily_top_hour", settings.get("daily_top_hour_utc", 3)))
        minute = int(settings.get("daily_top_minute", settings.get("daily_top_minute_utc", 22)))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Invalid daily top time")
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        schedule_timezone = datetime.timezone.utc
        timezone_name = "UTC"
        hour, minute = 3, 22

    local_now = now.astimezone(schedule_timezone)
    next_local = datetime.datetime.combine(
        local_now.date(),
        datetime.time(hour=hour, minute=minute),
        tzinfo=schedule_timezone,
    )
    if next_local <= local_now:
        next_local = datetime.datetime.combine(
            local_now.date() + datetime.timedelta(days=1),
            datetime.time(hour=hour, minute=minute),
            tzinfo=schedule_timezone,
        )
    return next_local, timezone_name


def _get_daily_top_dashboard_data():
    settings = app_config.get("automation", {})
    enabled = bool(settings.get("daily_top_enabled", True))
    next_local, timezone_name = _get_next_daily_top_run()
    history = list(reversed(_load_daily_top_history()))[:30]
    last = history[0] if history else None
    return {
        "enabled": enabled,
        "timezone": timezone_name,
        "next_run": next_local.strftime("%d.%m.%Y %H:%M"),
        "next_run_iso": next_local.isoformat(),
        "last": last,
        "history": history,
    }


def _daily_top_schedule_due(now, settings, last_run_date=None):
    """Evaluate the daily top's local wall-clock schedule with DST-aware timezone rules."""
    timezone_name = str(settings.get("daily_top_timezone") or "UTC")
    try:
        schedule_timezone = ZoneInfo(timezone_name)
        hour = int(settings.get("daily_top_hour", settings.get("daily_top_hour_utc", 3)))
        minute = int(settings.get("daily_top_minute", settings.get("daily_top_minute_utc", 22)))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Invalid daily top time")
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        logging.exception("Некорректный часовой пояс или время ежедневного топа; используется UTC")
        schedule_timezone = datetime.timezone.utc
        timezone_name = "UTC"
        hour, minute = 3, 22

    local_now = now.astimezone(schedule_timezone)
    today = local_now.date().isoformat()
    due = (
        local_now.hour == hour
        and local_now.minute == minute
        and last_run_date != today
    )
    return due, today, timezone_name


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
        due, today, timezone_name = _daily_top_schedule_due(
            now,
            settings,
            state.get("last_daily_top_date"),
        )
        if due:
            state["last_daily_top_date"] = today
            save_config(app_config)
            logging.info(
                "Наступило время ежедневного топа: %s (%s)",
                today,
                timezone_name,
            )
            await _publish_daily_top()

    try:
        await _process_due_giveaways(now)
    except Exception:
        logging.exception("Ошибка обработки запланированных розыгрышей")

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
    get_daily_top_dashboard_data=_get_daily_top_dashboard_data,
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

    await _restore_giveaway_views()

    if not _slash_commands_synced:
        try:
            synced = await bot.tree.sync()
            logging.info("Синхронизировано глобальных slash-команд: %s", len(synced))
            _slash_commands_synced = True
        except Exception:
            logging.exception("Не удалось синхронизировать глобальные slash-команды")

    await _sync_giveaway_commands_for_all_guilds()

    if not automation_scheduler.is_running():
        automation_scheduler.start()
    notification_settings = app_config.get("notifications", {})
    if not _ready_notification_sent and notification_settings.get("notify_startup", True):
        await send_admin_notification("🟢 SpideBot запущен и подключён к Discord.")
        _ready_notification_sent = True

@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await _sync_giveaway_command_for_guild(guild)
    except Exception:
        logging.exception(
            "Не удалось зарегистрировать /розыгрыш на новом сервере %s (%s)",
            guild.name,
            guild.id,
        )


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

async def _require_administrator(interaction: discord.Interaction) -> bool:
    """Require the Discord Administrator permission for management commands."""
    permissions = getattr(interaction.user, "guild_permissions", None)
    if interaction.guild is None or permissions is None or not permissions.administrator:
        message = "❌ Эта команда доступна только администраторам сервера."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False
    return True


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
            "Нажмите на кнопку ниже, чтобы получить роль своей платформы.\\n\\n"
            "🤖 **Android**\\n🌐 **Web**\\n💻 **Desktop**\\n🎨 **Своя роль** — создать одну личную роль с выбранным цветом"
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
    # Log the invocation immediately, including attempts rejected by permission checks.
    _log_slash_command_usage(
        interaction,
        command=getattr(interaction, "command", None),
        outcome="invoked",
        event_name="TOP_COMMAND_USED",
    )
    if not await _slash_permission_allowed(interaction, "top"):
        return

    # Keep the interaction public so Discord shows who invoked /top in the channel.
    await interaction.response.defer(ephemeral=False, thinking=True)
    success, _history_entry = await _publish_top_with_history(
        interaction.channel,
        source="manual",
    )
    if success:
        await interaction.followup.send("✅ Топ опубликован в этом канале.", ephemeral=False)
    else:
        await interaction.followup.send(
            "❌ Не удалось получить топ. Проверь SPIDE_COOKIE и логи бота.",
            ephemeral=False,
        )


@app_commands.command(name="розыгрыш", description="Создать розыгрыш на этом сервере")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_giveaway(interaction: discord.Interaction):
    if not await _require_administrator(interaction):
        return
    if interaction.channel is None or interaction.guild is None:
        await interaction.response.send_message("❌ Команду можно использовать только на сервере.", ephemeral=True)
        return
    await interaction.response.send_modal(
        GiveawayCreateModal(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            creator_id=interaction.user.id,
            creator_name=getattr(interaction.user, "display_name", interaction.user.name),
        )
    )




_giveaway_commands_synced_guilds = set()


async def _sync_giveaway_command_for_guild(guild):
    """Register only /розыгрыш as a guild command for immediate visibility."""
    guild_ref = discord.Object(id=int(guild.id))
    command = app_commands.Command(
        name=slash_giveaway.name,
        description=slash_giveaway.description,
        callback=slash_giveaway.callback,
    )
    bot.tree.add_command(command, guild=guild_ref, override=True)
    synced = await bot.tree.sync(guild=guild_ref)
    _giveaway_commands_synced_guilds.add(int(guild.id))
    logging.info(
        "Синхронизирована серверная команда /розыгрыш: guild=%s | guild_id=%s | commands=%s",
        getattr(guild, "name", "unknown"),
        guild.id,
        len(synced),
    )
    return synced


async def _sync_giveaway_commands_for_all_guilds():
    """Sync /розыгрыш separately to each joined server, bypassing global rollout delay."""
    for guild in bot.guilds:
        if int(guild.id) in _giveaway_commands_synced_guilds:
            continue
        try:
            await _sync_giveaway_command_for_guild(guild)
        except Exception:
            logging.exception(
                "Не удалось синхронизировать /розыгрыш для сервера %s (%s)",
                getattr(guild, "name", "unknown"),
                guild.id,
            )


@bot.tree.command(name="links", description="Официальные ссылки Spide")
async def slash_links(interaction: discord.Interaction):
    if not await _slash_permission_allowed(interaction, "links"):
        return
    await interaction.response.send_message(embed=_build_links_embed(interaction.user))


@bot.tree.command(name="checktg", description="Проверить новые публикации Telegram")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_checktg(interaction: discord.Interaction):
    if not await _require_administrator(interaction):
        return
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
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_setup_roles(interaction: discord.Interaction):
    if not await _require_administrator(interaction):
        return
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
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_commands(interaction: discord.Interaction):
    if not await _require_administrator(interaction):
        return
    if not await _slash_permission_allowed(interaction, "commands"):
        return
    await interaction.response.send_message(embed=_build_commands_embed(interaction.user))


@bot.tree.command(name="поиск", description="Найти трек, альбом или исполнителя")
@app_commands.describe(запрос="Название трека или исполнителя")
async def slash_search_ru(interaction: discord.Interaction, запрос: str):
    await _handle_slash_search(interaction, запрос)


@bot.tree.command(name="топ", description="Показать топ слушателей Spide")
async def slash_top_ru(interaction: discord.Interaction):
    await slash_top.callback(interaction)


@bot.tree.command(name="ссылки", description="Официальные ссылки Spide")
async def slash_links_ru(interaction: discord.Interaction):
    await slash_links.callback(interaction)


@bot.tree.command(name="проверить_тг", description="Проверить новые публикации Telegram")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_checktg_ru(interaction: discord.Interaction):
    await slash_checktg.callback(interaction)


@bot.tree.command(name="правила", description="Показать правила сообщества Spide")
async def slash_rules_ru(interaction: discord.Interaction):
    await slash_rules.callback(interaction)


@bot.tree.command(name="роли", description="Опубликовать кнопки выбора платформы")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_setup_roles_ru(interaction: discord.Interaction):
    await slash_setup_roles.callback(interaction)


@bot.tree.command(name="команды", description="Показать список команд SpideBot")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_commands_ru(interaction: discord.Interaction):
    await slash_commands.callback(interaction)


def _log_slash_command_usage(
    interaction: discord.Interaction,
    command=None,
    outcome: str = "success",
    event_name: str = "SLASH_COMMAND",
):
    """Write one structured log entry with the command and invocation context."""
    command_name = getattr(command, "name", None) or getattr(getattr(interaction, "command", None), "name", "unknown")
    user = interaction.user
    guild = interaction.guild
    channel = interaction.channel

    logging.info(
        "%s | outcome=%s | command=/%s | user=%s | user_id=%s "
        "| guild=%s | guild_id=%s | channel=%s | channel_id=%s",
        event_name,
        outcome,
        command_name,
        getattr(user, "display_name", None) or getattr(user, "name", "unknown"),
        getattr(user, "id", "unknown"),
        getattr(guild, "name", "DM") if guild is not None else "DM",
        getattr(guild, "id", None),
        getattr(channel, "name", "unknown"),
        getattr(channel, "id", None),
    )


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command: app_commands.Command):
    # /top and /топ already emit a dedicated TOP_COMMAND_USED entry at invocation.
    if getattr(command, "name", None) in {"top", "топ"}:
        return
    _log_slash_command_usage(interaction, command=command, outcome="success")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    command = getattr(interaction, "command", None)
    _log_slash_command_usage(interaction, command=command, outcome="error")
    logging.error(
        "Ошибка slash-команды /%s: %s",
        getattr(command, "name", "unknown"),
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
