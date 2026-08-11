"""JSON Schema -> Gemini function-declaration conversion.

The SDK does not normalise hand-built function declarations — `t_json_schema()` is
literally `return origin` — so a bad schema is not rejected locally, it just 400s
server-side. These tests run against the 39 schemas actually captured from Silpo.
"""

import json
import pathlib

import pytest
from google.genai import types

from komora.core.llm.gemini.schema_map import (
    UnsupportedSchema,
    json_schema_to_gemini,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "mcp" / "tools.json"
TOOLS = json.loads(FIXTURE.read_text(encoding="utf-8"))
INPUT_SCHEMAS = [(t["name"], t["inputSchema"]) for t in TOOLS if t.get("inputSchema")]


class TestAgainstRealSilpoSchemas:
    def test_fixture_is_present_and_substantial(self) -> None:
        assert len(INPUT_SCHEMAS) >= 30, "captured tool fixtures missing — rerun verify_mcp.py"

    @pytest.mark.parametrize("name,schema", INPUT_SCHEMAS, ids=[n for n, _ in INPUT_SCHEMAS])
    def test_every_tool_converts(self, name: str, schema: dict) -> None:
        json_schema_to_gemini(schema)

    @pytest.mark.parametrize("name,schema", INPUT_SCHEMAS, ids=[n for n, _ in INPUT_SCHEMAS])
    def test_output_is_accepted_by_the_gemini_sdk(self, name: str, schema: dict) -> None:
        """The real contract: would the SDK accept this as a declaration's parameters?"""
        converted = json_schema_to_gemini(schema)
        declaration = types.FunctionDeclaration(
            name=name, description="x", parameters=types.Schema(**converted)
        )
        assert declaration.parameters is not None

    @pytest.mark.parametrize("name,schema", INPUT_SCHEMAS, ids=[n for n, _ in INPUT_SCHEMAS])
    def test_no_rejected_keywords_survive(self, name: str, schema: dict) -> None:
        banned = {
            "$schema",
            "$ref",
            "$defs",
            "definitions",
            "oneOf",
            "allOf",
            "propertyNames",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "const",
            "additionalProperties",
            "examples",
        }

        def scan(node: object, path: str = "") -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    assert key not in banned, f"{name}: {key!r} survived at {path}"
                    scan(value, f"{path}/{key}")
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    scan(item, f"{path}[{i}]")

        scan(json_schema_to_gemini(schema))

    def test_a_known_tool_keeps_its_shape(self) -> None:
        """Spot-check that conversion preserves meaning, not just legality."""
        schema = dict(INPUT_SCHEMAS_BY_NAME["silpo_find_products_batch"])
        out = json_schema_to_gemini(schema)
        assert set(out["required"]) == {
            "branchId",
            "deliveryType",
            "timeslotStart",
            "timeslotEnd",
            "products",
        }
        assert out["properties"]["products"]["type"] == "array"
        assert out["properties"]["products"]["items"]["type"] == "string"
        assert len(out["properties"]["deliveryType"]["enum"]) > 5
        assert "description" in out["properties"]["branchId"]


INPUT_SCHEMAS_BY_NAME = dict(INPUT_SCHEMAS)


class TestNullableUnions:
    def test_anyof_with_null_collapses_to_nullable(self) -> None:
        """The dominant pattern in MCP schemas: optionality expressed as a union."""
        out = json_schema_to_gemini(
            {
                "type": "object",
                "properties": {
                    "q": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "d"}
                },
            }
        )
        field = out["properties"]["q"]
        assert field["type"] == "string"
        assert field["nullable"] is True
        assert field["description"] == "d"

    def test_nullable_object_keeps_its_properties(self) -> None:
        out = json_schema_to_gemini(
            {
                "anyOf": [
                    {"type": "object", "properties": {"a": {"type": "integer"}}},
                    {"type": "null"},
                ]
            }
        )
        assert out["type"] == "object"
        assert out["nullable"] is True
        assert out["properties"]["a"]["type"] == "integer"

    def test_genuine_union_degrades_to_string_and_says_so(self) -> None:
        """Gemini's subset cannot express a real union; losing it silently would be worse."""
        out = json_schema_to_gemini(
            {"anyOf": [{"type": "string"}, {"type": "integer"}], "description": "id"}
        )
        assert out["type"] == "string"
        assert "string" in out["description"] and "integer" in out["description"]


class TestUnsupportedKeywords:
    def test_schema_dialect_marker_is_dropped(self) -> None:
        assert "$schema" not in json_schema_to_gemini(
            {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}
        )

    def test_exclusive_minimum_is_dropped_but_noted(self) -> None:
        """types.Schema has no exclusiveMinimum; dropping it silently loses the rule."""
        out = json_schema_to_gemini({"type": "number", "exclusiveMinimum": 0})
        assert "exclusiveMinimum" not in out
        assert "> 0" in out.get("description", "")

    def test_additional_properties_is_dropped(self) -> None:
        """`true` is rejected outright by the Gemini Developer API."""
        assert "additionalProperties" not in json_schema_to_gemini(
            {"type": "object", "additionalProperties": True}
        )

    @pytest.mark.parametrize("keyword", ["oneOf", "allOf"])
    def test_unrepresentable_combinators_raise(self, keyword: str) -> None:
        """These pass client-side validation and then fail server-side — fail loudly here."""
        with pytest.raises(UnsupportedSchema, match=keyword):
            json_schema_to_gemini({keyword: [{"type": "string"}, {"type": "integer"}]})


class TestConst:
    def test_string_const_becomes_a_single_value_enum(self) -> None:
        out = json_schema_to_gemini({"type": "string", "const": "fixed"})
        assert out["enum"] == ["fixed"]
        assert "const" not in out

    def test_non_string_const_raises(self) -> None:
        """The SDK's own converter rejects these: 'Literal values must be strings'."""
        with pytest.raises(UnsupportedSchema, match="const"):
            json_schema_to_gemini({"type": "integer", "const": 7})


class TestRefs:
    def test_local_ref_is_inlined(self) -> None:
        out = json_schema_to_gemini(
            {
                "type": "object",
                "properties": {"line": {"$ref": "#/$defs/Line"}},
                "$defs": {"Line": {"type": "object", "properties": {"n": {"type": "integer"}}}},
            }
        )
        assert "$defs" not in out
        assert out["properties"]["line"]["properties"]["n"]["type"] == "integer"

    def test_recursive_ref_raises_rather_than_looping(self) -> None:
        with pytest.raises(UnsupportedSchema, match="recursive"):
            json_schema_to_gemini({"type": "object", "properties": {"self": {"$ref": "#"}}})

    def test_unknown_ref_raises(self) -> None:
        with pytest.raises(UnsupportedSchema, match="Nope"):
            json_schema_to_gemini({"$ref": "#/$defs/Nope", "$defs": {}})


class TestFormats:
    def test_unsupported_format_is_dropped_but_described(self) -> None:
        """Gemini accepts few string formats; `uuid` (15 uses in Silpo) is not one.
        Dropping it silently would lose a real hint to the model."""
        out = json_schema_to_gemini({"type": "string", "format": "uuid", "description": "Cart"})
        assert "format" not in out
        assert "uuid" in out["description"]

    def test_supported_format_survives(self) -> None:
        assert json_schema_to_gemini({"type": "string", "format": "date-time"})["format"] == (
            "date-time"
        )


class TestPreservation:
    def test_core_keywords_pass_through(self) -> None:
        source = {
            "type": "object",
            "description": "d",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 9, "pattern": "^a"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 30,
                },
                "size": {"type": "integer", "minimum": 1, "maximum": 100},
                "kind": {"type": "string", "enum": ["a", "b"]},
            },
            "required": ["name"],
        }
        out = json_schema_to_gemini(source)
        assert out["required"] == ["name"]
        assert out["properties"]["tags"]["maxItems"] == 30
        assert out["properties"]["size"]["maximum"] == 100
        assert out["properties"]["kind"]["enum"] == ["a", "b"]
        assert out["properties"]["name"]["pattern"] == "^a"

    def test_input_is_not_mutated(self) -> None:
        source = {
            "$schema": "x",
            "type": "object",
            "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        }
        before = json.dumps(source, sort_keys=True)
        json_schema_to_gemini(source)
        assert json.dumps(source, sort_keys=True) == before

    def test_deeply_nested_schemas_survive(self) -> None:
        """Silpo's schemas nest to depth 11."""
        node: dict = {"type": "string"}
        for _ in range(12):
            node = {"type": "object", "properties": {"child": node}}
        out = json_schema_to_gemini(node)
        for _ in range(12):
            out = out["properties"]["child"]
        assert out["type"] == "string"
