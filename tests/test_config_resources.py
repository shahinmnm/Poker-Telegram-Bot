import json

from pokerapp.config import GameConstants


def test_game_constants_loads_external_resources(tmp_path):
    translations_path = tmp_path / "translations.json"
    translations_payload = {
        "default_language": "en",
        "roles": {
            "dealer": {"en": "DealerX"}
        },
    }
    translations_path.write_text(json.dumps(translations_payload), encoding="utf-8")

    redis_path = tmp_path / "redis_keys.json"
    redis_payload = {"engine": {"stage_lock_prefix": "custom:", "stop_request": "custom_stop"}}
    redis_path.write_text(json.dumps(redis_payload), encoding="utf-8")

    constants = GameConstants(
        translations_path=str(translations_path),
        redis_keys_path=str(redis_path),
    )

    assert constants.translations["roles"]["dealer"]["en"] == "DealerX"
    assert constants.redis_keys["engine"]["stage_lock_prefix"] == "custom:"
    assert constants.redis_keys["engine"]["stop_request"] == "custom_stop"
