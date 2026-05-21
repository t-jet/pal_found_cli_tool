#!/usr/bin/env python3
"""AuthProvider — constructs UserTokenAuth from environment (SRS §3.1).

Validates that FOUNDRY_TOKEN and FOUNDRY_HOSTNAME are present
and constructs a UserTokenAuth instance for the SDK.
"""

import os
from typing import Optional, Tuple

from foundry_cli.common.config_loader import ConfigurationError


class AuthProvider:
    """Constructs and validates UserTokenAuth credentials.

    Usage
    -----
    >>> auth = AuthProvider()
    >>> auth_obj = auth.get_auth()
    """

    @staticmethod
    def validate(token: Optional[str], hostname: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Validate that credentials are present.

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
        """
        if not token:
            return False, "Missing FOUNDRY_TOKEN credential"
        if not hostname:
            return False, "Missing FOUNDRY_HOSTNAME credential"
        return True, None

    @staticmethod
    def get_auth(token: str, hostname: str):
        """Construct UserTokenAuth from credentials.

        Parameters
        ----------
        token : str
            FOUNDRY_TOKEN value.
        hostname : str
            FOUNDRY_HOSTNAME value.

        Returns
        -------
        UserTokenAuth
            SDK auth instance.
        """
        try:
            from foundry_sdk import UserTokenAuth
            return UserTokenAuth(token)
        except ImportError:
            raise ConfigurationError(
                "foundry-sdk not installed; run 'pip install foundry-platform-python'"
            )
