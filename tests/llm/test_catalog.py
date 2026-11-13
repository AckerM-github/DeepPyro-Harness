"""Model catalog value objects and listing parsing.

Payload shapes mirror what DSH reads from /v1/models probes:
  - {"data": [{"id": "deepseek-chat", "context_length": 65536}, ...]}  (OpenAI/Anthropic)
  - {"models": {"<id>": {"name": ...}}}                                 (gateway "models" object)
"""

from __future__ import annotations

import pytest

from deeppyro.config import ModelConfig, ProviderConfig
from deeppyro.llm.catalog import (
    BADGE_TONES,
    ModelBadge,
    ModelCatalog,
    ModelInfo,
    catalog_from_config,
    merge_catalogs,
    parse_model_listing,
)
from deeppyro.llm.errors import CODE_PROTOCOL, LlmRequestError


def provider_config(**kwargs: object) -> ProviderConfig:
    data = {"api": "openai-completions", "baseURL": "http://127.0.0.1:8000/v1", "models": []}
    data.update(kwargs)
    return ProviderConfig.model_validate(data)


# --------------------------------------------------------------------------
# ModelBadge / ModelInfo
# --------------------------------------------------------------------------


def test_badge_label_resolves_language_map() -> None:
    badge = ModelBadge(text={"en": "New", "zh": "新"}, tone="info")
    assert badge.label() == "New"
    assert badge.label("zh") == "新"
    assert badge.label("fr") == "New"  # falls back to en
    assert badge.to_dict() == {"text": {"en": "New", "zh": "新"}, "tone": "info"}


def test_badge_label_falls_back_to_first_entry_and_empty_map() -> None:
    assert ModelBadge(text={"de": "Neu"}).label() == "Neu"
    assert ModelBadge(text={}).label() == ""


def test_badge_tone_set_matches_dsh() -> None:
    assert BADGE_TONES == ("neutral", "info", "success", "warning")


def test_model_info_label_falls_back_to_id() -> None:
    assert ModelInfo(provider="p", id="m", name="").label == "m"
    assert ModelInfo(provider="p", id="m", name="Model M").label == "Model M"


def test_model_info_to_dict_minimal_uses_camel_case() -> None:
    row = ModelInfo(provider="p", id="m").to_dict()
    assert row == {"provider": "p", "id": "m", "name": "m", "inputModalities": ["text"]}


def test_model_info_to_dict_includes_optional_fields() -> None:
    info = ModelInfo(
        provider="p",
        id="m",
        name="M",
        description="fast",
        context_window=65536,
        max_tokens=8192,
        input_modalities=("text", "image"),
        badge=ModelBadge(text="Beta", tone="warning"),
    )
    assert info.to_dict() == {
        "provider": "p",
        "id": "m",
        "name": "M",
        "description": "fast",
        "contextWindow": 65536,
        "maxTokens": 8192,
        "inputModalities": ["text", "image"],
        "badge": {"text": "Beta", "tone": "warning"},
    }


# --------------------------------------------------------------------------
# catalog_from_config
# --------------------------------------------------------------------------


def test_catalog_from_config_reads_models() -> None:
    pc = provider_config(
        models=[
            {"id": "deepseek-chat", "name": "DeepSeek Chat", "contextWindow": 65536},
            {"id": "local"},
        ]
    )
    models = catalog_from_config("deepseek", pc)
    assert [m.id for m in models] == ["deepseek-chat", "local"]
    assert models[0].name == "DeepSeek Chat"
    assert models[0].context_window == 65536
    assert models[0].provider == "deepseek"
    assert models[1].name == "local"
    assert models[1].context_window is None


def test_catalog_from_config_accepts_extra_fields() -> None:
    pc = provider_config(
        models=[
            {
                "id": "vision",
                "contextWindow": 8192,
                "maxTokens": 2048,
                "inputModalities": ["text", "image", "audio"],
            }
        ]
    )
    models = catalog_from_config("p", pc)
    assert models[0].max_tokens == 2048
    assert models[0].input_modalities == ("text", "image")


def test_catalog_from_config_empty_provider() -> None:
    assert catalog_from_config("p", provider_config()) == []


def test_model_config_is_public_type() -> None:
    # Guard against a future refactor that stops using config.ModelConfig here.
    assert isinstance(provider_config(models=[{"id": "m"}]).models[0], ModelConfig)


# --------------------------------------------------------------------------
# parse_model_listing
# --------------------------------------------------------------------------


def test_parse_openai_data_listing() -> None:
    payload = {
        "object": "list",
        "data": [
            {
                "id": "deepseek-chat",
                "name": "DeepSeek Chat",
                "description": "general",
                "context_length": 65536,
                "max_output_tokens": 8192,
                "input_modalities": ["text", "image"],
                "badge": {"text": "New", "tone": "success"},
            },
            "not-an-object",
            {"id": ""},
            {"name": "missing id"},
        ],
    }
    models = parse_model_listing("deepseek", payload)
    assert len(models) == 1
    info = models[0]
    assert (info.provider, info.id) == ("deepseek", "deepseek-chat")
    assert info.name == "DeepSeek Chat"
    assert info.description == "general"
    assert info.context_window == 65536
    assert info.max_tokens == 8192
    assert info.input_modalities == ("text", "image")
    assert info.badge is not None and info.badge.to_dict() == {"text": "New", "tone": "success"}


def test_parse_accepts_bare_list_and_models_object() -> None:
    bare = parse_model_listing("p", [{"id": "a"}, {"id": "b", "display_name": "B"}])
    assert [m.id for m in bare] == ["a", "b"]
    assert bare[1].name == "B"

    object_form = parse_model_listing("p", {"models": {"a": {"name": "A"}, "b": {"contextWindow": 4096}}})
    assert [m.id for m in object_form] == ["a", "b"]
    assert object_form[0].name == "A"
    assert object_form[1].context_window == 4096


def test_parse_supports_anthropic_limit_and_openrouter_top_provider() -> None:
    models = parse_model_listing(
        "p",
        {
            "data": [
                {"id": "claude", "limit": {"context": 200000, "output": 8192}},
                {"id": "or", "top_provider": {"max_completion_tokens": 4096}},
            ]
        },
    )
    assert models[0].context_window == 200000
    assert models[0].max_tokens == 8192
    assert models[1].max_tokens == 4096


def test_parse_normalizes_unknown_badge_tone_and_modalities() -> None:
    info = parse_model_listing(
        "p",
        {
            "data": [
                {
                    "id": "m",
                    "badge": {"text": {"en": "Alpha"}, "tone": "purple"},
                    "input_modalities": ["audio"],
                }
            ]
        },
    )[0]
    assert info.badge is not None
    assert info.badge.tone == "neutral"
    assert info.badge.label() == "Alpha"
    assert info.input_modalities == ("text",)


def test_parse_ignores_bad_badge_shapes() -> None:
    info = parse_model_listing("p", {"data": [{"id": "m", "badge": "New"}]})[0]
    assert info.badge is None


def test_parse_duplicate_ids_keep_first() -> None:
    models = parse_model_listing("p", {"data": [{"id": "m", "name": "first"}, {"id": "m", "name": "second"}]})
    assert len(models) == 1
    assert models[0].name == "first"


def test_parse_empty_listing_is_empty() -> None:
    assert parse_model_listing("p", {"data": []}) == []
    assert parse_model_listing("p", {"models": []}) == []


@pytest.mark.parametrize("payload", [None, "text", 42, {"unexpected": True}, {"data": "nope"}])
def test_parse_rejects_non_listing_payload(payload: object) -> None:
    with pytest.raises(LlmRequestError) as excinfo:
        parse_model_listing("p", payload, source="listing from test")
    assert excinfo.value.code == CODE_PROTOCOL
    assert "listing from test" in str(excinfo.value)


# --------------------------------------------------------------------------
# merge_catalogs
# --------------------------------------------------------------------------


def _info(model_id: str, **kwargs: object) -> ModelInfo:
    return ModelInfo(provider="p", id=model_id, **kwargs)  # type: ignore[arg-type]


def test_merge_overlays_listing_and_keeps_configured_floor() -> None:
    base = [
        _info("configured-only", name="Configured Only", context_window=32768),
        _info("shared", name="shared", context_window=65536, max_tokens=4096),
    ]
    overlay = [
        _info("shared", name="Shared", description="listed", input_modalities=("text", "image")),
        _info("new-model", name="New"),
    ]
    merged = merge_catalogs(base, overlay)
    assert [m.id for m in merged] == ["configured-only", "shared", "new-model"]

    shared = merged[1]
    assert shared.name == "Shared"  # listing display name wins
    assert shared.description == "listed"
    assert shared.input_modalities == ("text", "image")
    # listing omitted context/max, so the configured values survive
    assert shared.context_window == 65536
    assert shared.max_tokens == 4096


def test_merge_keeps_configured_name_when_listing_name_is_just_the_id() -> None:
    merged = merge_catalogs(
        [_info("m", name="Friendly Name", context_window=8192)],
        [_info("m", name="m")],
    )
    assert merged[0].name == "Friendly Name"


def test_merge_empty_base_returns_overlay_order() -> None:
    merged = merge_catalogs([], [_info("b"), _info("a")])
    assert [m.id for m in merged] == ["b", "a"]


# --------------------------------------------------------------------------
# ModelCatalog protocol
# --------------------------------------------------------------------------


def test_model_catalog_protocol_is_structural() -> None:
    class WithListing:
        async def list_models(self) -> list[ModelInfo]:
            return []

    class WithoutListing:
        pass

    assert isinstance(WithListing(), ModelCatalog)
    assert not isinstance(WithoutListing(), ModelCatalog)
