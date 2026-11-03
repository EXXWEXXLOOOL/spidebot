"""Authenticated Flask admin panel for SpideBot.

The panel intentionally exposes read-only user/server inspection. Destructive
Discord moderation is not implemented until explicit permission rules exist.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

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
                         actions, get_top_data, data_dir=".", url_prefix="/admin"):
    data_path = Path(data_dir).resolve()
    backup_dir = data_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

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
            if not session.get("admin_authenticated"):
                return redirect(url_for("admin.login", next=request.path))
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
            admin_username=os.getenv("ADMIN_USERNAME", "admin"),
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
            expected_user = os.getenv("ADMIN_USERNAME", "")
            expected_password = os.getenv("ADMIN_PASSWORD", "")
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if not expected_user or not expected_password:
                error = "Задайте ADMIN_USERNAME и ADMIN_PASSWORD в переменных окружения."
            elif hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password):
                session.clear()
                session.permanent = True
                session["admin_authenticated"] = True
                session["admin_username"] = username
                csrf_value()
                audit("успешный вход")
                target = request.args.get("next", "")
                if not target.startswith("/") or target.startswith("//"):
                    target = url_for("admin.overview")
                return redirect(target)
            else:
                error = "Неверный логин или пароль."
                log.warning("Неудачная попытка входа в админ-панель")
        return render_template("login.html", csrf=csrf_value(), error=error,
                               configured=bool(os.getenv("ADMIN_USERNAME") and os.getenv("ADMIN_PASSWORD")))

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

    @bp.get("/backup")
    @login_required
    def backup():
        files = sorted(backup_dir.glob("spide-config-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return page("backup", backup_files=[{"name": p.name, "size": p.stat().st_size,
                                             "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
                                            for p in files[:30]])

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
