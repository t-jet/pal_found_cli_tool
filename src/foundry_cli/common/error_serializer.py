#!/usr/bin/env python3
"""ErrorSerializer mapping SDK exceptions to exit codes (ADR-001).

Maps SDK and runtime exceptions to the structured exit code taxonomy
defined in ADR-001. Produces a JSON error envelope on stdout with
structured metadata.

Exit Code Taxonomy (ADR-001)
----------------------------
0  : Success
1  : UserInputError (invalid CLI args, validation failure, missing param)
2  : AuthenticationError (missing/invalid token, SDK auth failure)
3  : PermissionDeniedError (API 403)
4  : NotFoundError (API 404, resource does not exist)
5  : TimeoutError (asyncio.wait_for timeout, SIGINT/SIGTERM)
6  : ServerError (API 5xx excluding 503)
7  : RateLimitExhausted (HTTP 429 + retries exhausted)
8  : AccessControlError (CLI access control policy)
9  : ConfigurationError (missing env var, malformed config)
"""

import asyncio
import json
import logging
import sys
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Type

logger = logging.getLogger(__name__)


# ADR-001 Exit code taxonomy
EXIT_SUCCESS = 0
EXIT_USER_INPUT = 1
EXIT_AUTH = 2
EXIT_PERMISSION_DENIED = 3
EXIT_NOT_FOUND = 4
EXIT_TIMEOUT = 5
EXIT_SERVER_ERROR = 6
EXIT_RATE_LIMIT = 7
EXIT_ACCESS_CONTROL = 8
EXIT_CONFIGURATION = 9

EXIT_CODE_NAMES = {
    EXIT_SUCCESS: "Success",
    EXIT_USER_INPUT: "UserInputError",
    EXIT_AUTH: "AuthenticationError",
    EXIT_PERMISSION_DENIED: "PermissionDeniedError",
    EXIT_NOT_FOUND: "NotFoundError",
    EXIT_TIMEOUT: "TimeoutError",
    EXIT_SERVER_ERROR: "ServerError",
    EXIT_RATE_LIMIT: "RateLimitExhausted",
    EXIT_ACCESS_CONTROL: "AccessControlError",
    EXIT_CONFIGURATION: "ConfigurationError",
}


class _SDKAuthError(Exception):
    """SDK authentication failure."""


class _SDKValidationError(Exception):
    """SDK validation failure."""


class _SDKNotFoundError(Exception):
    """SDK resource not found."""


class _SDKRateLimitError(Exception):
    """SDK rate limit exceeded."""


class _SDKConflictError(Exception):
    """SDK conflict error."""


class _SDKNetworkError(Exception):
    """SDK network error."""


# Map exception classes to ADR-001 exit codes
_EXCEPTION_TO_EXIT_CODE: Dict[Type[BaseException], int] = {
    # Auth errors -> code 2
    _SDKAuthError: EXIT_AUTH,
    # Validation -> code 1 (UserInputError in ADR-001)
    _SDKValidationError: EXIT_USER_INPUT,
    ValueError: EXIT_USER_INPUT,
    TypeError: EXIT_USER_INPUT,
    # Permission denied -> code 3
    PermissionError: EXIT_PERMISSION_DENIED,
    # Not found -> code 4
    _SDKNotFoundError: EXIT_NOT_FOUND,
    FileNotFoundError: EXIT_NOT_FOUND,
    # Timeout -> code 5
    TimeoutError: EXIT_TIMEOUT,
    asyncio.TimeoutError: EXIT_TIMEOUT,
    # Server errors -> code 6
    # Rate limit -> code 7
    _SDKRateLimitError: EXIT_RATE_LIMIT,
    # Config errors -> code 9
    ImportError: EXIT_CONFIGURATION,
    ModuleNotFoundError: EXIT_CONFIGURATION,
    EnvironmentError: EXIT_CONFIGURATION,
    OSError: EXIT_CONFIGURATION,
}


class ErrorSerializer:
    """Serializes exceptions to ADR-001 exit codes and JSON error envelopes.

    Maps exceptions to the structured exit code taxonomy and produces
    a JSON error envelope on stdout.

    Error Envelope Schema
    ---------------------
    {
        "error": true,
        "exit_code": int,
        "exit_code_name": str,
        "message": str,
        "exception_type": str,
        "traceback": str,
        "call_id": str,
    }

    Examples
    --------
    >>> serializer = ErrorSerializer()
    >>> try:
    ...     await call_api()
    ... except Exception as e:
    ...     exit_code = serializer.serialize(e)
    ...     sys.exit(exit_code)
    """

    def __init__(self, call_id: Optional[str] = None) -> None:
        """Initialize ErrorSerializer.

        Parameters
        ----------
        call_id : str, optional
            Unique identifier for this CLI invocation. Auto-generated if not set.
        """
        self.call_id = call_id or str(uuid.uuid4())

    def _get_http_status_from_exception(
        self, exception: BaseException
    ) -> Optional[int]:
        """Extract HTTP status code from an exception if available.

        Parameters
        ----------
        exception : BaseException
            The exception to inspect.

        Returns
        -------
        int or None
            HTTP status code if available, None otherwise.
        """
        if hasattr(exception, "response") and exception.response is not None:
            return getattr(exception.response, "status_code", None)
        return None

    def _classify_http_exception(
        self, exception: BaseException
    ) -> Optional[int]:
        """Classify HTTP exceptions based on status code.

        Parameters
        ----------
        exception : BaseException
            The exception to classify.

        Returns
        -------
        int or None
            Exit code if classifiable, None otherwise.
        """
        status = self._get_http_status_from_exception(exception)
        if status is None:
            return None

        if status == 401:
            return EXIT_AUTH
        if status == 403:
            return EXIT_PERMISSION_DENIED
        if status == 404:
            return EXIT_NOT_FOUND
        if status == 409:
            return EXIT_USER_INPUT  # Conflict treated as user input
        if status == 429:
            return EXIT_RATE_LIMIT
        if status and status >= 500 and status != 503:
            return EXIT_SERVER_ERROR
        return None

    def serialize(
        self,
        exception: BaseException,
        print_to_stdout: bool = True,
    ) -> int:
        """Serialize an exception to an ADR-001 exit code.

        Maps the exception to an exit code and optionally prints a
        structured JSON error envelope to stdout.

        Parameters
        ----------
        exception : BaseException
            The exception to serialize.
        print_to_stdout : bool, optional
            Whether to print the JSON error envelope to stdout.
            Default: True.

        Returns
        -------
        int
            The exit code (0-9) per ADR-001 taxonomy.
        """
        exit_code = EXIT_USER_INPUT  # Default: UserInputError

        # First try HTTP status classification
        http_exit = self._classify_http_exception(exception)
        if http_exit is not None:
            exit_code = http_exit
        else:
            # Walk MRO for exception type matching
            for klass in type(exception).__mro__:
                if klass in _EXCEPTION_TO_EXIT_CODE:
                    exit_code = _EXCEPTION_TO_EXIT_CODE[klass]
                    break

        exit_code_name = EXIT_CODE_NAMES.get(exit_code, "UnknownError")

        tb_lines = traceback.format_exception(type(exception), exception, exception.__traceback__)
        traceback_str = "".join(tb_lines).rstrip()

        error_envelope: Dict[str, Any] = {
            "error": True,
            "exit_code": exit_code,
            "exit_code_name": exit_code_name,
            "message": str(exception),
            "exception_type": type(exception).__name__,
            "traceback": traceback_str,
            "call_id": self.call_id,
        }

        # Add HTTP status if available
        http_status = self._get_http_status_from_exception(exception)
        if http_status is not None:
            error_envelope["http_status"] = http_status

        if print_to_stdout:
            json_str = json.dumps(error_envelope, default=str)
            sys.stdout.write(json_str + "\n")
            sys.stdout.flush()

        logger.error(
            f"Serialized {type(exception).__name__} to exit code {exit_code} "
            f"({exit_code_name}): {exception}",
            extra={
                "http_status": http_status,
                "call_id": self.call_id,
            },
        )

        return exit_code

    @staticmethod
    def get_exit_code_name(exit_code: int) -> str:
        """Get the human-readable name for an exit code.

        Parameters
        ----------
        exit_code : int
            The exit code to look up.

        Returns
        -------
        str
            The name of the exit code, or 'UnknownError' if not found.
        """
        return EXIT_CODE_NAMES.get(exit_code, "UnknownError")

    @staticmethod
    def create_error_envelope(
        exit_code: int,
        message: str,
        exception_type: str = "UnknownError",
        call_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a JSON error envelope without an exception object.

        Parameters
        ----------
        exit_code : int
            Exit code per ADR-001.
        message : str
            Human-readable error message.
        exception_type : str, optional
            Name of the exception type.
        call_id : str, optional
            Unique invocation identifier.

        Returns
        -------
        Dict[str, Any]
            Error envelope dictionary.
        """
        exit_code_name = EXIT_CODE_NAMES.get(exit_code, "UnknownError")

        return {
            "error": True,
            "exit_code": exit_code,
            "exit_code_name": exit_code_name,
            "message": message,
            "exception_type": exception_type,
            "traceback": "",
            "call_id": call_id or str(uuid.uuid4()),
        }
