"""Tool-call arguments, however a provider chose to encode them.

Shared rather than duplicated: the rule is subtle in the same way for everyone. A
provider may hand back a real object or a JSON string, and malformed output is a retry
for the caller — never a crash inside the client, where the agent loop cannot see it.
"""

import json
from typing import Any


def as_args(arguments: Any) -> dict[str, Any]:
    """Coerce whatever a provider returned into the args dict `ToolCall` declares.

    Ollama's `/api/chat` returns a real object; its OpenAI-compatible endpoint and
    OpenRouter both stringify. Anything else — null, a list, broken JSON — is an empty
    call, which the loop already handles as a model mistake.
    """
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
