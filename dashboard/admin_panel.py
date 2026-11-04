"""Authenticated Flask admin panel for SpideBot.

The panel intentionally exposes read-only user/server inspection. Destructive
Discord moderation is not implemented until explicit permission rules exist.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from werkzeug.security import check_password_hash, generate_password_hash

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template,
    request, send_file, session, url_for
)
from werkzeug.utils import secure_filename

log = logging.getLogger("spide.admin")
COMMANDS = ("search", "top", "links", "checktg", "rules", "setup_roles", "commands")


def _ensure_secret(data_dir: Path) -> str:
    env_secret = os.getenv("SECRET_KEY")
    if env_secret:
        return env_secret
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / "admin_session.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
    return key_file.read_text(encoding="utf-8").strip()


def register_admin_panel(app, bot, get_config, save_config, get_logs,
                         actions, get_top_data, data_dir=".", url_prefix="/admin", moderation_action=None):
    data_path = Path(data_dir).resolve()
    backup_dir = data_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    data_path.mkdir(parents=True, exist_ok=True)

    # Admin accounts live in a separate SQLite database. Passwords are stored
    # as Werkzeug password hashes, never as plaintext.
    admin_db_path = data_path / "admin_users.sqlite3"

    def connect_admin_db():
        connection = sqlite3.connect(admin_db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize_admin_db():
        connection = connect_admin_db()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('owner', 'admin', 'viewer')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    last_login_at TEXT
                )
                """
            )
            count = connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
            # Bootstrap the first owner from the existing environment variables.
            # This keeps current deployments working without a manual migration.
            if count == 0:
                initial_username = os.getenv("ADMIN_USERNAME", "").strip()
                initial_password = os.getenv("ADMIN_PASSWORD", "")
                if initial_username and initial_password:
                    connection.execute(
                        """
                        INSERT INTO admin_users
                            (username, password_hash, role, active, created_at)
                        VALUES (?, ?, 'owner', 1, ?)
                        """,
                        (
                            initial_username,
                            generate_password_hash(initial_password),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    log.info("Создана первая учётная запись владельца админ-панели: %s", initial_username)
                else:
                    log.error(
                        "Нет учётных записей админ-панели. Задайте ADMIN_USERNAME и ADMIN_PASSWORD "
                        "для создания первого владельца."
                    )
            connection.commit()
        finally:
            connection.close()

    def get_admin_by_username(username):
        connection = connect_admin_db()
        try:
            row = connection.execute(
                "SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def get_admin_by_id(user_id):
        connection = connect_admin_db()
        try:
            row = connection.execute(
                "SELECT * FROM admin_users WHERE id = ?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def list_admin_accounts():
        connection = connect_admin_db()
        try:
            rows = connection.execute(
                """
                SELECT id, username, role, active, created_at, last_login_at
                FROM admin_users ORDER BY
                    CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                    username COLLATE NOCASE
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def create_admin_account(username, password, role):
        connection = connect_admin_db()
        try:
            connection.execute(
                """
                INSERT INTO admin_users (username, password_hash, role, active, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (
                    username,
                    generate_password_hash(password),
                    role,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    initialize_admin_db()

    app.secret_key = _ensure_secret(data_path)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1").lower() in ("1", "true", "yes"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
    )

    bp = Blueprint("admin", __name__)

    def csrf_value():
        token = session.get("_csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf"] = token
        return token

    def require_csrf():
        supplied = request.form.get("_csrf", "")
        expected = session.get("_csrf", "")
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            abort(400, description="CSRF token is invalid or expired")

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user_id = session.get("admin_user_id")
            if not session.get("admin_authenticated") or not user_id:
                session.clear()
                return redirect(url_for("admin.login", next=request.path))

            account = get_admin_by_id(user_id)
            if not account or not account["active"]:
                session.clear()
                return redirect(url_for("admin.login", next=request.path))

            # Refresh identity and role from SQLite on every request so that
            # deactivated accounts lose access immediately.
            session["admin_username"] = account["username"]
            session["admin_role"] = account["role"]

            if request.method == "POST" and account["role"] == "viewer" and request.endpoint != "admin.logout":
                abort(403, description="Эта учётная запись имеет доступ только для просмотра.")
            return view(*args, **kwargs)
        return wrapped

    def owner_required(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if session.get("admin_role") != "owner":
                abort(403, description="Управлять учётными записями может только владелец панели.")
            return view(*args, **kwargs)
        return wrapped

    def page(name, **extra):
        cfg = get_config()
        ready = bool(bot.is_ready())
        guilds = list(bot.guilds) if ready else []
        members = sum(len(g.members) for g in guilds)
        logs = list(get_logs())
        context = dict(
            page=name, csrf=csrf_value(), status=ready,
            latency=round(bot.latency * 1000) if ready else 0,
            guilds=guilds, guild_count=len(guilds), member_count=members,
            channels=cfg.get("tg_channels", []),
            cookie_configured=bool(cfg.get("SPIDE_COOKIE")),
            permissions=cfg.get("permissions", {}),
            logs=logs[-200:],
            admin_username=session.get("admin_username", "admin"),
            admin_role=session.get("admin_role", "viewer"),
            is_owner=session.get("admin_role") == "owner",
            # The shared template evaluates chart placeholders on every page.
            level_counts={"INFO": 0, "WARNING": 0, "ERROR": 0},
        )
        context.update(extra)
        return render_template("admin.html", **context)

    def audit(message):
        log.info("ADMIN %s | %s", session.get("admin_username", "unknown"), message)

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("admin_authenticated"):
            return redirect(url_for("admin.overview"))
        error = None
        if request.method == "POST":
            require_csrf()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            account = get_admin_by_username(username) if username else None
            valid_password = False
            if account:
                try:
                    valid_password = check_password_hash(account["password_hash"], password)
                except (TypeError, ValueError):
                    valid_password = False

            if account and account["active"] and valid_password:
                session.clear()
                session.permanent = True
                session["admin_authenticated"] = True
                session["admin_user_id"] = account["id"]
                session["admin_username"] = account["username"]
                session["admin_role"] = account["role"]
                connection = connect_admin_db()
                try:
                    connection.execute(
                        "UPDATE admin_users SET last_login_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(), account["id"]),
                    )
                    connection.commit()
                finally:
                    connection.close()
                csrf_value()
                audit("успешный вход")
                target = request.args.get("next", "")
                if not target.startswith("/") or target.startswith("//"):
                    target = url_for("admin.overview")
                return redirect(target)
            else:
                error = "Неверный логин или пароль."
                log.warning("Неудачная попытка входа в админ-панель")
        return render_template(
            "login.html",
            csrf=csrf_value(),
            error=error,
            configured=bool(list_admin_accounts()),
        )

    @bp.post("/logout")
    @login_required
    def logout():
        require_csrf()
        session.clear()
        return redirect(url_for("admin.login"))

    @bp.get("/")
    @login_required
    def overview():
        cfg = get_config()
        log_items = list(get_logs())
        errors = sum("ERROR" in item for item in log_items)
        warnings = sum("WARNING" in item for item in log_items)
        return page("overview", error_count=errors, warning_count=warnings,
                    recent_events=log_items[-8:])

    @bp.get("/analytics")
    @login_required
    def analytics():
        items = list(get_logs())
        level_counts = {
            "INFO": sum("INFO" in x for x in items),
            "WARNING": sum("WARNING" in x for x in items),
            "ERROR": sum("ERROR" in x for x in items),
        }
        return page("analytics", level_counts=level_counts, sample_size=len(items))

    @bp.get("/leaderboard")
    @login_required
    def leaderboard():
        result = {"rows": [], "total": 0, "day": "—", "error": None}
        try:
            result.update(get_top_data() or {})
        except Exception as exc:
            log.exception("Не удалось загрузить рейтинг Spide")
            result["error"] = str(exc)[:240]
        rows = result.get("rows") or []
        return page("leaderboard", leaders=rows[:100], leaderboard_total=result.get("total", 0),
                    leaderboard_day=result.get("day", "—"), leaderboard_error=result.get("error"))

    @bp.get("/users")
    @login_required
    def users():
        guild_id = request.args.get("guild_id", type=int)
        query = request.args.get("q", "").strip().lower()[:80]
        selected = bot.get_guild(guild_id) if guild_id else (bot.guilds[0] if bot.guilds else None)
        found = []
        if selected:
            for member in selected.members:
                haystack = f"{member.name} {member.display_name} {member.id}".lower()
                if not query or query in haystack:
                    found.append({
                        "id": member.id, "name": member.display_name,
                        "username": member.name, "bot": member.bot,
                        "joined": member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "—",
                        "avatar": member.display_avatar.url,
                        "roles": [r.name for r in member.roles if not r.is_default()][:5],
                    })
            found.sort(key=lambda item: item["name"].lower())
        return page("users", selected_guild=selected, user_rows=found[:250], user_query=query)

    @bp.get("/servers")
    @login_required
    def servers():
        server_rows = []
        for guild in (list(bot.guilds) if bot.is_ready() else []):
            server_rows.append({
                "id": guild.id, "name": guild.name, "members": guild.member_count or len(guild.members),
                "channels": len(guild.channels), "roles": len(guild.roles),
                "owner": str(guild.owner) if guild.owner else "Недоступен",
                "icon": guild.icon.url if guild.icon else None,
                "text_channels": [c.name for c in guild.text_channels[:8]],
            })
        return page("servers", server_rows=server_rows)

    @bp.get("/logs")
    @login_required
    def logs():
        items = list(get_logs())
        level = request.args.get("level", "ALL").upper()
        query = request.args.get("q", "").lower()[:100]
        if level in ("INFO", "WARNING", "ERROR"):
            items = [x for x in items if level in x]
        if query:
            items = [x for x in items if query in x.lower()]
        return page("logs", filtered_logs=list(reversed(items[-200:])), log_level=level, log_query=query)

    @bp.get("/telegram")
    @login_required
    def telegram():
        cfg = get_config()
        return page("telegram", last_ids=cfg.get("last_ids", {}))

    @bp.get("/security")
    @login_required
    def security():
        return page("security", secret_key_source="SECRET_KEY env" if os.getenv("SECRET_KEY") else "локальный файл DATA_DIR/admin_session.key",
                    cookie_secure=app.config["SESSION_COOKIE_SECURE"],
                    password_configured=bool(os.getenv("ADMIN_PASSWORD")))

    @bp.get("/access")
    @owner_required
    def access():
        return page("access", admin_accounts=list_admin_accounts())

    @bp.post("/access/add")
    @owner_required
    def access_add():
        require_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "admin").strip().lower()

        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            flash("Логин: 3–32 символа, латиница, цифры, точка, дефис или подчёркивание.", "error")
            return redirect(url_for("admin.access"))
        if len(password) < 12:
            flash("Пароль должен содержать не менее 12 символов.", "error")
            return redirect(url_for("admin.access"))
        if role not in ("admin", "viewer"):
            abort(400, description="Можно создать только администратора или пользователя с доступом на просмотр.")

        try:
            create_admin_account(username, password, role)
        except sqlite3.IntegrityError:
            flash("Пользователь с таким логином уже существует.", "error")
            return redirect(url_for("admin.access"))

        audit(f"создана учётная запись {username} с ролью {role}")
        flash(f"Учётная запись {username} создана.", "success")
        return redirect(url_for("admin.access"))

    @bp.post("/access/<int:user_id>/toggle")
    @owner_required
    def access_toggle(user_id):
        require_csrf()
        if user_id == session.get("admin_user_id"):
            flash("Нельзя отключить собственную учётную запись.", "error")
            return redirect(url_for("admin.access"))

        account = get_admin_by_id(user_id)
        if not account:
            abort(404)
        if account["role"] == "owner":
            flash("Нельзя отключить владельца панели.", "error")
            return redirect(url_for("admin.access"))

        new_active = 0 if account["active"] else 1
        connection = connect_admin_db()
        try:
            connection.execute("UPDATE admin_users SET active = ? WHERE id = ?", (new_active, user_id))
            connection.commit()
        finally:
            connection.close()

        audit(f"{'включён' if new_active else 'отключён'} доступ пользователя {account['username']}")
        flash(f"Доступ пользователя {account['username']} {'включён' if new_active else 'отключён'}.", "success")
        return redirect(url_for("admin.access"))

    @bp.post("/access/<int:user_id>/delete")
    @owner_required
    def access_delete(user_id):
        require_csrf()
        if user_id == session.get("admin_user_id"):
            flash("Нельзя удалить собственную учётную запись.", "error")
            return redirect(url_for("admin.access"))

        account = get_admin_by_id(user_id)
        if not account:
            abort(404)
        if account["role"] == "owner":
            flash("Учётную запись владельца нельзя удалить из панели.", "error")
            return redirect(url_for("admin.access"))

        connection = connect_admin_db()
        try:
            connection.execute("DELETE FROM admin_users WHERE id = ?", (user_id,))
            connection.commit()
        finally:
            connection.close()

        audit(f"удалена учётная запись {account['username']}")
        flash(f"Учётная запись {account['username']} удалена.", "success")
        return redirect(url_for("admin.access"))

    @bp.get("/backup")
    @login_required
    def backup():
        files = sorted(backup_dir.glob("spide-config-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return page("backup", backup_files=[{"name": p.name, "size": p.stat().st_size,
                                             "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
                                            for p in files[:30]])


    daily_top_timezone_options = [
        ("UTC", "UTC"),
        ("Europe/Moscow", "Europe/Moscow"),
        ("Europe/Berlin", "Europe/Berlin"),
        ("Europe/Amsterdam", "Europe/Amsterdam"),
        ("Europe/London", "Europe/London"),
        ("America/New_York", "America/New_York"),
        ("Asia/Tokyo", "Asia/Tokyo"),
    ]

    def list_top_channels():
        channels = []
        try:
            guilds = list(bot.guilds) if bot.is_ready() else []
        except Exception:
            guilds = []
        for guild in guilds:
            bot_member = getattr(guild, "me", None)
            for channel in getattr(guild, "text_channels", []):
                if not hasattr(channel, "send"):
                    continue
                if bot_member is not None and hasattr(channel, "permissions_for"):
                    try:
                        permissions = channel.permissions_for(bot_member)
                        if not permissions.send_messages or not permissions.embed_links:
                            continue
                    except Exception:
                        continue
                channels.append({
                    "id": str(channel.id),
                    "name": f"{guild.name} / #{channel.name}",
                })
        channels.sort(key=lambda item: item["name"].lower())
        return channels

    @bp.get("/automation")
    @login_required
    def automation():
        cfg = get_config()
        settings = cfg.get("automation", {})
        defaults = {
            "telegram_enabled": True,
            "telegram_interval_minutes": 10,
            "daily_top_enabled": True,
            "daily_top_hour": settings.get("daily_top_hour_utc", 3),
            "daily_top_minute": settings.get("daily_top_minute_utc", 22),
            "daily_top_timezone": "UTC",
            "daily_top_channel_id": 1566895795654107217,
        }
        defaults.update(settings)
        selected_channel_id = str(defaults.get("daily_top_channel_id") or 1566895795654107217)
        channels = list_top_channels()
        if not bot.is_ready() and selected_channel_id and not any(
            item["id"] == selected_channel_id for item in channels
        ):
            channels.append({
                "id": selected_channel_id,
                "name": f"Сохранённый канал • ID {selected_channel_id} (бот не подключён)",
            })
        return page(
            "automation",
            automation_settings=defaults,
            daily_top_channels=channels,
            daily_top_timezone_options=daily_top_timezone_options,
        )

    @bp.post("/automation")
    @login_required
    def save_automation():
        require_csrf()
        cfg = get_config()
        current_settings = cfg.get("automation", {})
        try:
            interval = int(request.form.get("telegram_interval_minutes", "10"))
            time_value = request.form.get("daily_top_time", "").strip()
            if time_value:
                pieces = time_value.split(":")
                if len(pieces) != 2:
                    raise ValueError("Неверный формат времени")
                hour, minute = int(pieces[0]), int(pieces[1])
            else:
                # Compatibility with existing forms and external automation.
                hour = int(request.form.get(
                    "daily_top_hour_utc",
                    current_settings.get("daily_top_hour", current_settings.get("daily_top_hour_utc", 3)),
                ))
                minute = int(request.form.get(
                    "daily_top_minute_utc",
                    current_settings.get("daily_top_minute", current_settings.get("daily_top_minute_utc", 22)),
                ))
            timezone_name = request.form.get(
                "daily_top_timezone",
                current_settings.get("daily_top_timezone", "UTC"),
            ).strip()
            ZoneInfo(timezone_name)
            if not 1 <= interval <= 1440 or not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError("Значения вне допустимого диапазона")
        except (ValueError, ZoneInfoNotFoundError):
            flash("Проверьте интервал, время и часовой пояс.", "error")
            return redirect(url_for("admin.automation"))

        selected_channel = request.form.get("daily_top_channel_id", "").strip()
        if not selected_channel:
            selected_channel = str(
                current_settings.get("daily_top_channel_id") or 1566895795654107217
            )
        if not selected_channel.isdigit() or int(selected_channel) <= 0:
            flash("Выберите текстовый канал для публикации топа.", "error")
            return redirect(url_for("admin.automation"))

        available_channel_ids = {item["id"] for item in list_top_channels()}
        bot_ready = bot.is_ready()
        if bot_ready and selected_channel not in available_channel_ids:
            flash("Выбранный канал недоступен или у бота нет прав отправлять в него Embed.", "error")
            return redirect(url_for("admin.automation"))
        if not bot_ready and selected_channel != str(
            current_settings.get("daily_top_channel_id") or 1566895795654107217
        ):
            flash("Чтобы сменить канал, сначала подключите бота к Discord.", "error")
            return redirect(url_for("admin.automation"))

        automation_settings = dict(current_settings)
        automation_settings.update({
            "telegram_enabled": request.form.get("telegram_enabled") == "on",
            "telegram_interval_minutes": interval,
            "daily_top_enabled": request.form.get("daily_top_enabled") == "on",
            "daily_top_hour": hour,
            "daily_top_minute": minute,
            "daily_top_timezone": timezone_name,
            "daily_top_channel_id": int(selected_channel),
        })
        cfg["automation"] = automation_settings
        save_config(cfg)
        audit("обновлены настройки автоматизации")
        flash("Расписание и канал публикации топа сохранены.", "success")
        return redirect(url_for("admin.automation"))

    @bp.get("/notifications")
    @login_required
    def notifications():
        cfg = get_config()
        settings = cfg.get("notifications", {})
        channels = []
        for guild in (list(bot.guilds) if bot.is_ready() else []):
            for channel in guild.text_channels:
                if hasattr(channel, "send"):
                    channels.append({"id": str(channel.id), "name": f"{guild.name} / #{channel.name}"})
        channels.sort(key=lambda item: item["name"].lower())
        return page("notifications", notification_settings={
            "enabled": settings.get("enabled", False),
            "channel_id": str(settings.get("channel_id", "")),
            "notify_startup": settings.get("notify_startup", True),
            "notify_disconnect": settings.get("notify_disconnect", True),
            "notify_errors": settings.get("notify_errors", False),
        }, notification_channels=channels)

    @bp.post("/notifications")
    @login_required
    def save_notifications():
        require_csrf()
        channel_id = request.form.get("channel_id", "").strip()
        channel = bot.get_channel(int(channel_id)) if channel_id.isdigit() else None
        if channel_id and (channel is None or not hasattr(channel, "send")):
            flash("Выберите доступный текстовый канал Discord.", "error")
            return redirect(url_for("admin.notifications"))
        cfg = get_config()
        cfg["notifications"] = {
            "enabled": request.form.get("enabled") == "on",
            "channel_id": int(channel_id) if channel_id else 0,
            "notify_startup": request.form.get("notify_startup") == "on",
            "notify_disconnect": request.form.get("notify_disconnect") == "on",
            "notify_errors": request.form.get("notify_errors") == "on",
        }
        save_config(cfg)
        audit("обновлены настройки уведомлений")
        flash("Настройки уведомлений сохранены.", "success")
        return redirect(url_for("admin.notifications"))

    @bp.post("/notifications/test")
    @login_required
    def test_notification():
        require_csrf()
        callback = actions.get("notification_test")
        if not callback:
            flash("Отправка тестового уведомления пока недоступна.", "error")
        else:
            try:
                callback()
                audit("запрошено тестовое уведомление")
                flash("Тестовое уведомление отправлено в очередь.", "success")
            except Exception:
                log.exception("Не удалось отправить тестовое уведомление")
                flash("Не удалось отправить тестовое уведомление. Проверьте логи.", "error")
        return redirect(url_for("admin.notifications"))

    @bp.get("/moderation")
    @login_required
    def moderation():
        enabled = os.getenv("MODERATION_ENABLED", "0").lower() in ("1", "true", "yes")
        return page("moderation", moderation_enabled=enabled)

    @bp.post("/moderation/action")
    @login_required
    def moderation_submit():
        require_csrf()
        if os.getenv("MODERATION_ENABLED", "0").lower() not in ("1", "true", "yes"):
            abort(403, description="Discord moderation is disabled by MODERATION_ENABLED")
        action = request.form.get("action", "").lower()
        if action not in ("timeout", "kick", "ban", "unban"):
            abort(400, description="Unsupported moderation action")
        guild_id = request.form.get("guild_id", "").strip()
        user_id = request.form.get("user_id", "").strip()
        reason = request.form.get("reason", "").strip()[:500] or "Действие через SpideBot Admin"
        try:
            int(guild_id)
            int(user_id)
        except ValueError:
            flash("Укажите корректные ID сервера и пользователя.", "error")
            return redirect(url_for("admin.moderation"))
        duration = 10
        if action == "timeout":
            try:
                duration = int(request.form.get("duration_minutes", "10"))
                if not 1 <= duration <= 40320:
                    raise ValueError
            except ValueError:
                flash("Timeout должен быть от 1 до 40320 минут (28 дней).", "error")
                return redirect(url_for("admin.moderation"))
        if moderation_action is None:
            flash("Модерация не подключена к Discord-клиенту.", "error")
            return redirect(url_for("admin.moderation"))
        try:
            moderation_action(action, int(guild_id), int(user_id), reason, duration)
            audit(f"запрошено действие {action} для Discord user {user_id} на сервере {guild_id}")
            flash("Действие передано Discord-боту. Результат смотри в логах.", "success")
        except Exception:
            log.exception("Ошибка постановки модерации в очередь")
            flash("Не удалось запустить действие. Проверьте логи и состояние бота.", "error")
        return redirect(url_for("admin.moderation"))

    @bp.post("/permissions")
    @login_required
    def update_permissions():
        require_csrf()
        cfg = get_config()
        guild_id = request.form.get("guild_id", "")
        command = request.form.get("command_name", "")
        role_ids = request.form.getlist("roles")
        guild = bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
        valid_roles = {str(role.id) for role in guild.roles if not role.is_default() and not role.managed} if guild else set()
        if not guild or command not in COMMANDS or any(role_id not in valid_roles for role_id in role_ids):
            abort(400, description="Некорректный сервер, команда или роль")
        cfg.setdefault("permissions", {}).setdefault(guild_id, {})
        if role_ids:
            cfg["permissions"][guild_id][command] = role_ids
        else:
            cfg["permissions"][guild_id].pop(command, None)
        save_config(cfg)
        audit(f"изменены права команды {command} на сервере {guild_id}")
        flash("Права сохранены.", "success")
        return redirect(url_for("admin.overview") + "#permissions")

    @bp.get("/api/roles/<int:guild_id>")
    @login_required
    def roles(guild_id):
        guild = bot.get_guild(guild_id)
        if not guild:
            return {"roles": []}
        return {"roles": [{"id": str(r.id), "name": r.name} for r in guild.roles if not r.is_default() and not r.managed]}

    @bp.post("/telegram/add")
    @login_required
    def add_channel():
        require_csrf()
        raw = request.form.get("channel_name", "").strip()
        import re
        name = re.sub(r"^(https?://)?(t\.me/)?", "", raw).replace("@", "").strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", name):
            flash("Некорректное имя Telegram-канала.", "error")
        else:
            cfg = get_config()
            if name not in cfg.setdefault("tg_channels", []):
                cfg["tg_channels"].append(name)
                cfg.setdefault("last_ids", {})[name] = 0
                save_config(cfg)
                audit(f"добавлен Telegram-канал @{name}")
                flash(f"Канал @{name} добавлен.", "success")
        return redirect(url_for("admin.telegram"))

    @bp.post("/telegram/delete/<channel_name>")
    @login_required
    def delete_channel(channel_name):
        require_csrf()
        cfg = get_config()
        if channel_name in cfg.get("tg_channels", []):
            cfg["tg_channels"].remove(channel_name)
            cfg.get("last_ids", {}).pop(channel_name, None)
            save_config(cfg)
            audit(f"удалён Telegram-канал @{channel_name}")
            flash(f"Канал @{channel_name} удалён.", "success")
        return redirect(url_for("admin.telegram"))

    @bp.post("/cookie")
    @login_required
    def update_cookie():
        require_csrf()
        value = request.form.get("cookie_value", "").strip()
        if value:
            cfg = get_config()
            cfg["SPIDE_COOKIE"] = value
            save_config(cfg)
            audit("обновлены Spide cookie (значение не записывается в лог)")
            flash("Cookie обновлены. Значение не отображается в панели.", "success")
        return redirect(url_for("admin.security"))

    @bp.post("/action/<action_name>")
    @login_required
    def run_action(action_name):
        require_csrf()
        callback = actions.get(action_name)
        if not callback:
            abort(404)
        try:
            callback()
            audit(f"запущено действие {action_name}")
            flash("Действие отправлено в очередь бота.", "success")
        except Exception:
            log.exception("Ошибка запуска административного действия %s", action_name)
            flash("Не удалось запустить действие. Проверьте логи.", "error")
        return redirect(request.referrer if request.referrer and request.referrer.startswith("/") else url_for("admin.overview"))

    @bp.post("/backup/create")
    @login_required
    def create_backup():
        require_csrf()
        cfg = get_config()
        safe_cfg = {k: v for k, v in cfg.items() if k != "SPIDE_COOKIE"}
        safe_cfg["_backup_meta"] = {"created_at": datetime.now(timezone.utc).isoformat(), "format": 1,
                                    "note": "SPIDE_COOKIE intentionally excluded"}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"spide-config-{stamp}.json"
        target.write_text(json.dumps(safe_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass
        audit(f"создана резервная копия {target.name}")
        flash(f"Резервная копия {target.name} создана. Cookie в неё не включены.", "success")
        return redirect(url_for("admin.backup"))

    @bp.get("/backup/download/<filename>")
    @login_required
    def download_backup(filename):
        safe = secure_filename(filename)
        target = backup_dir / safe
        if not safe.startswith("spide-config-") or not target.is_file():
            abort(404)
        return send_file(target, as_attachment=True, download_name=safe, mimetype="application/json")

    @bp.post("/backup/restore")
    @login_required
    def restore_backup():
        require_csrf()
        upload = request.files.get("backup_file")
        if not upload or not upload.filename or not upload.filename.lower().endswith(".json"):
            flash("Выберите JSON-файл резервной копии.", "error")
            return redirect(url_for("admin.backup"))
        try:
            data = json.loads(upload.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Ожидался JSON-объект")
            restored = {
                "tg_channels": data.get("tg_channels", []),
                "last_ids": data.get("last_ids", {}),
                "permissions": data.get("permissions", {}),
                "SPIDE_COOKIE": get_config().get("SPIDE_COOKIE", ""),
            }
            if not isinstance(restored["tg_channels"], list) or not all(isinstance(x, str) for x in restored["tg_channels"]):
                raise ValueError("Некорректный список каналов")
            if not all(isinstance(restored[k], dict) for k in ("last_ids", "permissions")):
                raise ValueError("Некорректные настройки")
            save_config(restored)
            audit("восстановлены настройки из резервной копии")
            flash("Настройки восстановлены. Текущие cookie сохранены без изменений.", "success")
        except Exception as exc:
            flash(f"Не удалось восстановить копию: {str(exc)[:180]}", "error")
        return redirect(url_for("admin.backup"))

    app.register_blueprint(bp, url_prefix=url_prefix)
    return bp
