"""A small JSON Schema check for structured model output (v1.6.1, design note MP-04, P9).

E2A validates every structured result against the schema the agent asked for, whichever
provider produced it (native constrained output is a provider feature; checking the result is
E2A's rigor). Core depends on pydantic only, so this checks the keywords structured-output
schemas use in practice: type, enum, const, properties, required, additionalProperties,
items, minItems, maxItems, minimum, maximum, exclusiveMinimum, exclusiveMaximum, minLength,
maxLength, pattern, anyOf, oneOf, allOf. Unknown keywords are ignored, as JSON Schema does.
"""
import re
from typing import Any, Dict, List

__all__ = ['schema_errors']

_TYPES = {
    'object': lambda v: isinstance(v, dict),
    'array': lambda v: isinstance(v, list),
    'string': lambda v: isinstance(v, str),
    'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
    'number': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    'boolean': lambda v: isinstance(v, bool),
    'null': lambda v: v is None,
}


def schema_errors(schema: Dict[str, Any], value: Any, path: str = '$', limit: int = 20) -> List[str]:
    """The ways `value` violates `schema` (empty when it conforms), at most `limit`."""
    errors: List[str] = []
    _check(schema or {}, value, path, errors, limit)
    return errors[:limit]


def _check(schema: Dict[str, Any], value: Any, path: str, errors: List[str], limit: int) -> None:
    if len(errors) >= limit or not isinstance(schema, dict):
        return
    kind = schema.get('type')
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_TYPES.get(k, lambda v: True)(value) for k in kinds):
            errors.append(f'{path}: expected {"/".join(kinds)}, got {type(value).__name__}')
            return
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{path}: {value!r} is not one of {schema["enum"]!r}')
    if 'const' in schema and value != schema['const']:
        errors.append(f'{path}: must be {schema["const"]!r}')
    if isinstance(value, dict):
        props = schema.get('properties') or {}
        for name in schema.get('required') or []:
            if name not in value:
                errors.append(f'{path}: missing required property {name!r}')
        for name, sub in props.items():
            if name in value:
                _check(sub, value[name], f'{path}.{name}', errors, limit)
        extra = schema.get('additionalProperties', True)
        for name in value:
            if name in props:
                continue
            if extra is False:
                errors.append(f'{path}: unexpected property {name!r}')
            elif isinstance(extra, dict):
                _check(extra, value[name], f'{path}.{name}', errors, limit)
    if isinstance(value, list):
        if 'minItems' in schema and len(value) < schema['minItems']:
            errors.append(f'{path}: fewer than {schema["minItems"]} items')
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            errors.append(f'{path}: more than {schema["maxItems"]} items')
        if isinstance(schema.get('items'), dict):
            for i, item in enumerate(value):
                _check(schema['items'], item, f'{path}[{i}]', errors, limit)
    if _TYPES['number'](value):
        for key, ok in (('minimum', lambda v, b: v >= b), ('maximum', lambda v, b: v <= b),
                        ('exclusiveMinimum', lambda v, b: v > b), ('exclusiveMaximum', lambda v, b: v < b)):
            if isinstance(schema.get(key), (int, float)) and not ok(value, schema[key]):
                errors.append(f'{path}: {value} violates {key} {schema[key]}')
    if isinstance(value, str):
        if 'minLength' in schema and len(value) < schema['minLength']:
            errors.append(f'{path}: shorter than {schema["minLength"]}')
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            errors.append(f'{path}: longer than {schema["maxLength"]}')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            errors.append(f'{path}: does not match {schema["pattern"]!r}')
    for key in ('anyOf', 'oneOf'):
        options = schema.get(key)
        if isinstance(options, list) and options:
            matching = sum(1 for o in options if not schema_errors(o, value, path, limit))
            if matching == 0 or (key == 'oneOf' and matching > 1):
                errors.append(f'{path}: matches {matching} of the {key} options')
    for sub in schema.get('allOf') or []:
        _check(sub, value, path, errors, limit)
