"""JSON Schema validation for OCF documents.

Loads the bundled OCF schema and validates documents against it. The
schema is shipped with the package as a data file under
``ocf.schemas`` — there is no runtime dependency on the spec repo.

Public API:

- :func:`load_schema` — return the parsed schema dict.
- :func:`get_validator` — return a configured ``jsonschema`` validator.
- :func:`iter_errors` — yield every validation error.
- :func:`validate` — return all errors as a list (empty list = valid).
- :func:`validate_strict` — raise on the first error.
- :func:`is_valid` — boolean shortcut.

The current Draft is JSON Schema Draft-07 (matching the spec's
``$schema`` declaration).
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, Iterator

from jsonschema import Draft7Validator, ValidationError


SUPPORTED_VERSIONS: tuple[str, ...] = ("0.1.0",)
DEFAULT_VERSION: str = "0.1.0"


# Cache parsed schemas + validators per version. Both are pure-data
# objects that are safe to share across calls.
_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
_VALIDATOR_CACHE: dict[str, Draft7Validator] = {}


def load_schema(version: str = DEFAULT_VERSION) -> dict[str, Any]:
    """Load the OCF JSON schema for ``version``.

    Returns the cached parsed dict on subsequent calls.

    Raises
    ------
    ValueError
        If ``version`` is not in :data:`SUPPORTED_VERSIONS`.
    """
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(
            f"Unsupported OCF schema version: {version!r}. "
            f"Supported: {SUPPORTED_VERSIONS}"
        )
    if version not in _SCHEMA_CACHE:
        filename = f"ocf-v{version}.schema.json"
        schema_text = (
            resources.files("ocf.schemas").joinpath(filename).read_text(encoding="utf-8")
        )
        _SCHEMA_CACHE[version] = json.loads(schema_text)
    return _SCHEMA_CACHE[version]


def get_validator(version: str = DEFAULT_VERSION) -> Draft7Validator:
    """Return a Draft7Validator configured with the OCF schema."""
    if version not in _VALIDATOR_CACHE:
        schema = load_schema(version)
        # Validate the schema itself first — guards against accidentally
        # shipping a malformed schema.
        Draft7Validator.check_schema(schema)
        _VALIDATOR_CACHE[version] = Draft7Validator(schema)
    return _VALIDATOR_CACHE[version]


def iter_errors(doc: Any, version: str = DEFAULT_VERSION) -> Iterator[ValidationError]:
    """Yield every :class:`jsonschema.ValidationError` for ``doc``."""
    yield from get_validator(version).iter_errors(doc)


def validate(doc: Any, version: str = DEFAULT_VERSION) -> list[ValidationError]:
    """Return all validation errors. Empty list means the document is valid."""
    return list(iter_errors(doc, version))


def validate_strict(doc: Any, version: str = DEFAULT_VERSION) -> None:
    """Validate ``doc``; raise the first error encountered.

    Raises
    ------
    jsonschema.ValidationError
        When the document fails validation. The exception's ``.message``
        and ``.json_path`` describe the violation.
    """
    get_validator(version).validate(doc)


def is_valid(doc: Any, version: str = DEFAULT_VERSION) -> bool:
    """Return ``True`` if ``doc`` validates against the schema."""
    return get_validator(version).is_valid(doc)


__all__ = [
    "SUPPORTED_VERSIONS",
    "DEFAULT_VERSION",
    "ValidationError",
    "load_schema",
    "get_validator",
    "iter_errors",
    "validate",
    "validate_strict",
    "is_valid",
]
