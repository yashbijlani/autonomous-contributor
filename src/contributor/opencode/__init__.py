"""OpenCode re-exports."""
from contributor.opencode.client import (
    ModelSpec,
    OpenCodeRequest,
    build_argv,
    opencode_available,
    resolve_model,
    resolve_spec,
    resolve_variant,
    split_model_id,
)
from contributor.opencode.prompts import build_debug_prompt, build_implement_prompt
from contributor.opencode.runner import (
    ERROR_AUTH,
    ERROR_BINARY_NOT_FOUND,
    ERROR_MALFORMED_REQUEST,
    ERROR_MODEL_NOT_FOUND,
    ERROR_PROVIDER_CONFIG,
    ERROR_PROVIDER_NETWORK,
    ERROR_PROVIDER_RATE_LIMIT,
    ERROR_PROVIDER_SERVER,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_SUBPROCESS,
    ERROR_UNKNOWN,
    ERROR_VARIANT_UNSUPPORTED,
    OpenCodeResult,
    OpenCodeRunner,
    classify_error,
)

__all__ = [
    "ModelSpec", "OpenCodeRequest", "OpenCodeResult", "OpenCodeRunner", "build_argv",
    "build_debug_prompt", "build_implement_prompt", "opencode_available", "resolve_model",
    "resolve_spec", "resolve_variant", "split_model_id", "classify_error",
    "ERROR_AUTH", "ERROR_BINARY_NOT_FOUND", "ERROR_MALFORMED_REQUEST",
    "ERROR_MODEL_NOT_FOUND", "ERROR_PROVIDER_CONFIG", "ERROR_PROVIDER_NETWORK",
    "ERROR_PROVIDER_RATE_LIMIT", "ERROR_PROVIDER_SERVER", "ERROR_PROVIDER_TIMEOUT",
    "ERROR_SUBPROCESS", "ERROR_UNKNOWN", "ERROR_VARIANT_UNSUPPORTED",
]
