"""Generate the add-on's TypeScript types and constants from `protocol.py`.

Contract: docs/contracts/12-bridge-v2.md section 9. `python -m
chamberlain.connectors.thunderbird.typegen` rewrites
`addon/build/protocol.generated.ts`. The bundle can use `--stdout` instead.
Only the JSON Schema subset Pydantic emits for these models is handled.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Final

from pydantic import JsonValue
from pydantic.json_schema import models_json_schema

from chamberlain.connectors.thunderbird import frames, protocol
from chamberlain.schema.base import Model

_JSON_VALUE: Final = (
    "type JsonValue = string | number | boolean | null | JsonValue[]"
    " | { [key: string]: JsonValue };\n"
)
GENERATED: Final = Path(__file__).parent / "addon" / "build" / "protocol.generated.ts"
_HEADER: Final = (
    "// GENERATED from protocol.py by typegen.py. Do not edit; run\n"
    "//   python -m chamberlain.connectors.thunderbird.typegen\n"
)


def _models() -> list[type[Model]]:
    found = [
        value
        for value in vars(protocol).values()
        if isinstance(value, type) and issubclass(value, Model) and value is not Model
    ]
    return sorted(found, key=lambda model: model.__name__)


def definitions() -> dict[str, dict[str, JsonValue]]:
    """The `$defs` of every protocol model, with stable ordering."""
    _, top = models_json_schema(
        [(model, "validation") for model in _models()], ref_template="#/$defs/{model}"
    )
    defs = top["$defs"]
    return {name: defs[name] for name in sorted(defs)}


def _ts(node: JsonValue) -> str:
    if node is True or node == {}:
        return "JsonValue"
    if not isinstance(node, dict):
        raise TypeError(f"unsupported schema node {node!r}")
    if "$ref" in node:
        return str(node["$ref"]).rsplit("/", 1)[-1]
    if "const" in node:
        return json.dumps(node["const"])
    if "enum" in node:
        values = node["enum"]
        assert isinstance(values, list)
        return " | ".join(json.dumps(value) for value in values)
    for key in ("anyOf", "oneOf"):
        if key in node:
            options = node[key]
            assert isinstance(options, list)
            return " | ".join(dict.fromkeys(_ts(option) for option in options))
    kind = node.get("type")
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        item = _ts(node.get("items", {}))
        return f"({item})[]" if "|" in item else f"{item}[]"
    if kind == "object":
        properties = node.get("properties")
        if isinstance(properties, dict):
            return _object(node)
        extra = node.get("additionalProperties", True)
        return f"{{ [key: string]: {_ts(extra)} }}"
    raise TypeError(f"unsupported schema node {node!r}")


def _object(node: dict[str, JsonValue]) -> str:
    properties = node["properties"]
    assert isinstance(properties, dict)
    required = node.get("required", [])
    assert isinstance(required, list)
    lines: list[str] = []
    for name, child in properties.items():
        has_default = isinstance(child, dict) and child.get("default") is not None
        mark = "" if name in required or has_default else "?"
        lines.append(f"  {name}{mark}: {_ts(child)};")
    return "{\n" + "\n".join(lines) + "\n}"


def render() -> str:
    defs = definitions()
    out = [_HEADER]
    out.append(_JSON_VALUE)
    for name, node in defs.items():
        if name == "JsonValue":
            continue
        if node.get("type") == "object" and "properties" in node:
            out.append(f"interface {name} {_object(node)}\n")
        else:
            out.append(f"type {name} = {_ts(node)};\n")
    commands = {command.value: model for command, model in protocol.ARGS_MODELS.items()}
    results = {command.value: model for command, model in protocol.RESULT_MODELS.items()}
    out.append(
        "interface CommandArgs {\n"
        + "\n".join(f"  {cmd}: {model.__name__};" for cmd, model in commands.items())
        + "\n}\n"
    )
    out.append(
        "interface CommandResults {\n"
        + "\n".join(f"  {cmd}: {model.__name__};" for cmd, model in results.items())
        + "\n}\n"
    )
    out.append(f"const PROTOCOL_VERSION = {protocol.PROTOCOL_VERSION};")
    out.append(f"const CHUNK_BYTES = {frames.CHUNK_BYTES};")
    out.append(f"const TO_ADDON_LIMIT = {frames.TO_ADDON_LIMIT};")
    out.append(
        "const COMMANDS: readonly string[] = "
        + json.dumps([c.value for c in protocol.Command])
        + ";"
    )
    out.append(
        "const WRITE_COMMANDS: readonly string[] = "
        + json.dumps(sorted(c.value for c in protocol.WRITE_COMMANDS))
        + ";"
    )
    out.append("")
    return "\n".join(out)


def write_generated(output: Path = GENERATED) -> Path:
    """Write the generated types atomically for concurrent test and build processes."""
    output.parent.mkdir(parents=True, exist_ok=True)
    text = render()
    with tempfile.TemporaryDirectory(dir=output.parent) as pending:
        generated = Path(pending) / output.name
        generated.write_text(text, encoding="utf-8", newline="\n")
        generated.replace(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="print the types and constants")
    args = parser.parse_args()
    if args.stdout:
        sys.stdout.write(render())
    else:
        sys.stdout.write(f"wrote {write_generated()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
