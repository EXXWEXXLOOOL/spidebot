import os
import re
import sqlite3
from urllib.parse import urlsplit

from flask import Flask

from dashboard.admin_panel import register_admin_panel


class FakeBot:
    def __init__(self, guilds=None, ready=False):
        self.guilds = list(guilds or [])
        self.latency = 0.025
        self._ready = ready

    def is_ready(self):
        return self._ready

    def get_guild(self, guild_id):
        return None

    def get_channel(self, channel_id):
        return None


def build_client(tmp_path, bot=None, get_daily_top_dashboard_data=None):
    os.environ["ADMIN_USERNAME"] = "test-admin"
    os.environ["ADMIN_PASSWORD"] = "a-long-test-password"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["MODERATION_ENABLED"] = "0"
    app = Flask(__name__, template_folder="../templates")
    config = {"tg_channels": [], "last_ids": {}, "permissions": {}, "SPIDE_COOKIE": ""}
    register_admin_panel(
        app, bot or FakeBot(),
        get_config=lambda: config,
        save_config=lambda value: config.update(value),
        get_logs=lambda: [],
        actions={},
        get_top_data=lambda: {"rows": [], "total": 0, "day": "today"},
        data_dir=str(tmp_path),
        get_daily_top_dashboard_data=get_daily_top_dashboard_data,
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
        "/admin/access",
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
    assert config["automation"]["daily_top_hour"] == 2
    assert config["automation"]["daily_top_minute"] == 30
    assert config["automation"]["daily_top_timezone"] == "UTC"
    assert config["automation"]["daily_top_channel_id"] == 1566895795654107217


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

    slash_names = {command.name for command in namespace["bot"].tree.get_commands()}
    assert {
        "search", "поиск",
        "top", "топ",
        "links", "ссылки",
        "checktg", "проверить_тг",
        "rules", "правила",
        "setup_roles", "роли",
        "commands", "команды",
    } <= slash_names
    assert namespace["bot"].get_command("поиск") is None
    assert namespace["bot"].get_command("search") is None

    tree_commands = {command.name: command for command in namespace["bot"].tree.get_commands()}
    admin_only_commands = (
        "checktg", "проверить_тг",
        "setup_roles", "роли",
        "commands", "команды",
    )
    for command_name in admin_only_commands:
        command = tree_commands[command_name]
        assert command.guild_only is True
        assert command.default_permissions is not None
        assert command.default_permissions.administrator is True

    giveaway_command = namespace["slash_giveaway"]
    assert giveaway_command.name == "розыгрыш"
    assert giveaway_command.guild_only is True
    assert giveaway_command.default_permissions is not None
    assert giveaway_command.default_permissions.administrator is True
    assert "розыгрыш" not in slash_names

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

def test_owner_can_create_admin_and_viewer_accounts(tmp_path):
    import sqlite3

    client, _ = build_client(tmp_path)
    token = login(client)
    response = client.post("/admin/access/add", data={
        "_csrf": token,
        "username": "new_admin",
        "password": "a-very-long-password-123",
        "role": "admin",
    })
    assert response.status_code == 302

    connection = sqlite3.connect(tmp_path / "admin_users.sqlite3")
    try:
        row = connection.execute(
            "SELECT username, password_hash, role, active FROM admin_users WHERE username = ?",
            ("new_admin",),
        ).fetchone()
    finally:
        connection.close()

    assert row is not None
    assert row[0] == "new_admin"
    assert row[1] != "a-very-long-password-123"
    assert row[2] == "admin"
    assert row[3] == 1


def test_viewer_can_read_but_cannot_change_settings_or_manage_accounts(tmp_path):
    owner_client, _ = build_client(tmp_path)
    owner_token = login(owner_client)
    owner_client.post("/admin/access/add", data={
        "_csrf": owner_token,
        "username": "read_only",
        "password": "another-long-password-456",
        "role": "viewer",
    })

    viewer_client, _ = build_client(tmp_path)
    page = viewer_client.get("/admin/login")
    csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
    login_response = viewer_client.post("/admin/login", data={
        "_csrf": csrf,
        "username": "read_only",
        "password": "another-long-password-456",
    })
    assert login_response.status_code == 302
    assert viewer_client.get("/admin/").status_code == 200
    assert viewer_client.get("/admin/access").status_code == 403

    view_page = viewer_client.get("/admin/")
    viewer_csrf = re.search(rb'name="_csrf" value="([^"]+)"', view_page.data).group(1).decode()
    blocked = viewer_client.post("/admin/telegram/add", data={
        "_csrf": viewer_csrf,
        "channel_name": "somechannel",
    })
    assert blocked.status_code == 403


def test_owner_can_disable_account_and_existing_session_is_revoked(tmp_path):
    owner_client, _ = build_client(tmp_path)
    owner_token = login(owner_client)
    owner_client.post("/admin/access/add", data={
        "_csrf": owner_token,
        "username": "temp_admin",
        "password": "temporary-long-password-789",
        "role": "admin",
    })

    temp_client, _ = build_client(tmp_path)
    login_page = temp_client.get("/admin/login")
    csrf = re.search(rb'name="_csrf" value="([^"]+)"', login_page.data).group(1).decode()
    logged_in = temp_client.post("/admin/login", data={
        "_csrf": csrf,
        "username": "temp_admin",
        "password": "temporary-long-password-789",
    })
    assert logged_in.status_code == 302
    assert temp_client.get("/admin/").status_code == 200

    access_page = owner_client.get("/admin/access")
    owner_token = re.search(rb'name="_csrf" value="([^"]+)"', access_page.data).group(1).decode()
    connection = sqlite3.connect(tmp_path / "admin_users.sqlite3")
    try:
        temp_id = connection.execute(
            "SELECT id FROM admin_users WHERE username = ?",
            ("temp_admin",),
        ).fetchone()[0]
    finally:
        connection.close()

    disabled = owner_client.post(f"/admin/access/{temp_id}/toggle", data={"_csrf": owner_token})
    assert disabled.status_code == 302
    assert temp_client.get("/admin/").status_code == 302


def test_non_owner_cannot_create_admin_accounts(tmp_path):
    owner_client, _ = build_client(tmp_path)
    owner_token = login(owner_client)
    owner_client.post("/admin/access/add", data={
        "_csrf": owner_token,
        "username": "ordinary_admin",
        "password": "ordinary-admin-password-123",
        "role": "admin",
    })

    admin_client, _ = build_client(tmp_path)
    login_page = admin_client.get("/admin/login")
    csrf = re.search(rb'name="_csrf" value="([^"]+)"', login_page.data).group(1).decode()
    admin_client.post("/admin/login", data={
        "_csrf": csrf,
        "username": "ordinary_admin",
        "password": "ordinary-admin-password-123",
    })
    page = admin_client.get("/admin/")
    admin_csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
    response = admin_client.post("/admin/access/add", data={
        "_csrf": admin_csrf,
        "username": "escalated_user",
        "password": "escalated-user-password-123",
        "role": "admin",
    })
    assert response.status_code == 403

def test_role_selector_validation_and_command_help(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-role-selector-test")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_role_selector_test",
    )

    class FakeRole:
        def __init__(self, name, position, managed=False, default=False):
            self.name = name
            self.position = position
            self.managed = managed
            self._default = default

        def is_default(self):
            return self._default

        def __ge__(self, other):
            return self.position >= other.position

    def fake_guild(manage_roles=True, bot_position=10):
        bot_role = FakeRole("SpideBot", bot_position)
        permissions = SimpleNamespace(manage_roles=manage_roles)
        bot_member = SimpleNamespace(guild_permissions=permissions, top_role=bot_role)
        return SimpleNamespace(me=bot_member, get_member=lambda _user_id: bot_member)

    assignable_role = FakeRole("Android", 2)
    assert namespace["_role_assignment_error"](fake_guild(), assignable_role) is None
    assert "Управлять ролями" in namespace["_role_assignment_error"](
        fake_guild(manage_roles=False), assignable_role
    )
    assert "выше или на одном уровне" in namespace["_role_assignment_error"](
        fake_guild(bot_position=2), assignable_role
    )
    assert "не найдена" in namespace["_role_assignment_error"](fake_guild(), None)

    view = namespace["RoleSelectView"]()
    assert view.timeout is None
    assert {item.custom_id for item in view.children} == {
        "role_android", "role_web", "role_desktop"
    }

    author = SimpleNamespace(
        display_name="Tester",
        display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
    )
    help_embed = namespace["_build_commands_embed"](author)
    help_text = "\\n".join(field.value for field in help_embed.fields)
    assert "/search" in help_text and "/поиск" in help_text
    assert "/setup_roles" not in help_text and "/роли" not in help_text
    assert "Выбор роли" not in help_text
    assert "!commands" not in help_text
    assert "!checktg" not in help_text
    assert "/checktg" not in help_text

def test_legacy_daily_top_default_migrates_to_0322_utc(tmp_path, monkeypatch):
    import json
    import runpy
    from pathlib import Path

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "automation": {
            "telegram_enabled": True,
            "telegram_interval_minutes": 10,
            "daily_top_enabled": True,
            "daily_top_hour_utc": 0,
            "daily_top_minute_utc": 22,
        }
    }), encoding="utf-8")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-top-schedule-migration")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_schedule_migration_test",
    )

    assert namespace["app_config"]["config_version"] == 2
    assert namespace["app_config"]["automation"]["daily_top_hour_utc"] == 3
    assert namespace["app_config"]["automation"]["daily_top_minute_utc"] == 22
    assert namespace["app_config"]["automation"]["daily_top_hour"] == 3
    assert namespace["app_config"]["automation"]["daily_top_minute"] == 22
    assert namespace["app_config"]["automation"]["daily_top_timezone"] == "UTC"
    assert namespace["app_config"]["automation"]["daily_top_channel_id"] == 1566895795654107217
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["automation"]["daily_top_hour_utc"] == 3
    assert persisted["automation"]["daily_top_hour"] == 3


def test_custom_daily_top_time_is_preserved_during_migration(tmp_path, monkeypatch):
    import json
    import runpy
    from pathlib import Path

    (tmp_path / "config.json").write_text(json.dumps({
        "automation": {
            "telegram_enabled": True,
            "telegram_interval_minutes": 10,
            "daily_top_enabled": True,
            "daily_top_hour_utc": 5,
            "daily_top_minute_utc": 45,
        }
    }), encoding="utf-8")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-custom-schedule")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_custom_schedule_test",
    )
    assert namespace["app_config"]["automation"]["daily_top_hour_utc"] == 5
    assert namespace["app_config"]["automation"]["daily_top_minute_utc"] == 45
    assert namespace["app_config"]["automation"]["daily_top_hour"] == 5
    assert namespace["app_config"]["automation"]["daily_top_minute"] == 45
    assert namespace["app_config"]["automation"]["daily_top_timezone"] == "UTC"


def test_daily_top_uses_only_configured_target_channel(tmp_path, monkeypatch):
    import asyncio
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-daily-top-channel")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_daily_top_channel_test",
    )

    target_channel = SimpleNamespace(id=1566895795654107217, send=lambda *args, **kwargs: None)
    requested_ids = []
    published_to = []

    monkeypatch.setattr(
        namespace["bot"],
        "get_channel",
        lambda channel_id: requested_ids.append(channel_id) or target_channel,
    )

    async def fake_force_top(channel):
        published_to.append(channel)
        return True

    daily_top_globals = namespace["_publish_daily_top"].__globals__
    monkeypatch.setitem(daily_top_globals, "force_top", fake_force_top)
    assert asyncio.run(namespace["_publish_daily_top"]()) is True
    assert requested_ids == [1566895795654107217]
    assert published_to == [target_channel]


def test_slash_top_publishes_to_the_invocation_channel(tmp_path, monkeypatch):
    import asyncio
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-invocation-channel")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_invocation_top_test",
    )

    invocation_channel = SimpleNamespace(id=987654321)
    published_to = []

    class FakeResponse:
        def __init__(self):
            self.defer_kwargs = None

        async def defer(self, **kwargs):
            self.deferred = True
            self.defer_kwargs = kwargs

    class FakeFollowup:
        def __init__(self):
            self.messages = []

        async def send(self, *args, **kwargs):
            self.messages.append((args, kwargs))
            return None

    fake_response = FakeResponse()
    fake_followup = FakeFollowup()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123),
        guild=object(),
        channel=invocation_channel,
        command=SimpleNamespace(name="top"),
        response=fake_response,
        followup=fake_followup,
    )

    async def allow_top(_interaction, _command_key):
        return True

    async def fake_publish(channel, source="scheduled"):
        published_to.append((channel, source))
        return True, {"status": "success"}

    slash_top_globals = namespace["slash_top"].callback.__globals__
    top_log_entries = []
    monkeypatch.setitem(
        slash_top_globals,
        "_log_slash_command_usage",
        lambda *args, **kwargs: top_log_entries.append(kwargs),
    )
    monkeypatch.setitem(slash_top_globals, "_slash_permission_allowed", allow_top)
    monkeypatch.setitem(slash_top_globals, "_publish_top_with_history", fake_publish)

    asyncio.run(namespace["slash_top"].callback(interaction))
    assert published_to == [(invocation_channel, "manual")]
    assert fake_response.defer_kwargs == {"ephemeral": False, "thinking": True}
    assert fake_followup.messages[0][1]["ephemeral"] is False
    assert top_log_entries[0]["event_name"] == "TOP_COMMAND_USED"
    assert top_log_entries[0]["outcome"] == "invoked"

def test_admin_only_runtime_guard_rejects_non_admin_members(tmp_path, monkeypatch):
    import asyncio
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-admin-runtime-guard")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_admin_runtime_guard_test",
    )

    class FakeResponse:
        def __init__(self):
            self.messages = []

        def is_done(self):
            return False

        async def send_message(self, message, **kwargs):
            self.messages.append((message, kwargs))

    async def check():
        denied_response = FakeResponse()
        denied = SimpleNamespace(
            guild=object(),
            user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False)),
            response=denied_response,
            followup=SimpleNamespace(send=None),
        )
        assert await namespace["_require_administrator"](denied) is False
        assert denied_response.messages
        assert denied_response.messages[0][1]["ephemeral"] is True

        allowed = SimpleNamespace(
            guild=object(),
            user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True)),
            response=FakeResponse(),
        )
        assert await namespace["_require_administrator"](allowed) is True

    asyncio.run(check())

def test_russian_top_alias_invokes_top_callback(tmp_path, monkeypatch):
    import asyncio
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-russian-top-alias")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_russian_top_alias_test",
    )

    invocation_channel = SimpleNamespace(id=123456789)
    published_to = []

    class FakeResponse:
        async def defer(self, **kwargs):
            self.deferred = True

    class FakeFollowup:
        async def send(self, *args, **kwargs):
            return None

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123),
        guild=object(),
        channel=invocation_channel,
        command=SimpleNamespace(name="топ"),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )

    async def allow_top(_interaction, _command_key):
        return True

    async def fake_publish(channel, source="scheduled"):
        published_to.append((channel, source))
        return True, {"status": "success"}

    top_globals = namespace["slash_top"].callback.__globals__
    top_log_entries = []
    monkeypatch.setitem(
        top_globals,
        "_log_slash_command_usage",
        lambda *args, **kwargs: top_log_entries.append(kwargs),
    )
    monkeypatch.setitem(top_globals, "_slash_permission_allowed", allow_top)
    monkeypatch.setitem(top_globals, "_publish_top_with_history", fake_publish)

    asyncio.run(namespace["slash_top_ru"].callback(interaction))
    assert published_to == [(invocation_channel, "manual")]
    assert top_log_entries[0]["event_name"] == "TOP_COMMAND_USED"
    assert top_log_entries[0]["outcome"] == "invoked"


def test_slash_command_usage_log_contains_command_user_guild_and_channel(tmp_path, monkeypatch, caplog):
    import logging
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-command-usage-log")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_command_usage_log_test",
    )

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123456, display_name="Test User"),
        guild=SimpleNamespace(id=234567, name="Test Server"),
        channel=SimpleNamespace(id=345678, name="music"),
        command=SimpleNamespace(name="поиск"),
    )

    with caplog.at_level(logging.INFO):
        namespace["_log_slash_command_usage"](
            interaction,
            command=interaction.command,
            outcome="success",
        )

    assert "SLASH_COMMAND" in caplog.text
    assert "command=/поиск" in caplog.text
    assert "user=Test User" in caplog.text
    assert "user_id=123456" in caplog.text
    assert "guild=Test Server" in caplog.text
    assert "guild_id=234567" in caplog.text
    assert "channel=music" in caplog.text
    assert "channel_id=345678" in caplog.text



def test_giveaway_datetime_parsing_and_human_only_participants(tmp_path, monkeypatch):
    import runpy
    from datetime import datetime, timezone
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-giveaway-test")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_giveaway_test",
    )

    parsed_utc = namespace["_parse_giveaway_draw_at"]("2026-11-10 20:00 UTC")
    parsed_offset = namespace["_parse_giveaway_draw_at"]("2026-11-10T22:00:00+02:00")
    parsed_moscow = namespace["_parse_giveaway_draw_at"]("2026-11-10 23:10", "Europe/Moscow")
    parsed_berlin = namespace["_parse_giveaway_draw_at"]("2026-11-10 23:10", "Europe/Berlin")
    assert parsed_utc == datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
    assert parsed_offset == parsed_utc
    assert parsed_moscow == datetime(2026, 11, 10, 20, 10, tzinfo=timezone.utc)
    assert parsed_berlin == datetime(2026, 11, 10, 22, 10, tzinfo=timezone.utc)
    assert namespace["_parse_giveaway_draw_at"]("2026-11-10 23:10", "Not/A_Timezone") is None
    assert namespace["_parse_giveaway_draw_at"]("not-a-date") is None
    # Giveaway form is wall-clock time in the selected zone. An old-style UTC suffix
    # must be rejected rather than silently overriding the selected zone.
    local_moscow = namespace["_parse_giveaway_local_draw_at"](
        "2026-11-10 23:19", "Europe/Moscow"
    )
    local_berlin = namespace["_parse_giveaway_local_draw_at"](
        "2026-11-10 23:19", "Europe/Berlin"
    )
    assert local_moscow == datetime(2026, 11, 10, 20, 19, tzinfo=timezone.utc)
    assert local_berlin == datetime(2026, 11, 10, 22, 19, tzinfo=timezone.utc)
    assert namespace["_parse_giveaway_local_draw_at"](
        "2026-11-10 23:19 UTC", "Europe/Moscow"
    ) is None
    assert namespace["_parse_giveaway_local_draw_at"](
        "2026-11-10 23:19+03:00", "Europe/Moscow"
    ) is None


    giveaway = {
        "guild_id": 1234,
        "channel_id": 2345,
        "creator_id": 3456,
        "creator_name": "Admin",
        "prize": "Nitro",
        "conditions": "Be a member",
        "draw_at": parsed_utc.isoformat(),
        "participants": [],
        "status": "active",
    }
    namespace["_save_giveaways"]({"giveaway-1": giveaway})

    status, _ = namespace["_register_giveaway_participant"](
        "giveaway-1", 1234, SimpleNamespace(id=999, bot=True)
    )
    assert status == "bot"
    assert namespace["_load_giveaways"]()["giveaway-1"]["participants"] == []

    status, _ = namespace["_register_giveaway_participant"](
        "giveaway-1", 1234, SimpleNamespace(id=111, bot=False)
    )
    assert status == "added"
    status, _ = namespace["_register_giveaway_participant"](
        "giveaway-1", 1234, SimpleNamespace(id=111, bot=False)
    )
    assert status == "duplicate"
    status, _ = namespace["_register_giveaway_participant"](
        "giveaway-1", 9999, SimpleNamespace(id=222, bot=False)
    )
    assert status == "wrong_guild"

    saved = namespace["_load_giveaways"]()["giveaway-1"]
    assert saved["participants"] == [111]

    view = namespace["GiveawayJoinView"]("giveaway-1")
    assert view.is_persistent()
    assert view.children[0].custom_id == "spide_giveaway_join:giveaway-1"
    modal = namespace["GiveawayCreateModal"](1234, 2345, 3456, "Admin")
    assert len(modal.children) == 5
    assert modal.children[0].label == "Количество призовых мест (1–10)"
    assert modal.children[1].label == "Призы — каждый с новой строки"
    assert modal.children[2].label == "Когда провести розыгрыш?"
    assert modal.children[3].label == "Часовой пояс (IANA)"
    assert modal.children[3].default == "Europe/Moscow"

    places, prizes, error = namespace["_parse_giveaway_prizes"](
        "5",
        "Nitro 1 месяц\nNitro 1 месяц\nNitro 1 месяц\nNitro 1 месяц\nNitro 1 месяц",
    )
    assert error is None
    assert places == 5
    assert prizes == ["Nitro 1 месяц"] * 5

    places, prizes, error = namespace["_parse_giveaway_prizes"](
        "2",
        "Discord Nitro\nSteam Gift Card\nRoblox Gift Card",
    )
    assert places is None and prizes is None
    assert "призов: 3" in error

    places, prizes, error = namespace["_parse_giveaway_prizes"]("11", "Prize")
    assert places is None and prizes is None
    assert "от 1 до 10" in error


def test_due_giveaway_draws_and_publishes_a_human_winner(tmp_path, monkeypatch):
    import asyncio
    import runpy
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-giveaway-draw")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_giveaway_draw_test",
    )

    draw_at = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
    namespace["_save_giveaways"]({
        "draw-test": {
            "guild_id": 1234,
            "channel_id": 2345,
            "message_id": 4567,
            "creator_name": "Admin",
            "prize": "Nitro",
            "conditions": "Be a member",
            "draw_at": draw_at.isoformat(),
            "participants": [111, 222],
            "status": "active",
        }
    })

    class FakeMessage:
        def __init__(self):
            self.edited = None

        async def edit(self, **kwargs):
            self.edited = kwargs

    class FakeChannel:
        def __init__(self):
            self.message = FakeMessage()

        async def fetch_message(self, message_id):
            assert message_id == 4567
            return self.message

    channel = FakeChannel()
    monkeypatch.setattr(namespace["bot"], "get_channel", lambda channel_id: channel)

    asyncio.run(namespace["_process_due_giveaways"](draw_at + timedelta(minutes=1)))

    saved = namespace["_load_giveaways"]()["draw-test"]
    assert saved["status"] == "drawn"
    assert saved["winner_id"] in {111, 222}
    assert saved["result_published"] is True
    assert channel.message.edited is not None
    assert channel.message.edited["view"].children[0].disabled is True
    result_text = " ".join(
        field.value for field in channel.message.edited["embed"].fields
    )
    assert f"<@{saved['winner_id']}>" in result_text


def test_giveaway_command_syncs_to_each_guild_immediately(tmp_path, monkeypatch):
    import asyncio
    import runpy
    from pathlib import Path
    from types import SimpleNamespace
    import discord

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-guild-giveaway-sync")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_guild_giveaway_sync_test",
    )

    bot = namespace["bot"]
    sync_calls = []

    async def fake_sync(*, guild=None):
        sync_calls.append(guild.id)
        return [SimpleNamespace(name="розыгрыш")]

    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    guild = SimpleNamespace(id=987654321, name="Test server")
    synced = asyncio.run(namespace["_sync_giveaway_command_for_guild"](guild))

    assert sync_calls == [987654321]
    assert synced[0].name == "розыгрыш"
    assert bot.tree.get_command(
        "розыгрыш",
        guild=discord.Object(id=987654321),
    ) is not None
    assert bot.tree.get_command("розыгрыш") is None

    registered = bot.tree.get_command(
        "розыгрыш",
        guild=discord.Object(id=987654321),
    )
    assert registered.guild_only is True
    assert registered.default_permissions.administrator is True


def test_multi_prize_giveaway_draws_unique_winners_for_each_available_place(tmp_path, monkeypatch):
    import asyncio
    import runpy
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-multi-prize-draw")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_multi_prize_draw_test",
    )

    draw_at = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
    prizes = ["Nitro 1 месяц"] * 5
    namespace["_save_giveaways"]({
        "multi-draw": {
            "guild_id": 1234,
            "channel_id": 2345,
            "message_id": 5678,
            "creator_name": "Admin",
            "prize": prizes[0],
            "prizes": prizes,
            "places_count": 5,
            "conditions": "Be a member",
            "draw_at": draw_at.isoformat(),
            "participants": [111, 222, 333],
            "status": "active",
        }
    })

    class FakeMessage:
        def __init__(self):
            self.edited = None

        async def edit(self, **kwargs):
            self.edited = kwargs

    class FakeChannel:
        def __init__(self):
            self.message = FakeMessage()

        async def fetch_message(self, message_id):
            assert message_id == 5678
            return self.message

    channel = FakeChannel()
    monkeypatch.setattr(namespace["bot"], "get_channel", lambda channel_id: channel)

    asyncio.run(namespace["_process_due_giveaways"](draw_at + timedelta(minutes=1)))

    saved = namespace["_load_giveaways"]()["multi-draw"]
    winners = saved["winner_ids"]
    assert saved["status"] == "drawn"
    assert len(winners) == 3
    assert len(set(winners)) == 3
    assert set(winners).issubset({111, 222, 333})
    assert saved["winner_id"] == winners[0]
    assert saved["result_published"] is True

    result_fields = [
        field for field in channel.message.edited["embed"].fields
        if "Результаты" in field.name
    ]
    result_text = "\n".join(field.value for field in result_fields)
    assert result_text.count("победителя нет") == 2
    for winner in winners:
        assert f"<@{winner}>" in result_text
    assert channel.message.edited["view"].children[0].disabled is True


def test_automation_settings_save_local_time_timezone_and_selected_channel(tmp_path):
    from types import SimpleNamespace

    class FakeChannel:
        id = 987654321
        name = "music-top"

        def send(self, *args, **kwargs):
            return None

        def permissions_for(self, member):
            return SimpleNamespace(send_messages=True, embed_links=True)

    class FakeGuild:
        id = 123456
        name = "Spide Community"
        me = object()
        members = []

        def __init__(self):
            self.text_channels = [FakeChannel()]

    bot = FakeBot(guilds=[FakeGuild()], ready=True)
    client, config = build_client(tmp_path, bot=bot)
    token = login(client)

    response = client.post("/admin/automation", data={
        "_csrf": token,
        "telegram_enabled": "on",
        "telegram_interval_minutes": "15",
        "daily_top_enabled": "on",
        "daily_top_time": "23:10",
        "daily_top_timezone": "Europe/Moscow",
        "daily_top_channel_id": "987654321",
    })
    assert response.status_code == 302
    settings = config["automation"]
    assert settings["daily_top_hour"] == 23
    assert settings["daily_top_minute"] == 10
    assert settings["daily_top_timezone"] == "Europe/Moscow"
    assert settings["daily_top_channel_id"] == 987654321

    page = client.get("/admin/automation")
    assert page.status_code == 200
    assert b"Europe/Moscow" in page.data
    assert b"music-top" in page.data


def test_automation_rejects_a_channel_the_bot_cannot_post_to(tmp_path):
    from types import SimpleNamespace

    class FakeChannel:
        id = 987654321
        name = "read-only"

        def send(self, *args, **kwargs):
            return None

        def permissions_for(self, member):
            return SimpleNamespace(send_messages=False, embed_links=False)

    class FakeGuild:
        id = 123456
        name = "Spide Community"
        me = object()
        members = []
        text_channels = [FakeChannel()]

    bot = FakeBot(guilds=[FakeGuild()], ready=True)
    client, config = build_client(tmp_path, bot=bot)
    token = login(client)

    response = client.post("/admin/automation", data={
        "_csrf": token,
        "telegram_enabled": "on",
        "telegram_interval_minutes": "15",
        "daily_top_enabled": "on",
        "daily_top_time": "23:10",
        "daily_top_timezone": "UTC",
        "daily_top_channel_id": "987654321",
    })
    assert response.status_code == 302
    assert config.get("automation", {}).get("daily_top_channel_id") is None


def test_daily_top_publishes_to_channel_selected_in_automation_settings(tmp_path, monkeypatch):
    import asyncio
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-selected-daily-top")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_selected_daily_top_test",
    )
    selected_channel = SimpleNamespace(id=987654321, send=lambda *args, **kwargs: None)
    requested_ids = []
    published_to = []

    namespace["app_config"]["automation"]["daily_top_channel_id"] = 987654321
    monkeypatch.setattr(
        namespace["bot"],
        "get_channel",
        lambda channel_id: requested_ids.append(channel_id) or selected_channel,
    )

    async def fake_publish(channel):
        published_to.append(channel)
        return True

    monkeypatch.setitem(namespace["_publish_daily_top"].__globals__, "_publish_top_to_channel", fake_publish)
    assert asyncio.run(namespace["_publish_daily_top"]()) is True
    assert requested_ids == [987654321]
    assert published_to == [selected_channel]


def test_daily_top_schedule_uses_selected_timezone_and_runs_once_per_local_day(tmp_path, monkeypatch):
    import runpy
    from datetime import datetime, timezone
    from pathlib import Path

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-timezone-aware-top-schedule")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_timezone_top_schedule_test",
    )

    now = datetime(2026, 11, 4, 20, 22, tzinfo=timezone.utc)
    settings = {
        "daily_top_hour": 23,
        "daily_top_minute": 22,
        "daily_top_timezone": "Europe/Moscow",
    }
    due, local_date, timezone_name = namespace["_daily_top_schedule_due"](
        now, settings, last_run_date=None
    )
    assert due is True
    assert local_date == "2026-11-04"
    assert timezone_name == "Europe/Moscow"

    due_again, _, _ = namespace["_daily_top_schedule_due"](
        now, settings, last_run_date=local_date
    )
    assert due_again is False

    berlin_settings = {
        "daily_top_hour": 21,
        "daily_top_minute": 22,
        "daily_top_timezone": "Europe/Berlin",
    }
    berlin_due, _, berlin_timezone = namespace["_daily_top_schedule_due"](
        now, berlin_settings, last_run_date=None
    )
    assert berlin_due is True
    assert berlin_timezone == "Europe/Berlin"


def test_daily_top_history_persists_success_and_failure_results(tmp_path, monkeypatch):
    import asyncio
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-daily-top-history")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_daily_top_history_test",
    )

    channel = SimpleNamespace(
        id=987654321,
        name="music-top",
        guild=SimpleNamespace(name="Spide Community"),
        send=lambda *args, **kwargs: None,
    )
    publish_globals = namespace["_publish_top_with_history"].__globals__
    async def successful_publish(target):
        assert target is channel
        return True

    monkeypatch.setitem(publish_globals, "_publish_top_to_channel", successful_publish)
    success, success_entry = asyncio.run(
        namespace["_publish_top_with_history"](channel, source="scheduled")
    )
    assert success is True
    assert success_entry["status"] == "success"
    assert success_entry["source"] == "scheduled"
    assert success_entry["channel_id"] == 987654321
    assert success_entry["channel_name"] == "Spide Community / #music-top"

    async def failed_publish(target):
        return False

    monkeypatch.setitem(publish_globals, "_publish_top_to_channel", failed_publish)
    failed, failed_entry = asyncio.run(
        namespace["_publish_top_with_history"](channel, source="manual")
    )
    assert failed is False
    assert failed_entry["status"] == "failed"
    assert failed_entry["source"] == "manual"
    assert failed_entry["error"]

    saved_history = namespace["_load_daily_top_history"]()
    assert [item["status"] for item in saved_history] == ["success", "failed"]
    assert [item["source"] for item in saved_history] == ["scheduled", "manual"]


def test_daily_top_dashboard_data_calculates_next_run_and_latest_history(tmp_path, monkeypatch):
    import runpy
    from datetime import datetime, timezone
    from pathlib import Path

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-daily-top-dashboard")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    project_root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(project_root / "bot-searchSpide.py"),
        run_name="spidebot_daily_top_dashboard_test",
    )
    settings = {
        "daily_top_enabled": True,
        "daily_top_hour": 23,
        "daily_top_minute": 10,
        "daily_top_timezone": "Europe/Moscow",
    }
    next_run, timezone_name = namespace["_get_next_daily_top_run"](
        datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc),
        settings,
    )
    assert timezone_name == "Europe/Moscow"
    assert next_run.strftime("%Y-%m-%d %H:%M") == "2026-11-04 23:10"

    namespace["app_config"]["automation"].update(settings)
    namespace["_record_daily_top_publication"](
        123,
        "Server / #top",
        "success",
        source="scheduled",
    )
    dashboard_data = namespace["_get_daily_top_dashboard_data"]()
    assert dashboard_data["enabled"] is True
    assert dashboard_data["timezone"] == "Europe/Moscow"
    assert dashboard_data["last"]["channel_name"] == "Server / #top"
    assert dashboard_data["last"]["status"] == "success"
    assert len(dashboard_data["history"]) == 1


def test_automation_page_displays_daily_top_status_and_history(tmp_path):
    dashboard_data = {
        "enabled": True,
        "timezone": "Europe/Moscow",
        "next_run": "04.11.2026 23:10",
        "next_run_iso": "2026-11-04T23:10:00+03:00",
        "last": {
            "timestamp": "2026-11-04T20:10:00+00:00",
            "channel_id": 123456,
            "channel_name": "Spide / #top",
            "status": "failed",
            "source": "scheduled",
            "error": "Тестовая ошибка отправки",
        },
        "history": [{
            "timestamp": "2026-11-04T20:10:00+00:00",
            "channel_id": 123456,
            "channel_name": "Spide / #top",
            "status": "failed",
            "source": "scheduled",
            "error": "Тестовая ошибка отправки",
        }],
    }
    client, _ = build_client(
        tmp_path,
        get_daily_top_dashboard_data=lambda: dashboard_data,
    )
    token = login(client)
    response = client.get("/admin/automation")
    assert response.status_code == 200
    assert "Статус автопубликации" in response.get_data(as_text=True)
    assert "Следующий топ" in response.get_data(as_text=True)
    assert "История публикаций" in response.get_data(as_text=True)
    assert "Spide / #top" in response.get_data(as_text=True)
    assert "Тестовая ошибка отправки" in response.get_data(as_text=True)
