"""Shared timeout normalization approved in QUESTION-192."""

import math
import os

ENV_TIMEOUT_S = "FOUNDRY_AGENTIC_CLI_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 30
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 3600


def resolve_timeout(
    cli_timeout: int | None = None,
    *,
    env_var: str = ENV_TIMEOUT_S,
    default: int = DEFAULT_TIMEOUT_S,
) -> int:
    """Resolve CLI override, normalized environment, then namespace default."""
    if cli_timeout is not None:
        if (
            isinstance(cli_timeout, bool)
            or not isinstance(cli_timeout, int)
            or not MIN_TIMEOUT_S <= cli_timeout <= MAX_TIMEOUT_S
        ):
            raise ValueError("--timeout must be an integer between 1 and 3600 seconds")
        return cli_timeout
    try:
        value = int(os.environ.get(env_var, str(default)))
    except ValueError:
        value = default
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, value))


def validate_internal_timeout(timeout_s: float | int | None) -> float | int | None:
    """Allow finite positive internal timeouts, including bounded test values."""
    if timeout_s is None:
        return None
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not 0 < timeout_s <= MAX_TIMEOUT_S
        or not math.isfinite(timeout_s)
    ):
        raise ValueError("timeout_s must be finite, positive and at most 3600 seconds")
    return timeout_s
