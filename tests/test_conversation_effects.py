from elvin.services.conversation_effects import (
    any_effect_enabled,
    default_effects_config,
    effect_catalog_api,
    normalize_effects_config,
    phrases_from_value,
)


def test_catalog_contains_ten_opt_in_effects() -> None:
    catalog = effect_catalog_api()
    assert len(catalog["effects"]) == 10
    assert len(catalog["defaults"]) == 10
    assert all(not values["enabled"] for values in catalog["defaults"].values())
    assert not any_effect_enabled(catalog["defaults"])


def test_effect_values_are_completed_and_bounded() -> None:
    normalized = normalize_effects_config(
        {
            "natural_interruption": {
                "enabled": True,
                "duck_db": -999,
                "release_ms": 99999,
            },
            "listener_backchannels": {
                "enabled": True,
                "phrases": "угу\nага",
                "max_per_turn": 99,
            },
        }
    )
    assert normalized["natural_interruption"]["enabled"] is True
    assert normalized["natural_interruption"]["duck_db"] == -30
    assert normalized["natural_interruption"]["release_ms"] == 1000
    assert normalized["listener_backchannels"]["max_per_turn"] == 5
    assert normalized["listener_backchannels"]["opportunity_silence_ms"] == 220
    assert any_effect_enabled(normalized)


def test_phrase_list_is_deduplicated_and_bounded() -> None:
    assert phrases_from_value("угу; ага\nугу\n") == ["угу", "ага"]
    assert len(phrases_from_value("\n".join(f"p{i}" for i in range(50)))) == 20


def test_defaults_are_independent_copies() -> None:
    first = default_effects_config()
    second = default_effects_config()
    first["natural_interruption"]["release_ms"] = 999
    assert second["natural_interruption"]["release_ms"] == 280
