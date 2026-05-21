#!/usr/bin/env python3
"""AsyncClientFactory — creates and caches AsyncFoundryClient (SRS §3.4).

Creates a FoundryClient instance configured with UserTokenAuth
and injects attribution headers when ENABLE_ATTRIBUTION=true.
"""

import os
from typing import Any, Optional, TYPE_CHECKING

from foundry_cli.common.config_loader import ConfigLoader, ConfigurationError

if TYPE_CHECKING:
    from foundry_sdk import UserTokenAuth


class AsyncClientFactory:
    """Creates FoundryClient instances.

    Usage
    -----
    >>> factory = AsyncClientFactory()
    >>> client = factory.create(cfg)
    """

    _instance = None

    @classmethod
    def create(cls, cfg: ConfigLoader):
        """Create a FoundryClient configured with credentials from ConfigLoader.

        Parameters
        ----------
        cfg : ConfigLoader
            Configuration instance with loaded credentials.

        Returns
        -------
        FoundryClient
            Configured SDK client instance.

        Raises
        ------
        ConfigurationError
            If credentials are missing or SDK is not installed.
        """
        token = cfg.token
        hostname = cfg.hostname

        if not token or not hostname:
            raise ConfigurationError(
                "Missing FOUNDRY_TOKEN and/or FOUNDRY_HOSTNAME. "
                "Set credentials in .env or shell environment."
            )

        try:
            from foundry_sdk import FoundryClient, UserTokenAuth
        except ImportError:
            raise ConfigurationError(
                "foundry-sdk not installed; run 'pip install foundry-platform-python'"
            )

        auth: UserTokenAuth = UserTokenAuth(token)

        # Build client kwargs
        client_kwargs: dict[str, Any] = {
            "auth": auth,
            "hostname": hostname,
        }

        # Inject attribution if enabled
        if cfg.enable_attribution and cfg.attribution_rids:
            client_kwargs["attribution_rids"] = cfg.attribution_rids.split(",")

        client = FoundryClient(**client_kwargs)
        cls._instance = client
        return client

    @classmethod
    def get(cls):
        """Get the cached client instance.

        Returns
        -------
        FoundryClient or None
            Cached client instance.
        """
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the cached instance (for testing)."""
        cls._instance = None
