"""Tests for the PTC Python SDK renderer.

The renderer produces prompt text, so most assertions are on exact emitted
strings: class declaration order, annotations, escaping and the fixed usage
preamble all have to stay byte-stable for prompt caching.  The rich read_file
case is pinned against the output of DSH's own renderToolsSdkPy (checked with
Node 26 against lib/types/py-types.js), which makes this suite a parity test as
well as a regression test.
"""

from __future__ import annotations

import ast

import pytest

from deeppyro.core.protocols import ToolSchema
from deeppyro.ptc.sdk import MAX_CLASS_NAME_BASE, MAX_LIST_NESTING, SDK_INSTRUCTIONS, render_tools_sdk

READ_FILE_TOOL = ToolSchema.model_validate(
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file.\n\nReturns its content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path"},
                "offset": {"type": "integer", "description": "First line (1-based)"},
                "limit": {"type": "integer"},
                "encoding": {"type": "string", "enum": ["utf-8", "utf-16"]},
                "options": {
                    "type": "object",
                    "properties": {
                        "follow": {"type": "boolean"},
                        "maxBytes": {"type": "integer", "const": 1024},
                    },
                    "required": ["follow"],
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "matrix": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "integer"}},
                },
                "anyValue": {},
                "maybe": {"oneOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
            },
            "required": ["path", "options"],
        },
        "output": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "bytes": {"type": "integer"},
                "truncated": {"type": "boolean", "description": "Whether output was cut"},
            },
            "required": ["content", "bytes"],
            "additionalProperties": False,
        },
    }
)

# Generated once from DSH's renderToolsSdkPy for READ_FILE_TOOL; see module docstring.
_GOLDEN_READ_FILE = '## Writing code for run_code\n\n`run_code` takes two required arguments: `code`, the program to execute, and `description`, a short summary of what the program does. At run time exactly two of the names declared below are bound: `tools` and `ToolCallError`. Everything else is a STATIC STUB describing argument and return types — in particular the `TypedDict` classes do NOT exist at run time, so build arguments as plain `dict`/`list` JSON values: `await tools.name({"field": 1})`, never `FooArgs(field=1)`, which raises `NameError`. Inside the program:\n\n- Call tools as `await tools.name(args)` — subscript access for exotic, reserved, or underscore-leading names: `await tools["my-tool"](args)`. Every call resolves to the tool\'s typed canonical JSON value (each method\'s return type below). Tool arguments must be lossless JSON.\n- A FAILED tool call raises `ToolCallError`, whose `toolName` identifies the failed tool and whose message is human-readable — wrap in `try/except` to handle and continue.\n- Independent read-only calls MAY overlap under `asyncio.gather` (safe calls run concurrently; mutating calls run alone, in submission order). Sequence dependent work with `await`.\n- Only values emitted as program output enter the conversation. A successful tool result containing an image is attached after the run so you can inspect it on the next step; every other intermediate result stays out of the conversation, so extract just what you need.\n\nThe available tools:\n\n```python\nfrom typing import Any, Literal, NotRequired, Protocol, TypedDict\n\nclass ToolCallError(Exception):\n    toolName: str\n\nclass ReadFileArgsOptions(TypedDict):\n    follow: bool\n    maxBytes: NotRequired[Literal[1024]]\n    # Additional keys beyond those declared are allowed.\n\nclass ReadFileArgs(TypedDict):\n    # Absolute path\n    path: str\n    # First line (1-based)\n    offset: NotRequired[int]\n    limit: NotRequired[int]\n    encoding: NotRequired[Literal["utf-8", "utf-16"]]\n    options: ReadFileArgsOptions\n    tags: NotRequired[list[str]]\n    matrix: NotRequired[list[list[int]]]\n    anyValue: NotRequired[Any]\n    maybe: NotRequired[str | int | None]\n    # Additional keys beyond those declared are allowed.\n\nclass ReadFileOutput(TypedDict):\n    content: str\n    bytes: int\n    # Whether output was cut\n    truncated: NotRequired[bool]\n\nclass Tools(Protocol):\n    async def read_file(self, args: ReadFileArgs) -> ReadFileOutput:\n        """Read a UTF-8 text file. Returns its content."""\n\ntools: Tools\n```'


def _tool(
    name: str,
    parameters: dict | None = None,
    *,
    description: str = "",
    output: dict | None = None,
) -> ToolSchema:
    """Build a ToolSchema with concise defaults for a test case."""
    return ToolSchema(
        name=name,
        description=description,
        parameters=parameters if parameters is not None else {},
        output=output,
    )


def _block(text: str) -> str:
    """Return the fenced python block of a rendered SDK."""
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    return text[start : text.rindex("\n```")]


def _exec_block(text: str) -> dict:
    """Execute the fenced block and return its namespace, proving it is valid Python."""
    namespace: dict = {}
    exec(compile(_block(text), "<tools-sdk>", "exec"), namespace)  # noqa: S102 - generated stub text
    return namespace


def test_empty_tool_list_renders_preamble_and_pass_body() -> None:
    expected = (
        SDK_INSTRUCTIONS + "\n\n```python\n"
        "from typing import Protocol\n\n"
        "class ToolCallError(Exception):\n"
        "    toolName: str\n\n"
        "class Tools(Protocol):\n"
        "    pass\n\n"
        "tools: Tools\n"
        "```"
    )
    assert render_tools_sdk([]) == expected


def test_preamble_text_is_stable() -> None:
    assert SDK_INSTRUCTIONS.startswith("## Writing code for run_code\n\n")
    assert SDK_INSTRUCTIONS.endswith("\n\nThe available tools:")
    assert "STATIC STUB" in SDK_INSTRUCTIONS
    assert "raises `NameError`" in SDK_INSTRUCTIONS
    assert SDK_INSTRUCTIONS == SDK_INSTRUCTIONS.rstrip()


def test_read_file_rich_schema_is_byte_identical_to_dsh() -> None:
    assert render_tools_sdk([READ_FILE_TOOL]) == _GOLDEN_READ_FILE


def test_read_file_block_is_valid_python() -> None:
    namespace = _exec_block(render_tools_sdk([READ_FILE_TOOL]))
    assert namespace["Tools"].__name__ == "Tools"
    assert issubclass(namespace["ToolCallError"], Exception)
    assert {"ReadFileArgs", "ReadFileOutput", "ReadFileArgsOptions"} <= set(namespace)


def test_tools_are_sorted_by_name_not_input_order() -> None:
    tools = [_tool("write"), _tool("apple"), _tool("read")]
    block = _block(render_tools_sdk(tools))
    assert block.index("async def apple") < block.index("async def read") < block.index("async def write")


def test_sort_uses_utf16_code_unit_order() -> None:
    # U+1F600 starts with UTF-16 unit D83D, which sorts before BMP U+E000.
    block = _block(render_tools_sdk([_tool("\ue000"), _tool("\U0001f600")]))
    assert block.index(f'"{chr(0x1F600)}"') < block.index(f'"{chr(0xE000)}"')


def test_simple_annotations_and_imports() -> None:
    tool = _tool(
        "convert",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["path", "count"],
        },
        output={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    )
    block = _block(render_tools_sdk([tool]))
    assert block.startswith("from typing import Protocol, TypedDict\n")
    assert "class ConvertArgs(TypedDict):\n    path: str\n    count: int\n" in block
    assert "class ConvertOutput(TypedDict):\n    ok: bool\n" in block
    assert "async def convert(self, args: ConvertArgs) -> ConvertOutput: ..." in block


def test_optional_fields_use_not_required_and_open_object_comment() -> None:
    tool = _tool(
        "t",
        {
            "type": "object",
            "properties": {"a": {"type": "string", "description": "First"}, "b": {}},
            "required": ["a"],
        },
    )
    block = _block(render_tools_sdk([tool]))
    assert "from typing import Any, NotRequired, Protocol, TypedDict" in block
    assert (
        "class TArgs(TypedDict):\n"
        "    # First\n"
        "    a: str\n"
        "    b: NotRequired[Any]\n"
        "    # Additional keys beyond those declared are allowed.\n" in block
    )


def test_nested_objects_are_declared_before_their_parent() -> None:
    tool = _tool(
        "outer",
        {
            "type": "object",
            "properties": {
                "inner": {
                    "type": "object",
                    "properties": {
                        "leaf": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        "name": {"type": "string"},
                    },
                    "required": ["name"],
                }
            },
        },
    )
    block = _block(render_tools_sdk([tool]))
    assert (
        block.index("class OuterArgsInnerLeaf(TypedDict):")
        < block.index("class OuterArgsInner(TypedDict):")
        < block.index("class OuterArgs(TypedDict):")
    )
    assert "    inner: NotRequired[OuterArgsInner]\n" in block


def test_class_name_collisions_get_numeric_suffixes() -> None:
    block = _block(
        render_tools_sdk(
            [
                _tool("read_file", {"type": "object", "properties": {}, "additionalProperties": False}),
                _tool("readFile", {"type": "object", "properties": {}, "additionalProperties": False}),
            ]
        )
    )
    assert "class ReadFileArgs(TypedDict):\n    pass\n" in block
    assert "class ReadFileArgs2(TypedDict):\n    pass\n" in block


def test_class_name_base_is_capped() -> None:
    block = _block(
        render_tools_sdk(
            [_tool("a" * 300, {"type": "object", "properties": {}, "additionalProperties": False})]
        )
    )
    class_line = next(line for line in block.splitlines() if line.startswith("class A"))
    class_name = class_line.split("(")[0].removeprefix("class ")
    assert len(class_name) == MAX_CLASS_NAME_BASE


def test_enum_and_const_render_literal() -> None:
    tool = _tool(
        "theme",
        {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["dark", "light"]},
                "count": {"type": "integer", "const": 3},
                "ratio": {"type": "number", "const": 1.0},
                "flag": {"type": "boolean", "const": True},
            },
            "required": ["mode", "count", "ratio", "flag"],
        },
    )
    block = _block(render_tools_sdk([tool]))
    assert "from typing import Any, Literal, Protocol, TypedDict" in block
    assert '    mode: Literal["dark", "light"]\n' in block
    assert "    count: Literal[3]\n" in block
    assert "    ratio: Literal[1]\n" in block
    assert "    flag: Literal[True]\n" in block


def test_one_of_renders_union_and_names_branches() -> None:
    tool = _tool(
        "update",
        {
            "type": "object",
            "properties": {
                "target": {
                    "oneOf": [
                        {"type": "object", "properties": {"file": {"type": "string"}}},
                        {"type": "object", "properties": {"url": {"type": "string"}}},
                    ]
                }
            },
            "required": ["target"],
        },
    )
    block = _block(render_tools_sdk([tool]))
    assert "    target: UpdateArgsTarget1 | UpdateArgsTarget2\n" in block
    assert "class UpdateArgsTarget1(TypedDict):\n    file: NotRequired[str]\n" in block
    assert "class UpdateArgsTarget2(TypedDict):\n    url: NotRequired[str]\n" in block


def test_missing_output_renders_any() -> None:
    parameters = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    block = _block(render_tools_sdk([_tool("t", parameters)]))
    assert "from typing import Any, Protocol, TypedDict" in block
    assert "async def t(self, args: TArgs) -> Any: ..." in block


def test_null_output_type_renders_none() -> None:
    parameters = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    block = _block(render_tools_sdk([_tool("t", parameters, output={"type": "null"})]))
    assert "async def t(self, args: TArgs) -> None: ..." in block


def test_description_becomes_docstring_or_trailing_ellipsis() -> None:
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    with_doc = _block(render_tools_sdk([_tool("t", parameters, description="Do a thing.")]))
    assert 'async def t(self, args: TArgs) -> Any:\n        """Do a thing."""' in with_doc
    without_doc = _block(render_tools_sdk([_tool("t", parameters)]))
    assert "async def t(self, args: TArgs) -> Any: ..." in without_doc


def test_description_whitespace_and_escapes_are_normalized() -> None:
    tool = _tool(
        "t",
        {"type": "object", "properties": {}},
        description='line1\n\n  line2 \\ "quoted"\n',
    )
    block = _block(render_tools_sdk([tool]))
    assert '"""line1 line2 \\\\ \\"quoted\\""""' in block


def test_unprintable_and_surrogate_descriptions_are_escaped() -> None:
    tool = _tool(
        "t",
        {
            "type": "object",
            "properties": {
                "a": {"type": "string", "description": "x\x00y\x01z"},
                "b": {"type": "string", "description": "bad \ud800 pair"},
            },
            "required": ["a", "b"],
        },
    )
    block = _block(render_tools_sdk([tool]))
    assert "    # x\\x00y\\x01z\n" in block
    assert "    # bad \\ud800 pair\n" in block


def test_exotic_tool_name_is_a_subscript_comment() -> None:
    block = _block(
        render_tools_sdk([_tool("my-tool", {"type": "object", "properties": {}}, description="Exotic tool")])
    )
    assert '    # tools["my-tool"](args: dict[str, Any]) -> Any\n    #   Exotic tool\n' in block
    # Only comments, so the protocol body needs an explicit pass.
    assert 'class Tools(Protocol):\n    pass\n    # tools["my-tool"]' in block


def test_reserved_and_underscore_names_are_comments() -> None:
    parameters = {"type": "object", "properties": {}}
    block = _block(render_tools_sdk([_tool("class", parameters), _tool("_private", parameters)]))
    assert '    # tools["class"](args: dict[str, Any]) -> Any' in block
    assert '    # tools["_private"](args: dict[str, Any]) -> Any' in block
    assert "async def class" not in block


def test_illegal_field_names_degrade_the_object_to_a_dict() -> None:
    for field in ("weird field", "class", "__mangled"):
        tool = _tool(
            "t",
            {"type": "object", "properties": {field: {"type": "string"}}, "required": [field]},
        )
        block = _block(render_tools_sdk([tool]))
        assert "async def t(self, args: dict[str, Any]) -> Any: ..." in block
        assert "class TArgs(TypedDict)" not in block


def test_empty_closed_object_becomes_an_empty_typed_dict() -> None:
    block = _block(
        render_tools_sdk([_tool("t", {"type": "object", "properties": {}, "additionalProperties": False})])
    )
    assert "class TArgs(TypedDict):\n    pass\n" in block


def test_empty_open_object_becomes_a_dict() -> None:
    block = _block(render_tools_sdk([_tool("t", {"type": "object", "properties": {}})]))
    assert "async def t(self, args: dict[str, Any]) -> Any: ..." in block


def test_closed_output_object_omits_the_additional_keys_comment() -> None:
    block = _block(
        render_tools_sdk(
            [
                _tool(
                    "t",
                    {"type": "object", "properties": {}},
                    output={
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                )
            ]
        )
    )
    assert "class TOutput(TypedDict):\n    ok: bool\n" in block
    assert "Additional keys" not in block


@pytest.mark.parametrize(
    "parameters",
    [
        {"type": "string", "enum": [1]},  # enum value does not match type
        {"type": "object", "required": ["missing"]},  # required not declared
        {"type": "string", "properties": {}},  # keyword on wrong type
        {"type": "object", "oneOf": [{"type": "string"}]},  # fewer than two branches
        {"type": "array", "items": {"type": "string", "minLength": 1}},  # unsupported keyword
        {"type": "object", "properties": {}, "patternProperties": {}},  # unsupported keyword
        {"type": "number", "const": "x"},  # const does not match type
    ],
)
def test_unsupported_schema_degrades_to_any(parameters: dict) -> None:
    block = _block(render_tools_sdk([_tool("t", parameters)]))
    assert "async def t(self, args: Any) -> Any: ..." in block
    assert "TypedDict" not in block.split("class Tools(Protocol):")[0]


def test_circular_schema_degrades_to_any() -> None:
    parameters: dict = {"type": "object", "properties": {}}
    parameters["properties"]["nested"] = parameters
    block = _block(render_tools_sdk([_tool("t", parameters)]))
    assert "async def t(self, args: Any) -> Any: ..." in block


def test_deep_array_nesting_stops_at_the_limit() -> None:
    schema: dict = {"type": "string"}
    for _ in range(MAX_LIST_NESTING + 5):
        schema = {"type": "array", "items": schema}
    block = _block(render_tools_sdk([_tool("t", {"type": "object", "properties": {"deep": schema}})]))
    line = next(line for line in block.splitlines() if line.startswith("    deep: "))
    annotation = line.removeprefix("    deep: NotRequired[").removesuffix("]")
    depth = MAX_LIST_NESTING - 1
    assert annotation == "list[" * depth + "Any" + "]" * depth


def test_arrays_without_items_use_list_any() -> None:
    block = _block(render_tools_sdk([_tool("t", {"type": "object", "properties": {"a": {"type": "array"}}})]))
    assert "    a: NotRequired[list[Any]]\n" in block


def test_rendering_is_deterministic_and_input_order_independent() -> None:
    tools = [
        _tool("b", {"type": "object", "properties": {"x": {"type": "string"}}}),
        _tool("a", {"type": "object", "properties": {"y": {"type": "integer"}}}),
        _tool("c"),
    ]
    first = render_tools_sdk(tools)
    assert render_tools_sdk(tools) == first
    assert render_tools_sdk(list(reversed(tools))) == first


def test_accepts_any_sequence_and_returns_str() -> None:
    rendered = render_tools_sdk((_tool("a"),))
    assert isinstance(rendered, str)
    assert rendered.count("```") == 2


def test_exotic_only_tools_share_one_protocol_body() -> None:
    block = _block(render_tools_sdk([_tool("a-b"), _tool("c-d")]))
    assert block.count("    pass\n") == 1
    assert block.index('tools["a-b"]') < block.index('tools["c-d"]')


def test_output_block_is_parseable_for_a_mixed_schema_set() -> None:
    tools = [
        READ_FILE_TOOL,
        _tool("my-tool", {"type": "object", "properties": {"weird field": {"type": "string"}}}),
        _tool("empty_closed", {"type": "object", "properties": {}, "additionalProperties": False}),
        _tool("untyped", {"type": "object", "properties": {"x": {}}}, output={"type": "null"}),
    ]
    rendered = render_tools_sdk(tools)
    ast.parse(_block(rendered))
    assert rendered.endswith("\n```")
