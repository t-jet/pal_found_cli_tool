"""AuthProvider — constructs UserTokenAuth from environment (SRS §3.1).

Validates that FOUNDRY_TOKEN and FOUNDRY_HOSTNAME are present
and constructs a UserTokenAuth instance for the SDK.
"""

from typing import TYPE_CHECKING, Optional, Tuple

from foundry_cli.common.config_loader import ConfigurationError

if TYPE_CHECKING:
    from foundry_sdk import UserTokenAuth


class AuthProvider:
    """Constructs and validates UserTokenAuth credentials.

    Usage
    -----
    >>> auth = AuthProvider()
    >>> auth_obj = auth.get_auth(token, hostname)
    """

    @staticmethod
    def validate(token: Optional[str], hostname: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Validate that credentials are present and non-empty.

        Whitespace-only values are rejected — a token consisting solely of
        spaces is not a credential and must not pass validation (review
        finding A1/A4, OWASP A07).

        Parameters
        ----------
        token : str or None
            FOUNDRY_TOKEN value.
        hostname : str or None
            FOUNDRY_HOSTNAME value.

        Returns
        -------
        Tuple[bool, str or None]
            (is_valid, error_message). If valid, error_message is None.
            Error messages reference the env var name only — never the
            credential value — so they are safe to log.
        """
        if token is None or token.strip() == "":
            return False, "Missing FOUNDRY_TOKEN credential"
        if hostname is None or hostname.strip() == "":
            return False, "Missing FOUNDRY_HOSTNAME credential"
        return True, None

    @staticmethod
    def get_auth(token: str) -> "UserTokenAuth":
        """Construct UserTokenAuth from a token string.

        The SDK's ``UserTokenAuth`` is constructed from the token only;
        hostname is consumed by the client factory, not by the auth object,
        so it is intentionally absent from this signature (review finding A2).

        Parameters
        ----------
        token : str
            FOUNDRY_TOKEN value. Caller is expected to have validated it
            via :meth:`validate` first.

        Returns
        -------
        UserTokenAuth
            SDK auth instance.

        Raises
        ------
        ConfigurationError
            If the ``foundry-sdk`` package is not installed.
        """
        try:
            from foundry_sdk import UserTokenAuth
        except ImportError as exc:
            raise ConfigurationError(
                "foundry-sdk not installed; run 'pip install foundry-platform-python'"
            ) from exc
        return UserTokenAuth(token)
