import json
from typing import Dict, cast


def decode(value: str) -> Dict[str, object]:
    """Reject non-object protocol messages before accessing their fields."""
    result = cast(object, json.loads(value))
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object.')
    return cast(Dict[str, object], result)


def string(message: Dict[str, object], key: str) -> str:
    value = message[key]
    if not isinstance(value, str):
        raise ValueError(f'{key} must be a string.')
    return value
