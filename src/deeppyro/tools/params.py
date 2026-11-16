"""Typed tool arguments: pydantic models plus wire-schema derivation.

Tools declare a nested Args model (a ToolArgs subclass); when a tool does not
spell out parameters itself the registry-facing JSON schema is derived from that
model with derive_parameters(). The rendered schema is byte-compatible with the
hand-written dictionaries that used to live in the tool modules: no title or
default decoration, the same property order, and the same required ordering.

Validation goes through pydantic as well. ToolArgs.parse() converts a pydantic
ValidationError into the historical ToolError wording so model-facing messages
do not change.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Self, cast

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, WithJsonSchema

from deeppyro.core.errors import ToolError


def schema_enum(*values: str) -> dict[str, Any]:
    """Field metadata that pins a string field's wire enum without typing it.

    Some legacy schemas advertise an enum while the tool validates the value
    itself (or hands it to a service that does). Typing those fields as Literal
    would change the error wording, so the enum is schema decoration only.
    """
    return {"enum": list(values)}


def schema_number() -> dict[str, Any]:
    """Field metadata that keeps a historical `type: number` wire schema."""
    return {"type": "number"}


class ToolArgs(BaseModel):
    """Base class for every tool's argument model.

    Validation is strict: the legacy tools rejected "2" for integer fields and
    coerced nothing, so the models do not coerce either.
    """

    model_config = ConfigDict(strict=True, extra="ignore")

    #: additionalProperties rendered on the top-level schema object.
    #: False appends "additionalProperties": False; None omits the key
    #: (ask_user_question keeps its historical shape).
    SCHEMA_TOP_LEVEL_ADDITIONAL_PROPERTIES: ClassVar[bool | None] = False

    @classmethod
    def parse(cls, args: Any) -> Self:
        """Validate raw tool arguments, raising ToolError on failure."""
        if not isinstance(args, dict):
            raise ToolError("tool arguments must be an object")
        try:
            return cls.model_validate(args)
        except ValidationError as exc:
            raise ToolError(cls.format_validation_error(exc)) from None

    # -- error wording -----------------------------------------------------
    @classmethod
    def missing_message(cls, field: str) -> str:
        """Wording for a required argument that was not provided."""
        return f"{field} is required"

    @classmethod
    def field_error_message(cls, field: str, error: dict[str, Any]) -> str | None:
        """Tool-specific wording for one field error, or None for the default."""
        return None

    @classmethod
    def format_validation_error(cls, exc: ValidationError) -> str:
        error = _primary_error(exc)
        loc: tuple[Any, ...] = tuple(error.get("loc") or ())
        field = _field_name(loc)
        if field is None:
            return str(error.get("msg", "invalid arguments"))
        custom = cls.field_error_message(field, error)
        if custom is not None:
            return custom
        if error.get("type") == "extra_forbidden" and loc:
            return f"{_render_path(loc[:-1])} has unexpected fields: {loc[-1]}"
        if error.get("type") == "missing":
            return cls.missing_message(field)
        if error.get("input") is None and _is_required(cls, field):
            return cls.missing_message(field)
        return _default_message(field, error, loc)


def require_value(value: str | None, field: str) -> str:
    """Conditional-required string check with the legacy require_str wording."""
    if value is None:
        raise ToolError(f"{field} is required")
    if not value.strip():
        raise ToolError(f"{field} must be a non-empty string")
    return value


def _primary_error(exc: ValidationError) -> dict[str, Any]:
    errors = exc.errors()
    if not errors:
        return {"type": "value_error", "msg": "invalid arguments"}
    return cast(dict[str, Any], errors[0])


def _field_name(loc: tuple[Any, ...]) -> str | None:
    names = [part for part in loc if isinstance(part, str)]
    return names[0] if names else None


def _is_required(cls: type[ToolArgs], field: str) -> bool:
    info = cls.model_fields.get(field)
    return bool(info is not None and info.is_required())


def _render_path(loc: tuple[Any, ...]) -> str:
    rendered = ""
    for part in loc:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif rendered:
            rendered += f".{part}"
        else:
            rendered = str(part)
    return rendered or "arguments"


_TYPE_MESSAGES: dict[str, str] = {
    "string_type": "must be a string",
    "string_unicode": "must be a string",
    "int_type": "must be an integer",
    "int_parsing": "must be an integer",
    "int_from_float": "must be an integer",
    "bool_type": "must be a boolean",
    "bool_parsing": "must be a boolean",
    "list_type": "must be a list",
    "tuple_type": "must be a list",
    "dict_type": "must be an object",
    "model_type": "must be an object",
    "model_attributes_type": "must be an object",
}


def _default_message(field: str, error: dict[str, Any], loc: tuple[Any, ...]) -> str:
    kind = str(error.get("type", ""))
    prefix = _render_path(loc[: len(loc) - 1]) if len(loc) > 1 else ""
    subject = f"{prefix}.{field}" if prefix else field
    if kind in _TYPE_MESSAGES:
        return f"{subject} {_TYPE_MESSAGES[kind]}"
    ctx = error.get("ctx") or {}
    if kind == "literal_error":
        expected = str(ctx.get("expected", ""))
        values = ", ".join(part.strip().strip("'\"") for part in expected.replace(" or ", ", ").split(","))
        return f"{field} must be one of: {values}"
    if kind == "greater_than_equal":
        return f"{field} must be greater than or equal to {ctx.get('ge')}"
    if kind == "greater_than":
        return f"{field} must be greater than {ctx.get('gt')}"
    if kind == "less_than_equal":
        return f"{field} must be less than or equal to {ctx.get('le')}"
    if kind == "less_than":
        return f"{field} must be less than {ctx.get('lt')}"
    if kind == "too_short":
        return f"{field} must contain at least {ctx.get('min_length')} items"
    if kind == "too_long":
        return f"{field} must contain at most {ctx.get('max_length')} items"
    if kind == "string_too_short":
        return f"{field} must be a non-empty string"
    if kind == "value_error":
        return str(error.get("msg", "")).removeprefix("Value error, ")
    return f"{field} {error.get('msg', 'is invalid')}"


# -- schema derivation -----------------------------------------------------
def derive_parameters(model: type[BaseModel]) -> dict[str, Any]:
    """Render the historical tool schema for a pydantic argument model."""
    schema = model.model_json_schema()
    definitions: dict[str, Any] = schema.get("$defs") or {}
    top_level = getattr(model, "SCHEMA_TOP_LEVEL_ADDITIONAL_PROPERTIES", False)
    return _object_node(schema, definitions, top_level=True, top_level_extra=top_level)


def derive_property(model: type[BaseModel], name: str) -> dict[str, Any]:
    """Render one property node of a model (used for dynamic schemas)."""
    schema = model.model_json_schema()
    definitions: dict[str, Any] = schema.get("$defs") or {}
    return _field_node(schema["properties"][name], definitions)


def _resolve(node: Any, definitions: dict[str, Any]) -> dict[str, Any]:
    seen = 0
    while isinstance(node, dict) and "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        node = definitions[name]
        seen += 1
        if seen > 32:  # pragma: no cover - defensive against recursive models
            raise ValueError(f"unresolvable schema ref {name}")
    return dict(node) if isinstance(node, dict) else {}


def _object_node(
    node: dict[str, Any],
    definitions: dict[str, Any],
    *,
    top_level: bool = False,
    top_level_extra: bool | None = False,
) -> dict[str, Any]:
    resolved = _resolve(node, definitions)
    properties = resolved.get("properties") or {}
    rendered: dict[str, Any] = {"type": "object"}
    extra = resolved.get("additionalProperties")
    if not top_level and extra is True:
        rendered["additionalProperties"] = True
    rendered["properties"] = {name: _field_node(value, definitions) for name, value in properties.items()}
    required = [name for name in resolved.get("required") or [] if name in properties]
    if required:
        rendered["required"] = required
    if top_level:
        if top_level_extra is not None:
            rendered["additionalProperties"] = top_level_extra
    elif extra is False:
        rendered["additionalProperties"] = False
    return rendered


def _field_node(node: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    original = _resolve(node, definitions)
    description = original.get("description")
    # json_schema_extra metadata lands on the node itself; remember it before
    # unwrapping Optional[...] so the wire type/enum overrides survive.
    override_type = original.get("type")
    override_enum = original.get("enum")
    type_name = override_type
    if type_name is None and "anyOf" in original:
        branches = [_resolve(branch, definitions) for branch in original["anyOf"]]
        branch = next((item for item in branches if item.get("type") != "null"), {})
        if override_type is not None:
            branch["type"] = override_type
        if override_enum is not None and "enum" not in branch:
            branch["enum"] = list(override_enum)
        original = {**branch, "description": description}
        type_name = original.get("type")
    if type_name == "array":
        return _array_node(original, definitions, description)
    if type_name == "object":
        if "properties" not in original:
            # A free-form object (settings_write.patch): the wire schema is the
            # bare type plus its description, with no properties to enumerate.
            bare_object: dict[str, Any] = {"type": "object"}
            if description is not None:
                bare_object["description"] = description
            return bare_object
        return _object_node(original, definitions)
    if "enum" in original:
        rendered_enum: dict[str, Any] = {"type": type_name or "string", "enum": list(original["enum"])}
        if description is not None:
            rendered_enum["description"] = description
        return rendered_enum
    rendered: dict[str, Any] = {"type": type_name or "string"}
    if description is not None:
        rendered["description"] = description
    return rendered


def _array_node(node: dict[str, Any], definitions: dict[str, Any], description: Any) -> dict[str, Any]:
    items = _resolve(node.get("items") or {}, definitions)
    if items.get("type") == "object" or "properties" in items:
        rendered_array: dict[str, Any] = {"type": "array"}
        if description is not None:
            rendered_array["description"] = description
        rendered_array["items"] = _object_node(items, definitions)
        return rendered_array
    rendered: dict[str, Any] = {"type": "array", "items": {"type": items.get("type", "string")}}
    for key in ("minItems", "maxItems"):
        if key in node:
            rendered[key] = node[key]
    if description is not None:
        rendered["description"] = description
    return rendered


# -- annotated string types ------------------------------------------------
def _require_non_empty(value: Any, info: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        raise ToolError(f"{info.field_name} must be a non-empty string")
    return value


def _require_non_empty_stripped(value: Any, info: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{info.field_name} must be a non-empty string")
    return value.strip()


def _optional_non_empty(value: Any, info: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    if not value.strip():
        raise ToolError(f"{info.field_name} must be a non-empty string")
    return value


def _optional_stripped(value: Any, info: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    return value.strip() or None


NonEmptyStr = Annotated[str, BeforeValidator(_require_non_empty)]
StrippedStr = Annotated[str, BeforeValidator(_require_non_empty_stripped)]
OptionalNonEmptyStr = Annotated[str | None, BeforeValidator(_optional_non_empty)]
OptionalStrippedStr = Annotated[str | None, BeforeValidator(_optional_stripped)]
#: An arbitrary string-keyed object; renders as a bare "type": "object".
FreeFormObject = Annotated[dict[str, Any], WithJsonSchema({"type": "object"})]


__all__ = [
    "FreeFormObject",
    "NonEmptyStr",
    "OptionalNonEmptyStr",
    "OptionalStrippedStr",
    "StrippedStr",
    "ToolArgs",
    "derive_parameters",
    "derive_property",
    "require_value",
    "schema_enum",
    "schema_number",
]
