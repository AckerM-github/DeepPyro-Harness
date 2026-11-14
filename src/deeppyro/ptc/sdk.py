"""Python SDK renderer for programmatic tool calling.

The PTC executor hides native tool schemas from the request and instead injects
a generated Python SDK into the system prompt: a Tools protocol whose methods
are the visible tools, plus one TypedDict per argument and result object (and
per nested object).  This module is the pure projection from
deeppyro.core.protocols.ToolSchema to that text; it mirrors the
renderToolsSdkPy renderer in DSH's dsh-tool-executor-ptc package.

The output is advisory prompt text, not a module that is imported: only tools
and ToolCallError are bound at run time.  It must nevertheless be valid Python
and deterministic, because an unchanged tool set has to produce byte-identical
text across prompt assemblies.

Formatting contract:

* A fixed usage preamble (SDK_INSTRUCTIONS), a blank line, then one fenced
  python code block.
* Tools are ordered by name (UTF-16 code-unit order, i.e. JavaScript's <).
* Every tool contributes an async def method on Tools; tools whose names
  cannot be attributes (exotic, keyword, underscore-leading) contribute a
  # tools["name"](...) comment instead and are reached by subscript.
* Object schemas with a legal field set become TypedDict classes declared
  before the enclosing class that references them (post-order); illegal or
  anonymous object shapes degrade to dict[str, Any].
* The from typing import line lists exactly the symbols used, in a fixed
  order; Protocol is always present.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from deeppyro.core.protocols import ToolSchema

# --------------------------------------------------------------------------
# Public constants
# --------------------------------------------------------------------------

SDK_INSTRUCTIONS = """## Writing code for run_code

`run_code` takes two required arguments: `code`, the program to execute, and `description`, a short summary of what the program does. At run time exactly two of the names declared below are bound: `tools` and `ToolCallError`. Everything else is a STATIC STUB describing argument and return types — in particular the `TypedDict` classes do NOT exist at run time, so build arguments as plain `dict`/`list` JSON values: `await tools.name({"field": 1})`, never `FooArgs(field=1)`, which raises `NameError`. Inside the program:

- Call tools as `await tools.name(args)` — subscript access for exotic, reserved, or underscore-leading names: `await tools["my-tool"](args)`. Every call resolves to the tool's typed canonical JSON value (each method's return type below). Tool arguments must be lossless JSON.
- A FAILED tool call raises `ToolCallError`, whose `toolName` identifies the failed tool and whose message is human-readable — wrap in `try/except` to handle and continue.
- Independent read-only calls MAY overlap under `asyncio.gather` (safe calls run concurrently; mutating calls run alone, in submission order). Sequence dependent work with `await`.
- Only values emitted as program output enter the conversation. A successful tool result containing an image is attached after the run so you can inspect it on the next step; every other intermediate result stays out of the conversation, so extract just what you need.

The available tools:"""

#: typing symbols the renderer may emit, in deterministic import order.
_TYPING_ORDER: tuple[str, ...] = ("Any", "Literal", "NotRequired", "Protocol", "TypedDict")

#: Python hard keywords.  A tool with such a name is reached by subscript and an
#: object with such a field degrades to dict[str, Any].  Soft keywords
#: (match/case/type/underscore) are deliberately absent: each is special in
#: exactly one syntactic position, and both an attribute and a class-syntax
#: field are legal.
_RESERVED: frozenset[str] = frozenset(
    {
        "False",
        "None",
        "True",
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
        # Not a keyword, but assigning to it is a compile-time SyntaxError and a
        # TypedDict field is an assignment.
        "__debug__",
    }
)

#: Class-name bases are capped so a deep chain of single-field objects keeps
#: the emitted text linear in depth.  DSH counts UTF-16 code units here; this
#: renderer counts code points, which only differs for astral names.
MAX_CLASS_NAME_BASE = 120

#: Deepest list[...] nesting emitted into one annotation.  CPython's tokenizer
#: rejects a logical line with more than 200 simultaneously open brackets; 180
#: leaves headroom for the annotation's own brackets.
MAX_LIST_NESTING = 180

#: Keys accepted by the enforced JSON-Schema subset shared with DSH.
_CONSTRAINT_KEYWORDS: frozenset[str] = frozenset(
    {"type", "oneOf", "properties", "required", "additionalProperties", "items", "enum", "const"}
)
_ANNOTATION_KEYWORDS: frozenset[str] = frozenset({"description", "title", "default", "examples"})
_SCHEMA_TYPES: frozenset[str] = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)
_ONE_OF_SIBLING_KEYWORDS: tuple[str, ...] = (
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
)

_UNPRINTABLE = re.compile("[\u0000-\u0008\u000e-\u001f\u007f-\u009f]")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_WHITESPACE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def render_tools_sdk(tools: Sequence[ToolSchema]) -> str:
    """Render the tools:sdk prompt block for *tools*.

    The caller decides which tools are visible; this function neither filters
    nor reorders the set beyond the deterministic name sort.  The run_code tool
    itself must already be excluded by the caller.

    Args:
        tools: Tool schemas to declare.  The parameters and output attributes
            are JSON-Schema fragments; an output of None renders as Any.

    Returns:
        A complete prompt section: the usage preamble, a blank line, and one
        fenced python code block.  An empty sequence still yields the preamble
        and a Tools protocol with a pass body.
    """
    ordered = sorted(tools, key=lambda tool: _utf16_sort_key(tool.name))
    state = _RenderState()
    state.typing.add("Protocol")

    members: list[str] = []
    statements = 0
    for tool in ordered:
        arg_type = _render_type_root(tool.parameters, f"{_camel_case(tool.name)}Args", state)
        output_type = _render_type_root(tool.output, f"{_camel_case(tool.name)}Output", state)
        if _is_attr_name(tool.name):
            doc = _doc_lines(tool.description, 2)
            if doc:
                members.append(f"{_pad(1)}async def {tool.name}(self, args: {arg_type}) -> {output_type}:")
                members.extend(doc)
            else:
                members.append(
                    f"{_pad(1)}async def {tool.name}(self, args: {arg_type}) -> {output_type}: ..."
                )
            statements += 1
        else:
            members.append(f"{_pad(1)}# tools[{_json_quote(tool.name)}](args: {arg_type}) -> {output_type}")
            description = _describe(tool)
            if description is not None:
                members.append(f"{_pad(1)}#   {description}")

    # Subscript entries are comments, not statements, so a class body made only
    # of them needs an explicit pass to parse.
    body_lines = members if statements else [f"{_pad(1)}pass", *members]
    body = "\n".join(body_lines)
    imports = [symbol for symbol in _TYPING_ORDER if symbol in state.typing]
    class_block = "\n\n".join(state.classes) + "\n\n" if state.classes else ""
    declaration = (
        f"from typing import {', '.join(imports)}\n\n"
        "class ToolCallError(Exception):\n"
        "    toolName: str\n\n"
        f"{class_block}"
        "class Tools(Protocol):\n"
        f"{body}\n\n"
        "tools: Tools"
    )
    return f"{SDK_INSTRUCTIONS}\n\n```python\n{declaration}\n```"


# --------------------------------------------------------------------------
# Identifier and name helpers
# --------------------------------------------------------------------------


def _utf16_sort_key(name: str) -> bytes:
    """Sort key matching JavaScript string comparison (UTF-16 code units)."""
    return name.encode("utf-16-be", "surrogatepass")


def _is_xid_continue(char: str) -> bool:
    """Whether *char* can continue a Python identifier (XID_Continue)."""
    return f"a{char}".isidentifier()


def _is_xid_start(char: str) -> bool:
    """Whether *char* can start a Python identifier (XID_Start or underscore)."""
    return char.isidentifier()


def _is_bare_identifier(name: str) -> bool:
    """Whether *name* is a bare Python identifier stable under NFKC.

    str.isidentifier supplies the XID start/continue grammar; the NFKC check is
    separate because CPython normalizes identifiers at compile time while JSON
    keys are compared as written.  A name such as the ligature "fi" would be
    declared as "fi", so it takes the exact-bytes path instead.
    """
    return name.isidentifier() and unicodedata.normalize("NFKC", name) == name


def _camel_case(raw: str) -> str:
    """Turn a tool or field name into a class-name segment (read_file -> ReadFile).

    Characters outside XID_Continue (and underscore explicitly) split words;
    each word's first character is uppercased.  A head that cannot start an
    identifier takes a Tool prefix so "1x" becomes "Tool1x".  The result is
    NFKC-normalized because it is generated text, never matched against a JSON
    key.
    """
    parts: list[str] = []
    current: list[str] = []
    for char in raw:
        if char == "_" or not _is_xid_continue(char):
            if current:
                parts.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    joined = "".join(f"{part[0].upper()}{part[1:]}" for part in parts)
    joined = unicodedata.normalize("NFKC", joined)
    head = joined[:1]
    if not head or not _is_xid_start(head):
        joined = f"Tool{joined}"
    return unicodedata.normalize("NFKC", joined)


def _cap_class_name_base(base: str) -> str:
    """Cap a class-name base at MAX_CLASS_NAME_BASE code points."""
    return base[:MAX_CLASS_NAME_BASE]


def _allocate_class_name(base: str, state: _RenderState) -> str:
    """Reserve a unique class name, suffixing 2, 3, ... on collision."""
    capped = _cap_class_name_base(base)
    name = capped
    if name in state.used_class_names:
        counter = state.next_class_counter.get(capped, 2)
        while f"{capped}{counter}" in state.used_class_names:
            counter += 1
        name = f"{capped}{counter}"
        state.next_class_counter[capped] = counter + 1
    state.used_class_names.add(name)
    return name


def _child_class_name(base: str, segment: str) -> str:
    """Append *segment* to a parent class-name base and cap the result."""
    return _cap_class_name_base(unicodedata.normalize("NFKC", f"{base}{segment}"))


def _is_attr_name(name: str) -> bool:
    """Whether a tool can be declared as async def <name> on Tools."""
    return _is_bare_identifier(name) and name not in _RESERVED and not name.startswith("_")


def _is_valid_field_name(name: str) -> bool:
    """Whether an object property is expressible as a class-syntax TypedDict field."""
    if not _is_bare_identifier(name) or name in _RESERVED:
        return False
    # A single-underscore name is a legal field; a non-dunder __token name would
    # be name-mangled inside class syntax and describe a different key.
    return not (name.startswith("__") and not name.endswith("__"))


# --------------------------------------------------------------------------
# Scalar and description rendering
# --------------------------------------------------------------------------


def _json_quote(text: str) -> str:
    """Quote *text* as a JSON/Python string literal, escaping lone surrogates."""
    quoted = json.dumps(text, ensure_ascii=False)
    return _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", quoted)


def _py_scalar(value: Any) -> str:
    """Render a scalar const/enum value as Python literal text."""
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, str):
        return _json_quote(value)
    if isinstance(value, int):
        # Python integers are arbitrary precision, so the digits are exact.
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if value is None:
        return "None"
    return str(value)


def _describe(node: Any) -> str | None:
    """Return the collapsed one-line description of a schema node, or None.

    Whitespace folds to single spaces; remaining unprintable control characters
    become \\xNN and unpaired surrogates \\uNNNN so the emitted text is always
    encodable and printable.  A description that collapses to nothing is
    absent, like a missing one.
    """
    if isinstance(node, Mapping):
        description = node.get("description")
    else:
        description = getattr(node, "description", None)
    if not isinstance(description, str):
        return None
    collapsed = _WHITESPACE.sub(" ", description)
    collapsed = _UNPRINTABLE.sub(lambda match: f"\\x{ord(match.group()):02x}", collapsed)
    collapsed = _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", collapsed)
    collapsed = collapsed.strip()
    return collapsed or None


def _doc_lines(description: Any, indent: int) -> list[str]:
    """Render a one-line docstring at *indent*, or no lines when absent."""
    collapsed = _describe({"description": description})
    if collapsed is None:
        return []
    escaped = collapsed.replace("\\", "\\\\").replace('"', '\\"')
    return [f'{_pad(indent)}"""{escaped}"""']


def _pad(indent: int) -> str:
    """Return the four-space prefix for an *indent*-deep line."""
    return "    " * indent


# --------------------------------------------------------------------------
# Type rendering
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _RenderState:
    """Mutable collectors threaded through one SDK render."""

    classes: list[str] = field(default_factory=list)
    used_class_names: set[str] = field(default_factory=set)
    next_class_counter: dict[str, int] = field(default_factory=dict)
    typing: set[str] = field(default_factory=set)


def _render_type_root(schema: Any, class_name: str, state: _RenderState) -> str:
    """Validate one schema tree and render its Python type expression.

    A malformed or unsupported schema degrades to Any instead of failing prompt
    assembly, matching DSH's context-free fallback.  Validation runs before
    anything is emitted, so a rejected tree contributes no classes.
    """
    try:
        _assert_supported_json_schema(schema)
    except _UnsupportedSchema:
        state.typing.add("Any")
        return "Any"
    try:
        return _render_validated(schema, class_name, state, 0)
    except Exception:  # noqa: BLE001 - a prompt renderer must never crash prompt assembly
        state.typing.add("Any")
        return "Any"


def _render_validated(schema: Any, class_name: str, state: _RenderState, list_depth: int) -> str:
    """Render one already-validated JSON-Schema node to Python type text."""
    if not isinstance(schema, dict):
        raise _UnsupportedSchema("schema node must be an object")
    one_of = schema.get("oneOf")
    if one_of is not None:
        rendered = [
            _render_validated(branch, _child_class_name(class_name, str(index + 1)), state, list_depth)
            for index, branch in enumerate(one_of)
        ]
        return " | ".join(rendered)
    node_type = schema.get("type")
    if node_type is None:
        state.typing.add("Any")
        return "Any"
    if node_type == "string":
        return _render_constrained_scalar(schema, "str", state)
    if node_type == "number":
        return _render_constrained_scalar(schema, "float", state)
    if node_type == "integer":
        return _render_constrained_scalar(schema, "int", state)
    if node_type == "boolean":
        return _render_constrained_scalar(schema, "bool", state)
    if node_type == "null":
        return "None"
    if node_type == "array":
        items = schema.get("items")
        if items is None:
            state.typing.add("Any")
            return "list[Any]"
        if list_depth >= MAX_LIST_NESTING:
            state.typing.add("Any")
            return "Any"
        return f"list[{_render_validated(items, class_name, state, list_depth + 1)}]"
    if node_type == "object":
        return _render_object(schema, class_name, state)
    raise _UnsupportedSchema(f"unsupported schema type: {node_type!r}")


def _render_constrained_scalar(schema: Mapping[str, Any], broad: str, state: _RenderState) -> str:
    """Render const/enum as Literal[...], falling back to *broad*."""
    if "const" in schema:
        state.typing.add("Literal")
        return f"Literal[{_py_scalar(schema['const'])}]"
    if "enum" in schema:
        state.typing.add("Literal")
        values = ", ".join(_py_scalar(value) for value in schema["enum"])
        return f"Literal[{values}]"
    return broad


def _render_object(schema: dict[str, Any], class_name: str, state: _RenderState) -> str:
    """Render an object node as a named TypedDict or a permissive dict."""
    properties = schema.get("properties")
    entries: list[tuple[str, Any]] = list(properties.items()) if isinstance(properties, dict) else []
    anonymous = not class_name
    if anonymous or not all(_is_valid_field_name(name) for name, _ in entries):
        state.typing.add("Any")
        return "dict[str, Any]"
    # An OPEN empty object is any dict; a CLOSED empty object declares an empty
    # TypedDict, because "no keys accepted" is information worth carrying.
    if not entries and schema.get("additionalProperties") is not False:
        state.typing.add("Any")
        return "dict[str, Any]"

    allocated = _allocate_class_name(class_name, state)
    state.typing.add("TypedDict")
    field_types = [
        _render_validated(child, _child_class_name(allocated, _camel_case(name)), state, 1)
        for name, child in entries
    ]
    required = set(schema.get("required") or ())
    lines = [f"class {allocated}(TypedDict):"]
    for (name, child_schema), field_type in zip(entries, field_types):
        description = _describe(child_schema)
        if description is not None:
            lines.append(f"{_pad(1)}# {description}")
        if name in required:
            lines.append(f"{_pad(1)}{name}: {field_type}")
        else:
            state.typing.add("NotRequired")
            lines.append(f"{_pad(1)}{name}: NotRequired[{field_type}]")
    if schema.get("additionalProperties") is not False:
        lines.append(f"{_pad(1)}# Additional keys beyond those declared are allowed.")
    if len(lines) == 1:
        lines.append(f"{_pad(1)}pass")
    state.classes.append("\n".join(lines))
    return allocated


# --------------------------------------------------------------------------
# Schema subset validation
# --------------------------------------------------------------------------


class _UnsupportedSchema(Exception):
    """A schema node falls outside the enforced JSON-Schema subset."""


def _assert_supported_json_schema(schema: Any) -> None:
    """Assert *schema* belongs to the supported subset.

    The subset is the one DSH enforces for tool schemas: any JSON root, an
    annotation-only schema for unconstrained JSON, one scalar type, object
    properties/required/boolean additionalProperties, array items, type-correct
    scalar enum/const, and exact oneOf with at least two branches.  Anything
    else is rejected so the renderer degrades the whole tree to Any instead of
    guessing.
    """
    _check_schema_node(schema, set())


def _check_schema_node(schema: Any, seen: set[int]) -> None:
    """Recursively validate one schema node, rejecting cycles on the path."""
    if not isinstance(schema, dict):
        raise _UnsupportedSchema("schema must be an object")
    marker = id(schema)
    if marker in seen:
        raise _UnsupportedSchema("schema is circular")
    seen.add(marker)
    try:
        for key in schema:
            if key in _CONSTRAINT_KEYWORDS or key in _ANNOTATION_KEYWORDS:
                continue
            raise _UnsupportedSchema(f"unsupported keyword: {key!r}")
        for key in ("description", "title"):
            if key in schema and not isinstance(schema[key], str):
                raise _UnsupportedSchema(f"{key} must be a string")

        has_type = "type" in schema
        has_one_of = "oneOf" in schema
        if has_type and has_one_of:
            raise _UnsupportedSchema("cannot declare both type and oneOf")
        if not has_type and not has_one_of:
            _reject_sibling_keywords(schema)
            return
        if has_one_of:
            one_of = schema["oneOf"]
            if not isinstance(one_of, list) or len(one_of) < 2:
                raise _UnsupportedSchema("oneOf must be an array of at least two schemas")
            _reject_sibling_keywords(schema)
            for branch in one_of:
                _check_schema_node(branch, seen)
            return

        node_type = schema["type"]
        if not isinstance(node_type, str) or node_type not in _SCHEMA_TYPES:
            raise _UnsupportedSchema(f"unsupported type: {node_type!r}")
        _check_keyword_placement(schema, node_type)
        if node_type == "object":
            _check_object_schema(schema, seen)
        elif node_type == "array":
            if "items" in schema:
                _check_schema_node(schema["items"], seen)
        else:
            _check_constraints(schema, node_type)
    finally:
        seen.discard(marker)


def _reject_sibling_keywords(schema: Mapping[str, Any]) -> None:
    """Reject structural keywords beside oneOf (annotations stay legal)."""
    for key in _ONE_OF_SIBLING_KEYWORDS:
        if key in schema:
            raise _UnsupportedSchema(f"{key} is not supported beside oneOf")


def _check_keyword_placement(schema: Mapping[str, Any], node_type: str) -> None:
    """Reject structural keywords used on the wrong schema type."""
    allowed: dict[str, frozenset[str]] = {
        "object": frozenset({"properties", "required", "additionalProperties"}),
        "array": frozenset({"items"}),
        "string": frozenset({"enum", "const"}),
        "number": frozenset({"enum", "const"}),
        "integer": frozenset({"enum", "const"}),
        "boolean": frozenset({"enum", "const"}),
        "null": frozenset({"enum", "const"}),
    }
    for key in _ONE_OF_SIBLING_KEYWORDS:
        if key in schema and key not in allowed[node_type]:
            raise _UnsupportedSchema(f"{key} is not supported on type {node_type!r}")


def _check_object_schema(schema: Mapping[str, Any], seen: set[int]) -> None:
    """Validate an object node's properties, required list and openness."""
    properties = schema.get("properties")
    if "properties" in schema:
        if not isinstance(properties, dict) or not all(isinstance(key, str) for key in properties):
            raise _UnsupportedSchema("properties must be an object of schemas")
        for child in properties.values():
            _check_schema_node(child, seen)
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(isinstance(entry, str) for entry in required):
            raise _UnsupportedSchema("required must be an array of strings")
        declared = properties if isinstance(properties, dict) else {}
        for entry in required:
            if entry not in declared:
                raise _UnsupportedSchema(f"required names {entry!r} which is not in properties")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise _UnsupportedSchema("additionalProperties must be a boolean")


def _check_constraints(schema: Mapping[str, Any], node_type: str) -> None:
    """Validate enum/const type-correctness for one scalar node."""
    enum_valid = False
    if "enum" in schema:
        enum = schema["enum"]
        if (
            not isinstance(enum, list)
            or not enum
            or not all(_scalar_matches(node_type, value) for value in enum)
        ):
            raise _UnsupportedSchema(f"enum must be a non-empty array of {node_type} values")
        enum_valid = True
    if "const" in schema:
        const = schema["const"]
        if not _scalar_matches(node_type, const):
            raise _UnsupportedSchema(f"const must be a {node_type} value")
        if enum_valid and const not in schema["enum"]:
            raise _UnsupportedSchema("const must be one of enum when both are declared")


def _scalar_matches(node_type: str, value: Any) -> bool:
    """Whether *value* is a finite JSON scalar of *node_type*."""
    if node_type == "string":
        return isinstance(value, str)
    if node_type == "boolean":
        return isinstance(value, bool)
    if node_type == "null":
        return value is None
    if node_type == "number":
        return _is_json_number(value)
    if node_type == "integer":
        return _is_json_number(value) and float(value).is_integer()
    return False


def _is_json_number(value: Any) -> bool:
    """Whether *value* is a finite JSON number, excluding negative zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return True
    return math.isfinite(value) and not (value == 0 and math.copysign(1.0, value) < 0)
