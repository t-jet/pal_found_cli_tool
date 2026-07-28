"""AsyncClientFactory — creates stateless AsyncFoundryClient (SRS §3.4).

Creates an ``AsyncFoundryClient`` instance per invocation, configured with
``UserTokenAuth`` and the resolved hostname. When attribution is enabled,
the SDK attribution context variable is set for the current invocation.

Stateless per invocation (DCC-3): no client is cached across calls —
each ``create()`` returns a fresh client. This keeps tests isolated,
avoids shared mutable state, and matches the CLI's per-command lifecycle.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Optional

from foundry_cli.common.config_loader import ConfigLoader, ConfigurationError
from foundry_cli.common.tracing_provider import B3Context, TracingProvider

if TYPE_CHECKING:
    from foundry_sdk import AsyncFoundryClient


class AsyncClientFactory:
    """Creates stateless ``AsyncFoundryClient`` instances.

    Usage
    -----
    >>> factory = AsyncClientFactory()
    >>> client = factory.create(cfg)
    """

    def __init__(self) -> None:
        # Per-instance state only — no class-level caching. Each factory
        # instance is independent; ``create()`` never mutates shared state.
        self._last_client: AsyncFoundryClient | None = None

    def create(self, cfg: ConfigLoader) -> "AsyncFoundryClient":
        """Create an ``AsyncFoundryClient`` from credentials in ``cfg``.

        Validates token and hostname are present before constructing the
        client. A new client is returned on every call — nothing is cached
        on the class.

        Parameters
        ----------
        cfg : ConfigLoader
            Configuration instance with loaded credentials.

        Returns
        -------
        AsyncFoundryClient
            Freshly constructed SDK async client instance.

        Raises
        ------
        ConfigurationError
            If credentials are missing or the SDK is not installed.
        """
        token = cfg.token
        hostname = cfg.hostname

        # Strip before presence check so whitespace-only tokens are rejected
        # (review finding A1 applied at the factory boundary as defense in depth).
        if not token or not token.strip() or not hostname or not hostname.strip():
            raise ConfigurationError(
                "Missing FOUNDRY_TOKEN and/or FOUNDRY_HOSTNAME. "
                "Set credentials in .env or shell environment."
            )

        try:
            from foundry_sdk import ATTRIBUTION_VAR, AsyncFoundryClient, UserTokenAuth
        except ImportError as exc:
            raise ConfigurationError(
                "foundry-sdk not installed; run 'pip install foundry-platform-sdk'"
            ) from exc

        auth: UserTokenAuth = UserTokenAuth(token)

        client_kwargs: dict[str, Any] = {
            "auth": auth,
            "hostname": hostname,
            "preview": True,
        }

        # SDK reads attribution from a context variable when sending requests.
        # Always set it here so disabled or empty config clears prior values.
        rids: list[str] = []
        if cfg.enable_attribution and cfg.attribution_rids:
            rids = [r.strip() for r in cfg.attribution_rids.split(",")]
            rids = [r for r in rids if r]
        ATTRIBUTION_VAR.set(rids or None)

        client: AsyncFoundryClient = AsyncFoundryClient(**client_kwargs)
        self._last_client = client
        return client

    @contextmanager
    def invocation_scope(
        self,
        cfg: ConfigLoader,
        supplied: B3Context | None = None,
    ) -> Iterator[B3Context | None]:
        """Keep one trace context active across client creation and SDK calls.

        Callers enter this scope before :meth:`create` and leave it only after
        retry handling and response processing finish.
        """
        with TracingProvider(config=cfg).scope(supplied) as context:
            yield context

    @property
    def last_client(self) -> Optional["AsyncFoundryClient"]:
        """Most recently created client, or ``None``.

        Convenience accessor for inspection/diagnostics only. Callers must
        not rely on this for caching — ``create()`` always builds a new
        client.
        """
        return self._last_client
