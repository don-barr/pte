"""Check an index (table of contents) file against the CMS JSON Schema."""

import json
from functools import cache
from pathlib import Path

from jsonschema import Draft7Validator

# Copied from CMSgov/price-transparency-guide, tag v2.0.0 (identical through v2.2.1).
SCHEMA_PATH = Path(__file__).parent / "schemas" / "table-of-contents.json"
# Messages quote the offending value; a whole plans list is cut to this many characters.
MAX_VALUE_CHARS = 80


@cache
def _validator() -> Draft7Validator:
    return Draft7Validator(json.loads(SCHEMA_PATH.read_text()))


def _sort_key(path) -> tuple:
    # Indexes sort as numbers, so reporting_plans[3] comes before reporting_plans[10].
    return tuple((0, part, "") if isinstance(part, int) else (1, 0, part) for part in path)


def _describe(path) -> str:
    """("reporting_structure", 0, "reporting_plans", 3) -> "reporting_structure[0] → reporting_plans[3]"."""
    parts = []
    for part in path:
        if isinstance(part, int) and parts:
            parts[-1] += f"[{part}]"
        else:
            parts.append(str(part))
    return " → ".join(parts)


def _message(error) -> str:
    value = repr(error.instance)
    if len(value) <= MAX_VALUE_CHARS:
        return error.message
    return error.message.replace(value, value[:MAX_VALUE_CHARS] + "…")


def validate_index(index: dict) -> list[str]:
    """Readable schema problems, sorted by where they are; empty when the index is valid."""
    errors = sorted(_validator().iter_errors(index), key=lambda e: (_sort_key(e.absolute_path), e.message))
    return [
        f"{_describe(e.absolute_path)}: {_message(e)}" if e.absolute_path else _message(e)
        for e in errors
    ]
