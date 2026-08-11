"""Convert MCP tool JSON Schema into Gemini's function-declaration subset.

This is **Gemini-specific**. Ollama accepts near-raw JSON Schema and must not be routed
through here — the conversions below are lossy by necessity.

Why it has to exist: the SDK does not normalise hand-built function declarations
(`t_json_schema()` is literally `return origin`), so an unsupported keyword is not
rejected locally — the request just 400s server-side, with a message that rarely
points at the offending field.

Gemini's `types.Schema` is an OpenAPI 3.0 subset. It has no `oneOf`, `allOf`, `not`,
`const`, `$ref`, `exclusiveMinimum`, `propertyNames`, `prefixItems` or `multipleOf`.
Where a rule cannot be expressed, we move it into `description` rather than dropping
it silently — the model can still act on prose, and a lost constraint that nobody
noticed is the worse outcome.

Measured against the 39 schemas captured from Silpo (`tests/fixtures/mcp/tools.json`):
`anyOf: [X, null]` is by far the most common construct, `$schema` appears on every
tool, and `format: uuid` appears 15 times.
"""

from typing import Any, Final

__all__ = ["UnsupportedSchema", "json_schema_to_gemini"]


class UnsupportedSchema(ValueError):
    """A schema uses something Gemini cannot express and we refuse to guess at."""


# Keys `types.Schema` accepts, in the JSON spelling the SDK validates.
_PASSTHROUGH: Final = frozenset(
    {
        "type",
        "description",
        "title",
        "default",
        "enum",
        "format",
        "pattern",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "required",
        "nullable",
    }
)

# Silently dropping these is safe: they carry no constraint Gemini could honour.
_IGNORED: Final = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "additionalProperties",
        "examples",
        "propertyNames",
        "unevaluatedProperties",
        "readOnly",
        "writeOnly",
    }
)

# Gemini accepts very few string formats. `uuid` is not among them.
_SUPPORTED_FORMATS: Final = frozenset({"date-time", "enum"})

_NOT_REPRESENTABLE: Final = ("oneOf", "allOf", "not", "if", "then", "else")


def json_schema_to_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a JSON Schema object into Gemini's subset.

    Pure: the input is never mutated.

    Raises:
        UnsupportedSchema: for constructs with no faithful representation — an
            unresolvable or recursive `$ref`, a non-string `const`, or a
            `oneOf`/`allOf` combinator.
    """
    defs: dict[str, Any] = {}
    for key in ("$defs", "definitions"):
        found = schema.get(key)
        if isinstance(found, dict):
            defs.update(found)
    return _convert(schema, defs, seen_refs=frozenset())


def _convert(
    node: dict[str, Any], defs: dict[str, Any], seen_refs: frozenset[str]
) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise UnsupportedSchema(f"expected a schema object, got {type(node).__name__}")

    if "$ref" in node:
        return _convert(
            _resolve_ref(node["$ref"], defs, seen_refs), defs, seen_refs | {node["$ref"]}
        )

    for keyword in _NOT_REPRESENTABLE:
        if keyword in node:
            raise UnsupportedSchema(
                f"{keyword!r} has no representation in Gemini's schema subset; "
                f"rewrite it as anyOf or flatten it upstream"
            )

    if "anyOf" in node:
        return _convert_any_of(node, defs, seen_refs)

    out: dict[str, Any] = {}
    notes: list[str] = []

    for key, value in node.items():
        if key in _IGNORED or key in ("$defs", "definitions"):
            continue
        if key == "const":
            out.update(_convert_const(value))
        elif key == "format":
            if value in _SUPPORTED_FORMATS:
                out["format"] = value
            else:
                # Keep the hint where the model can still use it.
                notes.append(f"format: {value}")
        elif key in ("exclusiveMinimum", "exclusiveMaximum"):
            notes.append(f"{'>' if key == 'exclusiveMinimum' else '<'} {value}")
        elif key == "properties":
            out["properties"] = {
                name: _convert(sub, defs, seen_refs) for name, sub in value.items()
            }
        elif key == "items":
            out["items"] = _convert(value, defs, seen_refs)
        elif key in _PASSTHROUGH:
            out[key] = value
        # Anything else is unknown to both dialects; dropping it is the safe default.

    if notes:
        out["description"] = " ".join(
            filter(None, [out.get("description"), f"({'; '.join(notes)})"])
        )
    return out


def _convert_const(value: Any) -> dict[str, Any]:
    """`const` becomes a single-value enum, which Gemini only allows for strings.

    Mirrors the SDK's own converter, which raises "Literal values must be strings".
    """
    if not isinstance(value, str):
        raise UnsupportedSchema(
            f"const must be a string to become an enum; got {type(value).__name__} ({value!r})"
        )
    return {"enum": [value]}


def _convert_any_of(
    node: dict[str, Any], defs: dict[str, Any], seen_refs: frozenset[str]
) -> dict[str, Any]:
    """Collapse `anyOf`, which JSON Schema uses for two different jobs.

    `[X, {"type": "null"}]` is optionality — by far the common case in MCP schemas —
    and becomes X with `nullable: true`. A genuine union has no representation, so it
    degrades to a string whose description names the alternatives; that keeps the
    information visible to the model instead of discarding it.
    """
    members = [m for m in node["anyOf"] if isinstance(m, dict)]
    non_null = [m for m in members if m.get("type") != "null"]
    nullable = len(non_null) < len(members)
    siblings = {k: v for k, v in node.items() if k != "anyOf"}

    if len(non_null) == 1:
        converted = _convert({**non_null[0], **siblings}, defs, seen_refs)
        if nullable:
            converted["nullable"] = True
        return converted

    if not non_null:
        raise UnsupportedSchema("anyOf contains no non-null member")

    named = ", ".join(str(m.get("type", "object")) for m in non_null)
    degraded = _convert({**siblings, "type": "string"}, defs, seen_refs)
    degraded["description"] = " ".join(
        filter(None, [degraded.get("description"), f"(one of: {named})"])
    )
    if nullable:
        degraded["nullable"] = True
    return degraded


def _resolve_ref(ref: str, defs: dict[str, Any], seen_refs: frozenset[str]) -> dict[str, Any]:
    """Inline a local `$ref`. Gemini has no `$ref`, so it must be flattened."""
    if ref in seen_refs or ref == "#":
        raise UnsupportedSchema(f"recursive $ref {ref!r} cannot be inlined")
    name = ref.rsplit("/", 1)[-1]
    target = defs.get(name)
    if not isinstance(target, dict):
        raise UnsupportedSchema(f"cannot resolve $ref {ref!r} — no definition named {name!r}")
    return target
