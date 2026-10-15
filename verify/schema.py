"""Validation JSON par le code : json.loads puis schéma.

Sous-ensemble volontairement réduit de JSON Schema, sans dépendance :
type (object, array, string, integer, number, boolean, null, ou liste de types),
properties, required, additionalProperties (booléen), items, minItems,
maxItems, enum, minLength, maxLength, minimum, maximum.
Un mot-clé non pris en charge lève une erreur plutôt que d'être ignoré.
"""

from __future__ import annotations

import json
from typing import Any

SUPPORTED = {
    "type", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "enum", "minLength", "maxLength", "minimum", "maximum",
    "description", "title",
}

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class SchemaError(ValueError):
    """Le document ne respecte pas le schéma (ou n'est pas du JSON)."""


def parse_json(text: str, schema: dict | None = None) -> Any:
    """json.loads strict, puis validation. Tolère seulement une clôture ```json ... ```."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1])
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"JSON invalide : {exc}") from None
    if schema is not None:
        validate(value, schema)
    return value


def validate(value: Any, schema: dict, path: str = "$") -> None:
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise ValueError(f"mots-clés de schéma non pris en charge à {path} : {sorted(unknown)}")

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](value) for t in types):
            raise SchemaError(f"{path} : type attendu {'/'.join(types)}, reçu {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path} : valeur {value!r} hors de {schema['enum']!r}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaError(f"{path} : chaîne trop courte (min {schema['minLength']})")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(f"{path} : chaîne trop longue (max {schema['maxLength']})")

    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path} : {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path} : {value} > maximum {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaError(f"{path} : {len(value)} éléments, minimum {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path} : {len(value)} éléments, maximum {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                validate(item, schema["items"], f"{path}[{i}]")

    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path} : champ obligatoire manquant « {key} »")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise SchemaError(f"{path} : champs non autorisés {sorted(extra)}")
        for key, sub in props.items():
            if key in value:
                validate(value[key], sub, f"{path}.{key}")
