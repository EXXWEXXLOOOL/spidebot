import json
import runpy
from pathlib import Path

import pytest

from dashboard.admin_panel import _safe_return_path


@pytest.fixture
def bot_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "regression-test-secret")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "bot-searchSpide.py"))


def test_failed_save_preserves_disk_and_live_config(bot_module, tmp_path, monkeypatch):
    save = bot_module["_admin_save_config"]
    original = json.loads(json.dumps(bot_module["app_config"]))
    save(original)
    path = tmp_path / "config.json"
    before = path.read_bytes()

    def fail_replace(*args):
        raise PermissionError("simulated disk failure")

    monkeypatch.setattr(bot_module["os"], "replace", fail_replace)
    with pytest.raises(PermissionError):
        save({**original, "SPIDE_COOKIE": "replacement"})
    assert path.read_bytes() == before
    assert bot_module["app_config"] == original
    assert not (tmp_path / "config.json.tmp").exists()


@pytest.mark.parametrize("section", ["automation", "notifications", "permissions", "tg_channels"])
def test_invalid_section_does_not_discard_other_settings(bot_module, tmp_path, section):
    config = json.loads(json.dumps(bot_module["app_config"]))
    config["SPIDE_COOKIE"] = "keep-existing-cookie"
    config[section] = None
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loaded = bot_module["load_config"]()
    assert loaded["SPIDE_COOKIE"] == "keep-existing-cookie"
    assert loaded[section] == bot_module["DEFAULT_CONFIG"][section]


@pytest.mark.parametrize("target", [
    "//outside.example/path", "/\\outside.example", "https://outside.example/",
    "http://[invalid", "/\n/outside.example", "https://local.example//outside.example",
])
def test_redirect_rejects_external_or_ambiguous_targets(target):
    assert _safe_return_path(target, "https://local.example/") is None


@pytest.mark.parametrize("target", [
    "/admin/logs?q=test", "https://local.example/admin/logs?q=test",
])
def test_redirect_preserves_local_page_and_query(target):
    assert _safe_return_path(target, "https://local.example/") == "/admin/logs?q=test"