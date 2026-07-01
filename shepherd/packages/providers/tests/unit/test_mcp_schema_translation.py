"""Spike 2, Step 3: Schema translation tests for MCP -> OpenAI function tool format.

Validates that ``mcp_tools_to_function_schemas`` correctly converts MCP Tool
objects into OpenAI-compatible function tool definitions, including JSON Schema
normalisation (``$ref`` resolution, unsupported field stripping, etc.).
"""

from __future__ import annotations

import pytest

try:
    from mcp.types import Tool
    from shepherd_providers.openai._mcp_stdio_bridge import mcp_tools_to_function_schemas

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _MCP_AVAILABLE, reason="mcp package not installed")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str, schema: dict, description: str | None = None) -> Tool:
    return Tool(name=name, inputSchema=schema, description=description)


def _convert_one(tool: Tool, server: str = "srv") -> dict:
    """Convert a single tool and return the function dict."""
    results = mcp_tools_to_function_schemas([tool], server)
    assert len(results) == 1
    return results[0]


# ---------------------------------------------------------------------------
# Tool naming
# ---------------------------------------------------------------------------


class TestToolNaming:
    def test_basic_naming(self):
        tool = _make_tool("echo", {"type": "object", "properties": {}})
        result = _convert_one(tool, "myserver")
        assert result["type"] == "function"
        assert result["function"]["name"] == "mcp__myserver__echo"

    def test_naming_with_dashes(self):
        tool = _make_tool("get-weather", {"type": "object", "properties": {}})
        result = _convert_one(tool, "weather-api")
        assert result["function"]["name"] == "mcp__weather-api__get-weather"

    def test_description_included(self):
        tool = _make_tool("echo", {"type": "object", "properties": {}}, description="Echo back")
        result = _convert_one(tool)
        assert result["function"]["description"] == "Echo back"

    def test_description_omitted_when_none(self):
        tool = _make_tool("echo", {"type": "object", "properties": {}})
        result = _convert_one(tool)
        assert "description" not in result["function"]

    def test_multiple_tools(self):
        tools = [
            _make_tool("a", {"type": "object", "properties": {}}),
            _make_tool("b", {"type": "object", "properties": {}}),
            _make_tool("c", {"type": "object", "properties": {}}),
        ]
        results = mcp_tools_to_function_schemas(tools, "s")
        names = [r["function"]["name"] for r in results]
        assert names == ["mcp__s__a", "mcp__s__b", "mcp__s__c"]


# ---------------------------------------------------------------------------
# Simple schemas -- pass through without modification
# ---------------------------------------------------------------------------


class TestSimpleSchemas:
    def test_string_params(self):
        schema = {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
            "required": ["message"],
        }
        result = _convert_one(_make_tool("echo", schema))
        params = result["function"]["parameters"]
        assert params == schema

    def test_int_params(self):
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
            },
        }
        result = _convert_one(_make_tool("count", schema))
        assert result["function"]["parameters"] == schema

    def test_nested_object(self):
        schema = {
            "type": "object",
            "properties": {
                "address": {
                    "type": "object",
                    "properties": {
                        "street": {"type": "string"},
                        "city": {"type": "string"},
                    },
                },
            },
        }
        result = _convert_one(_make_tool("update", schema))
        assert result["function"]["parameters"] == schema

    def test_empty_schema(self):
        result = _convert_one(_make_tool("noop", {}))
        # Empty schema should pass through
        assert result["function"]["parameters"] == {}

    def test_additional_properties_only(self):
        schema = {
            "type": "object",
            "additionalProperties": {"type": "string"},
        }
        result = _convert_one(_make_tool("kv", schema))
        assert result["function"]["parameters"] == schema


# ---------------------------------------------------------------------------
# $ref / $defs resolution
# ---------------------------------------------------------------------------


class TestRefResolution:
    def test_simple_ref(self):
        schema = {
            "type": "object",
            "properties": {
                "item": {"$ref": "#/$defs/Item"},
            },
            "$defs": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                },
            },
        }
        result = _convert_one(_make_tool("add", schema))
        params = result["function"]["parameters"]
        # $defs should be removed
        assert "$defs" not in params
        # $ref should be resolved inline
        assert params["properties"]["item"] == {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }

    def test_nested_ref(self):
        """$ref pointing to a type that itself uses $ref."""
        schema = {
            "type": "object",
            "properties": {
                "tree": {"$ref": "#/$defs/Node"},
            },
            "$defs": {
                "Leaf": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                },
                "Node": {
                    "type": "object",
                    "properties": {
                        "child": {"$ref": "#/$defs/Leaf"},
                    },
                },
            },
        }
        result = _convert_one(_make_tool("tree", schema))
        params = result["function"]["parameters"]
        assert "$defs" not in params
        node = params["properties"]["tree"]
        assert node["properties"]["child"] == {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
        }

    def test_ref_in_array_items(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Entry"},
                },
            },
            "$defs": {
                "Entry": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                },
            },
        }
        result = _convert_one(_make_tool("list", schema))
        params = result["function"]["parameters"]
        assert "$defs" not in params
        assert params["properties"]["items"]["items"] == {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
        }


# ---------------------------------------------------------------------------
# anyOf handling
# ---------------------------------------------------------------------------


class TestAnyOf:
    def test_anyof_mixed_types(self):
        schema = {
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "integer"},
                    ],
                },
            },
        }
        result = _convert_one(_make_tool("mixed", schema))
        params = result["function"]["parameters"]
        assert params["properties"]["value"]["anyOf"] == [
            {"type": "string"},
            {"type": "integer"},
        ]

    def test_anyof_with_ref(self):
        schema = {
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": [
                        {"$ref": "#/$defs/Foo"},
                        {"type": "null"},
                    ],
                },
            },
            "$defs": {
                "Foo": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                },
            },
        }
        result = _convert_one(_make_tool("opt", schema))
        params = result["function"]["parameters"]
        assert "$defs" not in params
        any_of = params["properties"]["value"]["anyOf"]
        assert any_of[0] == {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
        }
        assert any_of[1] == {"type": "null"}


# ---------------------------------------------------------------------------
# Unsupported field stripping
# ---------------------------------------------------------------------------


class TestUnsupportedFieldStripping:
    def test_strip_default(self):
        schema = {
            "type": "object",
            "properties": {
                "x": {"type": "string", "default": "hello"},
            },
        }
        result = _convert_one(_make_tool("t", schema))
        assert "default" not in result["function"]["parameters"]["properties"]["x"]

    def test_strip_examples(self):
        schema = {
            "type": "object",
            "properties": {
                "x": {"type": "string", "examples": ["a", "b"]},
            },
        }
        result = _convert_one(_make_tool("t", schema))
        assert "examples" not in result["function"]["parameters"]["properties"]["x"]

    def test_strip_schema_and_id(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:example:test",
            "type": "object",
            "properties": {"x": {"type": "string"}},
        }
        result = _convert_one(_make_tool("t", schema))
        params = result["function"]["parameters"]
        assert "$schema" not in params
        assert "$id" not in params
        assert params["type"] == "object"

    def test_format_preserved(self):
        """``format`` is supported by OpenAI -- should NOT be stripped."""
        schema = {
            "type": "object",
            "properties": {
                "date": {"type": "string", "format": "date-time"},
            },
        }
        result = _convert_one(_make_tool("t", schema))
        assert result["function"]["parameters"]["properties"]["date"]["format"] == "date-time"

    def test_strip_defaults_in_nested(self):
        schema = {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {
                        "inner": {"type": "integer", "default": 42, "examples": [1, 2]},
                    },
                },
            },
        }
        result = _convert_one(_make_tool("t", schema))
        inner = result["function"]["parameters"]["properties"]["outer"]["properties"]["inner"]
        assert "default" not in inner
        assert "examples" not in inner
        assert inner["type"] == "integer"

    def test_pattern_properties_preserved(self):
        """``patternProperties`` should pass through."""
        schema = {
            "type": "object",
            "patternProperties": {
                "^x-": {"type": "string"},
            },
        }
        result = _convert_one(_make_tool("t", schema))
        assert "patternProperties" in result["function"]["parameters"]


# ---------------------------------------------------------------------------
# Deeply nested objects
# ---------------------------------------------------------------------------


class TestDeeplyNested:
    def test_three_levels_deep(self):
        schema = {
            "type": "object",
            "properties": {
                "a": {
                    "type": "object",
                    "properties": {
                        "b": {
                            "type": "object",
                            "properties": {
                                "c": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
        result = _convert_one(_make_tool("deep", schema))
        c = result["function"]["parameters"]["properties"]["a"]["properties"]["b"]["properties"]["c"]
        assert c == {"type": "string"}

    def test_deeply_nested_with_ref_and_defaults(self):
        """Combined: nested $ref + defaults that need stripping."""
        schema = {
            "type": "object",
            "properties": {
                "config": {"$ref": "#/$defs/Config"},
            },
            "$defs": {
                "Config": {
                    "type": "object",
                    "properties": {
                        "timeout": {"type": "integer", "default": 30},
                        "retries": {"type": "integer", "default": 3, "examples": [1, 5]},
                        "nested": {
                            "type": "object",
                            "properties": {
                                "flag": {"type": "boolean", "default": True},
                            },
                        },
                    },
                },
            },
        }
        result = _convert_one(_make_tool("cfg", schema))
        params = result["function"]["parameters"]
        assert "$defs" not in params
        config = params["properties"]["config"]
        assert config["properties"]["timeout"] == {"type": "integer"}
        assert config["properties"]["retries"] == {"type": "integer"}
        assert config["properties"]["nested"]["properties"]["flag"] == {"type": "boolean"}


class TestRecursiveSchemas:
    def test_self_referencing_schema_does_not_hang(self):
        """A tree node referencing itself should not cause an infinite loop."""
        schema = {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "children": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Node"},
                },
            },
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "children": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/Node"},
                        },
                    },
                },
            },
        }
        # Should complete without hanging, depth-limited to {"type": "object"}
        result = _convert_one(_make_tool("tree", schema))
        params = result["function"]["parameters"]
        assert "$defs" not in params
        assert "$ref" not in str(params)
        assert params["properties"]["value"] == {"type": "string"}

    def test_mutual_recursion_does_not_hang(self):
        """A -> B -> A cycle should be depth-limited."""
        schema = {
            "type": "object",
            "properties": {
                "a": {"$ref": "#/$defs/TypeA"},
            },
            "$defs": {
                "TypeA": {
                    "type": "object",
                    "properties": {"b": {"$ref": "#/$defs/TypeB"}},
                },
                "TypeB": {
                    "type": "object",
                    "properties": {"a": {"$ref": "#/$defs/TypeA"}},
                },
            },
        }
        result = _convert_one(_make_tool("mutual", schema))
        params = result["function"]["parameters"]
        assert "$ref" not in str(params)
