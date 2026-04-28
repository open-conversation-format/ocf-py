"""Tests for ocf.core.schema.

Two layers:

1. **Module-level**: schema loads, validator caches, basic positive/negative
   cases that exercise the wire-strict role validation rules.
2. **Spec cross-check**: every ``.ocf.json`` file in the spec repo's
   ``examples/`` directory must validate against the bundled schema.
   This is the integration test that catches inconsistencies between
   the spec and its own examples.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocf.core.schema import (
    DEFAULT_VERSION,
    SUPPORTED_VERSIONS,
    ValidationError,
    get_validator,
    is_valid,
    iter_errors,
    load_schema,
    validate,
    validate_strict,
)


# Local path convention: spec repo lives next to ocf-py during dev.
# Resolved relative to this test file's location.
SPEC_EXAMPLES_DIR = (
    Path(__file__).parent.parent.parent.parent
    / "OpenChatFormat"
    / "examples"
).resolve()


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

def test_supported_versions_includes_default() -> None:
    assert DEFAULT_VERSION in SUPPORTED_VERSIONS


def test_load_schema_returns_dict() -> None:
    schema = load_schema()
    assert isinstance(schema, dict)
    assert schema["title"] == "Open Conversation Format"


def test_schema_const_version_matches_default() -> None:
    schema = load_schema()
    assert schema["properties"]["ocf_version"]["const"] == DEFAULT_VERSION


def test_schema_self_validates_as_draft7() -> None:
    # If load_schema didn't raise, the schema passed Draft7 meta-schema check.
    get_validator()


def test_schema_cache_returns_same_object() -> None:
    a = load_schema()
    b = load_schema()
    assert a is b


def test_validator_cache_returns_same_object() -> None:
    a = get_validator()
    b = get_validator()
    assert a is b


def test_unsupported_version_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        load_schema(version="9.9.9")


# ---------------------------------------------------------------------------
# Spec cross-check: every examples/*.ocf.json must validate
# ---------------------------------------------------------------------------

def _spec_example_files() -> list[Path]:
    """Return all .ocf.json files under the spec's examples dir."""
    if not SPEC_EXAMPLES_DIR.exists():
        return []
    return sorted(SPEC_EXAMPLES_DIR.rglob("*.ocf.json"))


SPEC_EXAMPLE_FILES = _spec_example_files()


def test_spec_examples_dir_found() -> None:
    """Sanity: the spec/examples directory must be reachable from this test.

    If this fails, the SPEC_EXAMPLES_DIR path constant in this file
    needs adjusting for the local development layout.
    """
    assert SPEC_EXAMPLES_DIR.exists(), (
        f"Cannot find spec examples at {SPEC_EXAMPLES_DIR}. "
        "Adjust SPEC_EXAMPLES_DIR in test_schema.py for your local layout."
    )


def test_spec_examples_found() -> None:
    assert len(SPEC_EXAMPLE_FILES) > 0, (
        f"No .ocf.json files found under {SPEC_EXAMPLES_DIR}"
    )


@pytest.mark.parametrize(
    "path",
    SPEC_EXAMPLE_FILES,
    ids=lambda p: p.relative_to(SPEC_EXAMPLES_DIR).as_posix(),
)
def test_spec_example_validates(path: Path) -> None:
    """Every spec example must conform to the bundled schema."""
    with path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    errors = validate(doc)
    if errors:
        details = "\n".join(
            f"  - {e.json_path}: {e.message}" for e in errors[:10]
        )
        pytest.fail(
            f"{path.name} failed validation with {len(errors)} error(s):\n"
            f"{details}"
        )


# ---------------------------------------------------------------------------
# Minimal positive case
# ---------------------------------------------------------------------------

MINIMAL_VALID: dict = {
    "ocf_version": "0.1.0",
    "conversation": {
        "id": "conv_001",
        "created_at": "2026-04-26T12:00:00Z",
    },
    "messages": [
        {
            "id": "msg_001",
            "message": {"role": "user", "content": "hi"},
        }
    ],
}


def test_minimal_valid_passes() -> None:
    assert validate(MINIMAL_VALID) == []


def test_is_valid_true_on_minimal() -> None:
    assert is_valid(MINIMAL_VALID) is True


def test_validate_strict_passes_on_minimal() -> None:
    validate_strict(MINIMAL_VALID)  # raises on failure


# ---------------------------------------------------------------------------
# Wire-strict negative cases (regression-guards for role rules)
# ---------------------------------------------------------------------------

def _doc_with_messages(messages: list[dict]) -> dict:
    return {
        "ocf_version": "0.1.0",
        "conversation": {
            "id": "c", "created_at": "2026-04-26T12:00:00Z"
        },
        "messages": messages,
    }


def test_missing_ocf_version_fails() -> None:
    bad = dict(MINIMAL_VALID)
    del bad["ocf_version"]
    assert not is_valid(bad)


def test_wrong_ocf_version_fails() -> None:
    bad = {**MINIMAL_VALID, "ocf_version": "0.0.1"}
    assert not is_valid(bad)


def test_unknown_role_fails() -> None:
    bad = _doc_with_messages([{
        "id": "m",
        "message": {"role": "wizard", "content": "hi"},
    }])
    assert not is_valid(bad)


def test_user_with_null_content_fails() -> None:
    """User must have non-null content."""
    bad = _doc_with_messages([{
        "id": "m",
        "message": {"role": "user", "content": None},
    }])
    assert not is_valid(bad)


def test_user_with_tool_calls_fails() -> None:
    """User must not carry tool_calls (assistant-only field)."""
    bad = _doc_with_messages([{
        "id": "m",
        "message": {
            "role": "user",
            "content": "hi",
            "tool_calls": [{
                "id": "t",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }],
        },
    }])
    assert not is_valid(bad)


def test_user_with_tool_call_id_fails() -> None:
    bad = _doc_with_messages([{
        "id": "m",
        "message": {
            "role": "user",
            "content": "hi",
            "tool_call_id": "t",
        },
    }])
    assert not is_valid(bad)


def test_system_with_image_url_block_fails() -> None:
    """System content is text-only."""
    bad = _doc_with_messages([{
        "id": "m",
        "message": {
            "role": "system",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://x.png"}},
            ],
        },
    }])
    assert not is_valid(bad)


def test_assistant_null_content_with_tool_calls_passes() -> None:
    """Assistant may have null content if tool_calls is non-empty."""
    doc = _doc_with_messages([{
        "id": "m",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "t",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }],
        },
    }])
    assert is_valid(doc)


def test_assistant_null_content_no_tool_calls_fails() -> None:
    bad = _doc_with_messages([{
        "id": "m",
        "message": {"role": "assistant", "content": None},
    }])
    assert not is_valid(bad)


def test_assistant_with_tool_call_id_fails() -> None:
    """Assistant cannot carry tool_call_id (tool-message-only)."""
    bad = _doc_with_messages([{
        "id": "m",
        "message": {
            "role": "assistant",
            "content": "hi",
            "tool_call_id": "t",
        },
    }])
    assert not is_valid(bad)


def test_tool_without_tool_call_id_fails() -> None:
    bad = _doc_with_messages([{
        "id": "m",
        "message": {"role": "tool", "content": "result"},
    }])
    assert not is_valid(bad)


def test_tool_with_tool_calls_fails() -> None:
    bad = _doc_with_messages([{
        "id": "m",
        "message": {
            "role": "tool",
            "content": "result",
            "tool_call_id": "t",
            "tool_calls": [{
                "id": "x",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }],
        },
    }])
    assert not is_valid(bad)


def test_unknown_field_in_message_fails() -> None:
    """openai_message has additionalProperties: false."""
    bad = _doc_with_messages([{
        "id": "m",
        "message": {
            "role": "user",
            "content": "hi",
            "unknown_field": "value",
        },
    }])
    assert not is_valid(bad)


# ---------------------------------------------------------------------------
# File block constraints
# ---------------------------------------------------------------------------

def test_file_block_without_file_id_or_data_fails() -> None:
    bad = _doc_with_messages([{
        "id": "m",
        "message": {
            "role": "user",
            "content": [
                {"type": "file", "file": {"filename": "x.pdf"}},
            ],
        },
    }])
    assert not is_valid(bad)


def test_file_block_with_file_id_passes() -> None:
    doc = _doc_with_messages([{
        "id": "m",
        "message": {
            "role": "user",
            "content": [
                {"type": "file", "file": {"file_id": "f-123"}},
            ],
        },
    }])
    assert is_valid(doc)


# ---------------------------------------------------------------------------
# Hash patterns and minimums
# ---------------------------------------------------------------------------

def test_invalid_sha256_pattern_fails() -> None:
    bad = {
        **MINIMAL_VALID,
        "resources": [{
            "id": "r",
            "kind": "user_file",
            "sha256": "not-a-hash",
            "source": {"type": "file", "path": "resources/x"},
        }],
    }
    assert not is_valid(bad)


def test_valid_sha256_passes() -> None:
    doc = {
        **MINIMAL_VALID,
        "resources": [{
            "id": "r",
            "kind": "user_file",
            "sha256": "0" * 64,
            "source": {"type": "file", "path": "resources/x"},
        }],
    }
    assert is_valid(doc)


def test_negative_byte_size_fails() -> None:
    bad = {
        **MINIMAL_VALID,
        "resources": [{
            "id": "r",
            "kind": "user_file",
            "byte_size": -1,
            "source": {"type": "file", "path": "resources/x"},
        }],
    }
    assert not is_valid(bad)


def test_negative_duration_ms_fails() -> None:
    bad = _doc_with_messages([{
        "id": "m",
        "duration_ms": -50,
        "message": {"role": "user", "content": "hi"},
    }])
    assert not is_valid(bad)
