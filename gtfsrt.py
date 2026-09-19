"""Parse Clever Devices GTFS-Realtime protobuf-text (?debug) feeds."""

from __future__ import annotations

import re
from typing import Any


_SCALAR = re.compile(
    r"^(?P<key>[A-Za-z0-9_]+):\s*(?P<value>\".*?\"|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|[A-Za-z0-9_]+)\s*$"
)
_MESSAGE_OPEN = re.compile(r"^(?P<key>[A-Za-z0-9_]+)\s*\{\s*$")


def parse_protobuf_text(text: str) -> dict[str, Any]:
    tokens = _tokenize(text)
    body, _ = _parse_block(tokens, 0)
    return body


def _tokenize(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lines.append(line)
    return lines


def _parse_block(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        if line == "}":
            return result, index + 1
        opened = _MESSAGE_OPEN.match(line)
        if opened:
            child, index = _parse_block(lines, index + 1)
            _append(result, opened.group("key"), child)
            continue
        scalar = _SCALAR.match(line)
        if scalar:
            _append(result, scalar.group("key"), _coerce(scalar.group("value")))
            index += 1
            continue
        index += 1
    return result, index


def _append(target: dict[str, Any], key: str, value: Any) -> None:
    if key not in target:
        target[key] = value
        return
    existing = target[key]
    if isinstance(existing, list):
        existing.append(value)
    else:
        target[key] = [existing, value]


def _coerce(raw: str) -> Any:
    if raw.startswith('"') and raw.endswith('"'):
        return bytes(raw[1:-1], "utf-8").decode("unicode_escape")
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+(?:[eE][-+]?\d+)?", raw):
        return float(raw)
    return raw


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
