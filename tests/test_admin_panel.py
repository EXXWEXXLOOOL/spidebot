import os
import re

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


def build_client(tmp_path):
    os.environ["ADMIN_USERNAME"] = "test-admin"
    os.environ["ADMIN_PASSWORD"] = "a-long-test-password"
    os.environ["COOKIE_SECURE"] = "0"
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
    return app.test_client()


def test_admin_requires_login(tmp_path):
    client = build_client(tmp_path)
    response = client.get("/admin/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/login")


def test_login_requires_csrf_and_accepts_valid_credentials(tmp_path):
    client = build_client(tmp_path)
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
    assert accepted.headers["Location"].endswith("/admin/")


def test_authenticated_overview_renders(tmp_path):
    client = build_client(tmp_path)
    page = client.get("/admin/login")
    token = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
    client.post("/admin/login", data={
        "_csrf": token,
        "username": "test-admin",
        "password": "a-long-test-password",
    })
    response = client.get("/admin/")
    assert response.status_code == 200
    assert b"SPIDEBOT" in response.data
