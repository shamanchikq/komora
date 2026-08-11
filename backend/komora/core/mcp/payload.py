"""Reading what an MCP tool actually returned.

Two traps, both of which produced wrong answers on the first live run:

* the payload hides behind snake_case attributes, while the wire format and the docs
  use camelCase — `getattr(result, "structuredContent", None)` is `None` forever;
* a failure arrives as an ordinary **string**, so a truthiness check reports it as a
  success. `verify_mcp.py` once printed "8 passed, 0 failed" while two calls had
  returned `-32602` validation errors.
"""

import contextlib
import json
from typing import Any


def unwrap(result: Any) -> Any:
    """Pull the payload out of an MCP CallToolResult.

    Attributes are snake_case; the camelCase names are pydantic aliases only.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(text)
            return text
    return None


def error_of(payload: Any) -> str | None:
    """Return the error message if this payload is a failure, else None.

    Neither form raises, and neither is falsy — classifying explicitly is the only way
    to tell them apart from a result.
    """
    if isinstance(payload, str) and payload.lstrip().startswith("MCP error"):
        return payload.strip()
    if isinstance(payload, dict) and payload.get("success") is False:
        return json.dumps(payload, ensure_ascii=False)[:300]
    return None


def as_dict(payload: Any) -> dict[str, Any]:
    """Normalise a successful payload to a mapping.

    A tool that returns a bare list or scalar is wrapped rather than dropped, so a
    caller never has to guard the type before reading a key.
    """
    if isinstance(payload, dict):
        return payload
    return {"result": payload}


def root_causes(exc: BaseException) -> list[BaseException]:
    """Flatten nested ExceptionGroups.

    The MCP transport runs on nested task groups, so one failure otherwise surfaces as
    several stacked tracebacks with the real cause buried inside.
    """
    if isinstance(exc, BaseExceptionGroup):
        return [cause for sub in exc.exceptions for cause in root_causes(sub)]
    return [exc]
