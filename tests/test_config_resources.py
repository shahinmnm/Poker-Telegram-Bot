import json

from pokerapp.config import Config, GameConstants


def test_game_constants_loads_external_resources(tmp_path):
    translations_path = tmp_path / "translations.json"
    translations_payload = {"default_language": "en"}
    translations_path.write_text(json.dumps(translations_payload), encoding="utf-8")

    redis_path = tmp_path / "redis_keys.json"
    redis_payload = {
        "engine": {"stage_lock_prefix": "custom:", "stop_request": "custom_stop"},
        "player_report": {"cache_prefix": "custom:player:"},
    }
    redis_path.write_text(json.dumps(redis_payload), encoding="utf-8")

    emojis_path = tmp_path / "emojis.json"
    emojis_payload = {"chips": {"pot": "P", "stack": "S"}}
    emojis_path.write_text(json.dumps(emojis_payload), encoding="utf-8")

    roles_path = tmp_path / "roles.json"
    roles_payload = {
        "default_language": "en",
        "roles": {"dealer": {"en": "DealerX"}},
    }
    roles_path.write_text(json.dumps(roles_payload), encoding="utf-8")

    hands_path = tmp_path / "hands.json"
    hands_payload = {
        "default_language": "en",
        "hands": {"ROYAL_FLUSH": {"emoji": "*", "en": "Royal"}},
    }
    hands_path.write_text(json.dumps(hands_payload), encoding="utf-8")

    constants = GameConstants(
        translations_path=str(translations_path),
        redis_keys_path=str(redis_path),
        emojis_path=str(emojis_path),
        roles_path=str(roles_path),
        hands_path=str(hands_path),
    )

    assert constants.roles["roles"]["dealer"]["en"] == "DealerX"
    assert constants.hands["hands"]["ROYAL_FLUSH"]["emoji"] == "*"
    assert constants.emojis["chips"]["pot"] == "P"
    assert constants.redis_keys["engine"]["stage_lock_prefix"] == "custom:"
    assert constants.redis_keys["engine"]["stop_request"] == "custom_stop"
    assert constants.redis_keys["player_report"]["cache_prefix"] == "custom:player:"


def _clear_webhook_env(monkeypatch):
    env_vars = [
        "POKERBOT_WEBHOOK_PUBLIC_URL",
        "POKERBOT_WEBHOOK_DOMAIN",
        "POKERBOT_WEBHOOK_PATH",
        "POKERBOT_WEBHOOK_LISTEN",
        "POKERBOT_WEBHOOK_PORT",
    ]
    for env_var in env_vars:
        monkeypatch.delenv(env_var, raising=False)


def test_config_derives_public_url_from_public_listen_and_port(monkeypatch):
    _clear_webhook_env(monkeypatch)

    monkeypatch.setenv("POKERBOT_WEBHOOK_LISTEN", "203.0.113.10")
    monkeypatch.setenv("POKERBOT_WEBHOOK_PORT", "8080")

    cfg = Config()

    assert (
        cfg.WEBHOOK_PUBLIC_URL
        == "http://203.0.113.10:8080/telegram/webhook-poker2025"
    )


def test_config_does_not_derive_public_url_from_loopback_listen(monkeypatch):
    _clear_webhook_env(monkeypatch)

    monkeypatch.setenv("POKERBOT_WEBHOOK_LISTEN", "127.0.0.1")
    monkeypatch.setenv("POKERBOT_WEBHOOK_PORT", "8080")

    cfg = Config()

    assert cfg.WEBHOOK_PUBLIC_URL == ""
