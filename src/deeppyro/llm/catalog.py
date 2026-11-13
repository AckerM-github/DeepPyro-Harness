"""Model catalog values shared by discovery, providers and the UI layer.

DSH splits model metadata in two layers (see docs/plan/p1-providers.md and
docs/spec/llm.md section 13):

* a catalog entry answers "which models may a client offer/select?" and carries only
  {provider, id, name, description?, inputModalities?, badge?}. contextWindow /
  default output limit / reasoning efforts belong to resolveModelInfo, not listModels.
* discovery (GET /models) is one source of catalog entries; the configured
  ProviderConfig.models list is the always-available floor, because DSH keeps
  configured-but-unlisted model ids routable.

ModelInfo below is the Python value object for one catalog entry. It is a superset of
the DSH catalog entry (context_window/max_tokens are optional) so the TUI can show a
context window without a second resolve call; nothing in the streaming path requires it.

parse_model_listing() accepts the two shapes DSH probes (pi-ai readListing):
  - OpenAI /v1/models and Anthropic /v1/models: {"data": [{id, ...}, ...]}
  - some gateways: {"models": {"<id>": {...}, ...}} (pi-ai "models object" form)
plus a bare JSON list for lenient local servers. Invalid entries are dropped rather than
failing the whole listing; a payload with no recognizable container raises PROTOCOL.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from deeppyro.config import ModelConfig, ProviderConfig
from deeppyro.llm.errors import CODE_PROTOCOL, LlmRequestError

BadgeTone = Literal["neutral", "info", "success", "warning"]

#: Badge tones DSH allows (dsh-llm-deepseek lib/index.js BADGE_TONES). Unknown tones are
#: normalized to "neutral" instead of failing the listing.
BADGE_TONES: tuple[BadgeTone, ...] = ("neutral", "info", "success", "warning")

#: Input modalities the catalog understands. Anything else is dropped; an entry with no
#: usable modality falls back to ("text",).
INPUT_MODALITIES: frozenset[str] = frozenset({"text", "image"})

_DEFAULT_MODALITIES: tuple[str, ...] = ("text",)

# Field aliases observed in the wild (pi-ai readListing and Anthropic listings).
_CONTEXT_KEYS = (
    "contextWindow",
    "context_window",
    "context_length",
    "max_input_tokens",
    "maxInputTokens",
)
_MAX_TOKENS_KEYS = (
    "maxTokens",
    "max_tokens",
    "maxOutputTokens",
    "max_output_tokens",
    "max_completion_tokens",
    "maxCompletionTokens",
)
_MODALITY_KEYS = ("inputModalities", "input_modalities", "input")
_NAME_KEYS = ("name", "display_name", "displayName")


@dataclass(frozen=True, slots=True)
class ModelBadge:
    """Optional display badge for one catalog entry.

    text is either a single label or a mapping of lower-case language id -> label
    (DSH requires the map to contain "en"). This port does not translate; label()
    resolves the requested language for callers that want one.
    """

    text: str | Mapping[str, str]
    tone: BadgeTone = "neutral"

    def label(self, language: str = "en") -> str:
        """Resolve the badge text for one language, falling back to "en"/first entry."""
        if isinstance(self.text, str):
            return self.text
        for key in (language, "en"):
            value = self.text.get(key)
            if isinstance(value, str) and value:
                return value
        for value in self.text.values():
            if isinstance(value, str) and value:
                return value
        return ""

    def to_dict(self) -> dict[str, Any]:
        text: str | dict[str, str]
        text = self.text if isinstance(self.text, str) else dict(self.text)
        return {"text": text, "tone": self.tone}


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One catalog entry (provider + model id + display metadata)."""

    provider: str
    id: str
    name: str = ""
    description: str = ""
    context_window: int | None = None
    max_tokens: int | None = None
    input_modalities: tuple[str, ...] = _DEFAULT_MODALITIES
    badge: ModelBadge | None = None

    @property
    def label(self) -> str:
        """Display name, falling back to the model id."""
        return self.name or self.id

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready summary used by inspect/TUI (camelCase like the wire records)."""
        row: dict[str, Any] = {
            "provider": self.provider,
            "id": self.id,
            "name": self.label,
            "inputModalities": list(self.input_modalities),
        }
        if self.description:
            row["description"] = self.description
        if self.context_window is not None:
            row["contextWindow"] = self.context_window
        if self.max_tokens is not None:
            row["maxTokens"] = self.max_tokens
        if self.badge is not None:
            row["badge"] = self.badge.to_dict()
        return row


@runtime_checkable
class ModelCatalog(Protocol):
    """Optional capability: a provider that can advertise a model listing.

    Deliberately separate from core.protocols.LlmProvider so existing LlmProvider
    implementations (tests/integration/fake_provider.py) keep working unchanged.
    """

    async def list_models(self) -> list[ModelInfo]: ...


def catalog_from_config(provider: str, pc: ProviderConfig) -> list[ModelInfo]:
    """Build the catalog floor from ProviderConfig.models.

    Extra (underscore-loose) config fields are honoured because ProviderConfig and
    ModelConfig are extra="allow": maxTokens/max_tokens and inputModalities/
    input_modalities let a config declare richer entries without core changes.
    """
    entries: list[ModelInfo] = []
    for model in pc.models:
        entries.append(_model_from_config(provider, model))
    return entries


def parse_model_listing(provider: str, payload: Any, *, source: str = "") -> list[ModelInfo]:
    """Parse an OpenAI/Anthropic style model listing into catalog entries.

    Malformed entries are dropped; duplicate ids keep the first entry. A payload that is
    not a listing at all raises LlmRequestError(code="PROTOCOL") so discovery callers can
    fall back to the configured catalog.
    """
    prefix = f"{provider}: " + (source if source else "model listing")
    entries = _listing_entries(payload, provider, prefix)
    models: list[ModelInfo] = []
    seen: set[str] = set()
    for key, raw in entries:
        info = _entry_from_raw(provider, key, raw)
        if info is None or info.id in seen:
            continue
        seen.add(info.id)
        models.append(info)
    return models


def merge_catalogs(base: Sequence[ModelInfo], overlay: Sequence[ModelInfo]) -> list[ModelInfo]:
    """Layer a discovered listing over the configured catalog (DSH resolveListedCatalog).

    Configured models stay visible; a discovered entry with the same id overrides display
    metadata but keeps the configured context window / output limit when the listing does
    not provide them.
    """
    merged: dict[str, ModelInfo] = {info.id: info for info in base}
    order: list[str] = [info.id for info in base]
    for item in overlay:
        existing = merged.get(item.id)
        if existing is None:
            merged[item.id] = item
            order.append(item.id)
        else:
            merged[item.id] = _overlay_info(existing, item)
    return [merged[model_id] for model_id in order]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _model_from_config(provider: str, model: ModelConfig) -> ModelInfo:
    extra = model.model_extra or {}
    modalities = _modalities(extra.get("inputModalities", extra.get("input_modalities")))
    return ModelInfo(
        provider=provider,
        id=model.id,
        name=model.name or model.id,
        context_window=_positive_int(model.contextWindow),
        max_tokens=_first_positive_int(extra, _MAX_TOKENS_KEYS),
        input_modalities=modalities,
    )


def _listing_entries(payload: Any, provider: str, prefix: str) -> list[tuple[str | None, Any]]:
    if isinstance(payload, list):
        return [(None, item) for item in payload]
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            return [(None, item) for item in data]
        models = payload.get("models")
        if isinstance(models, Mapping):
            return list(models.items())
        if isinstance(models, list):
            return [(None, item) for item in models]
    raise LlmRequestError(
        f"{prefix} is not a model listing (expected a JSON list, {{'data': [...]}} or {{'models': {{...}}}})",
        provider=provider,
        code=CODE_PROTOCOL,
    )


def _entry_from_raw(provider: str, key: str | None, raw: Any) -> ModelInfo | None:
    if not isinstance(raw, Mapping):
        return None
    raw_id = key if key is not None else raw.get("id")
    model_id = _text(raw_id)
    if model_id is None:
        return None
    name = _first_text(raw, _NAME_KEYS) or model_id
    description = _text(raw.get("description")) or ""
    context_window = _first_positive_int(raw, _CONTEXT_KEYS)
    if context_window is None:
        limit = raw.get("limit")
        if isinstance(limit, Mapping):
            context_window = _positive_int(limit.get("context"))
    max_tokens = _first_positive_int(raw, _MAX_TOKENS_KEYS)
    if max_tokens is None:
        top_provider = raw.get("top_provider")
        if isinstance(top_provider, Mapping):
            max_tokens = _positive_int(top_provider.get("max_completion_tokens"))
    if max_tokens is None:
        limit = raw.get("limit")
        if isinstance(limit, Mapping):
            max_tokens = _positive_int(limit.get("output"))
    modalities = _modalities(_first_present(raw, _MODALITY_KEYS))
    return ModelInfo(
        provider=provider,
        id=model_id,
        name=name,
        description=description,
        context_window=context_window,
        max_tokens=max_tokens,
        input_modalities=modalities,
        badge=_parse_badge(raw.get("badge")),
    )


def _overlay_info(base: ModelInfo, item: ModelInfo) -> ModelInfo:
    name = item.name if item.name and item.name != item.id else (base.name or item.id)
    return ModelInfo(
        provider=item.provider,
        id=item.id,
        name=name,
        description=item.description or base.description,
        context_window=item.context_window or base.context_window,
        max_tokens=item.max_tokens or base.max_tokens,
        input_modalities=item.input_modalities or base.input_modalities,
        badge=item.badge or base.badge,
    )


def _parse_badge(value: Any) -> ModelBadge | None:
    if not isinstance(value, Mapping):
        return None
    raw_text = value.get("text")
    text: str | dict[str, str] | None
    if isinstance(raw_text, str):
        stripped = raw_text.strip()
        text = stripped or None
    elif isinstance(raw_text, Mapping):
        translated = {
            str(key): str(label).strip()
            for key, label in raw_text.items()
            if isinstance(label, str) and label.strip()
        }
        text = translated or None
    else:
        text = None
    if text is None:
        return None
    tone = value.get("tone")
    normalized: BadgeTone = tone if tone in BADGE_TONES else "neutral"
    return ModelBadge(text=text, tone=normalized)


def _modalities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return _DEFAULT_MODALITIES
    seen: list[str] = []
    for raw in value:
        if isinstance(raw, str) and raw in INPUT_MODALITIES and raw not in seen:
            seen.append(raw)
    return tuple(seen) or _DEFAULT_MODALITIES


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first_text(raw: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        found = _text(raw.get(key))
        if found is not None:
            return found
    return None


def _first_present(raw: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    return None


def _first_positive_int(raw: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        found = _positive_int(raw.get(key))
        if found is not None:
            return found
    return None


__all__ = [
    "BADGE_TONES",
    "INPUT_MODALITIES",
    "BadgeTone",
    "ModelBadge",
    "ModelCatalog",
    "ModelInfo",
    "catalog_from_config",
    "merge_catalogs",
    "parse_model_listing",
]
