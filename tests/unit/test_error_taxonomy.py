"""Unit: OpenCode error taxonomy — raw errors map to stable categories."""
from contributor.opencode.runner import (
    ERROR_AUTH,
    ERROR_BINARY_NOT_FOUND,
    ERROR_MODEL_NOT_FOUND,
    ERROR_PROVIDER_NETWORK,
    ERROR_PROVIDER_RATE_LIMIT,
    ERROR_PROVIDER_SERVER,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_VARIANT_UNSUPPORTED,
    classify_error,
)


def test_server_error():
    assert classify_error(1, "", 'Error: {"name": "UnknownError", "data": {"message": "Unexpected server error"}}') == ERROR_PROVIDER_SERVER


def test_auth():
    assert classify_error(1, "", "Authentication failed: invalid api key") == ERROR_AUTH


def test_model_not_found():
    assert classify_error(1, "", "model not found: foo/bar") == ERROR_MODEL_NOT_FOUND


def test_variant_unsupported():
    assert classify_error(1, "", "unknown variant 'turbo' for model") == ERROR_VARIANT_UNSUPPORTED


def test_rate_limit():
    assert classify_error(1, "", "429 rate limit exceeded") == ERROR_PROVIDER_RATE_LIMIT


def test_network():
    assert classify_error(1, "", "fetch failed: ENOTFOUND api.example.com") == ERROR_PROVIDER_NETWORK


def test_timeout():
    assert classify_error(124, "", "TIMEOUT after 180s", timed_out=True) == ERROR_PROVIDER_TIMEOUT


def test_binary_missing():
    assert classify_error(127, "", "opencode binary not found: opencode") == ERROR_BINARY_NOT_FOUND


def test_success_empty():
    assert classify_error(0, "OK", "") == ""
