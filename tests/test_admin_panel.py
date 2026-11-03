import os
import re
from urllib.parse import urlsplit

from flask import Flask

from dashboard.admin_panel import register_admin_panel


class FakeBot:
    def __init__(self):
        self.guilds = []
        self.latency = 0.025

    def is_ready(self):
        return False

    def get_guild(self, guild_id):
        return None

    def get_channel(self, channel_id):
        return None


def build_client(tmp_path):
    os.environ["ADMIN_USERNAME"] = "test-admin"
    os.environ["ADMIN_PASSWORD"] = "a-long-test-password"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MODERATION_ENABLED"] = "0"
    app = Flask(__name__, template_folder="../templates")
    config = {"tg_channels": [], "last_ids": {}, "permissions": {}, "SPIDE_COOKIE": ""}
    register_admin_panel(
        app, FakeBot(),
        get_config=lambda: config,
        save_config=lambda value: config.update(value),
        get_logs=lambda: [],
        actions={},
        get_top_data=lambda: {"rows": [], "total": 0, "day": "today"},
        data_dir=str(tmp_path),
    )
    app.config["TESTING"] = True
    return app.test_client(), config


def login(client):
    page = client.get("/admin/login")
    token = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
    response = client.post("/admin/login", data={
        "_csrf": token,
        "username": "test-admin",
        "password": "a-long-test-password",
    })
    assert response.status_code == 302
    authenticated_page = client.get("/admin/")
    refreshed = re.search(rb'name="_csrf" value="([^"]+)"', authenticated_page.data)
    assert refreshed is not None
    return refreshed.group(1).decode()


def test_admin_requires_login(tmp_path):
    client, _ = build_client(tmp_path)
    response = client.get("/admin/")
    assert response.status_code == 302
    assert urlsplit(response.headers["Location"]).path == "/admin/login"


def test_login_requires_csrf_and_accepts_valid_credentials(tmp_path):
    client, _ = build_client(tmp_path)
    page = client.get("/admin/login")
    token = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()

    rejected = client.post("/admin/login", data={
        "username": "test-admin",
        "password": "a-long-test-password",
    })
    assert rejected.status_code == 400

    accepted = client.post("/admin/login", data={
        "_csrf": token,
        "username": "test-admin",
        "password": "a-long-test-password",
    })
    assert accepted.status_code == 302
    assert urlsplit(accepted.headers["Location"]).path == "/admin/"


def test_authenticated_overview_renders(tmp_path):
    client, _ = build_client(tmp_path)
    login(client)
    response = client.get("/admin/")
    assert response.status_code == 200
    assert b"SPIDE" in response.data
    assert b"BOT" in response.data
    assert "Обзор системы".encode("utf-8") in response.data


def test_new_admin_sections_render(tmp_path):
    client, _ = build_client(tmp_path)
    login(client)
    for path in (
        "/admin/automation",
        "/admin/notifications",
        "/admin/moderation",
        "/admin/leaderboard",
    ):
        response = client.get(path)
        assert response.status_code == 200, path


def test_automation_settings_are_saved(tmp_path):
    client, config = build_client(tmp_path)
    token = login(client)
    response = client.post("/admin/automation", data={
        "_csrf": token,
        "telegram_enabled": "on",
        "telegram_interval_minutes": "15",
        "daily_top_enabled": "on",
        "daily_top_hour_utc": "2",
        "daily_top_minute_utc": "30",
    })
    assert response.status_code == 302
    assert config["automation"]["telegram_interval_minutes"] == 15
    assert config["automation"]["daily_top_hour_utc"] == 2
    assert config["automation"]["daily_top_minute_utc"] == 30


def test_moderation_is_disabled_by_default(tmp_path):
    client, _ = build_client(tmp_path)
    token = login(client)
    response = client.post("/admin/moderation/action", data={
        "_csrf": token,
        "action": "ban",
        "guild_id": "123",
        "user_id": "123456789012345678",
    })
    assert response.status_code == 403

def test_bot_module_imports_without_callback_name_errors(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-import-smoke-test")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_import_smoke_test",
    )
    assert "bot" in namespace
    assert "web_app" in namespace
    assert "admin" in namespace["web_app"].blueprints

def test_leaderboard_medals_are_only_used_for_top_three(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-leaderboard-test")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_leaderboard_test",
    )
    rows = [
        {"user": {"displayName": f"Listener {index}"}, "score": 20000 - index * 1000}
        for index in range(1, 11)
    ]
    embed = namespace["_build_listener_top_embed"]({
        "rows": rows,
        "total": 7762,
        "day": "2026-11-03",
    })

    assert len(embed.fields) == 4
    podium_text = " ".join(field.name + " " + field.value for field in embed.fields[:3])
    remaining_text = embed.fields[3].name + " " + embed.fields[3].value

    assert all(medal in podium_text for medal in ("🥇", "🥈", "🥉"))
    assert all(medal not in remaining_text for medal in ("🥇", "🥈", "🥉", "🏅", "🎖️"))
    assert "**04.**" in remaining_text
    assert "**10.**" in remaining_text
    assert "Listener 4" in remaining_text
    assert "мин" in remaining_text

