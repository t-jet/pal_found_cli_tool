"""ConfigLoader — .env file search path (ADR-006).

Loads configuration from .env file following ADR-006 search path order:
1. Explicit path via FOUNDRY_AGENTIC_CLI_ENV_FILE
2. Git root .env (walk up from CWD to find .git)
3. CWD .env fallback (non-git deployment)
4. Environment variables only (no .env file)

Never searches home directory.
"""

import os
from enum import Enum
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv

from pal_found_cli.common.log_setup import DEFAULT_LOG_LEVEL, ENV_LOG_LEVEL

# Environment variable names
ENV_ENV_FILE = "FOUNDRY_AGENTIC_CLI_ENV_FILE"
ENV_TOKEN = "FOUNDRY_TOKEN"  # nosec B105 - environment variable name, not a secret value.
ENV_HOSTNAME = "FOUNDRY_HOSTNAME"
ENV_TIMEOUT_S = "FOUNDRY_AGENTIC_CLI_TIMEOUT_S"
ENV_DEFAULT_FORMAT = "FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT"
ENV_ENABLED = "FOUNDRY_AGENTIC_CLI_ENABLED"
ENV_READONLY = "FOUNDRY_AGENTIC_CLI_READONLY"
ENV_METADATA_ONLY = "FOUNDRY_AGENTIC_CLI_METADATA_ONLY"
ENV_ENABLE_ATTRIBUTION = "FOUNDRY_AGENTIC_CLI_ENABLE_ATTRIBUTION"
ENV_ATTRIBUTION_RIDS = "FOUNDRY_AGENTIC_CLI_ATTRIBUTION_RIDS"
ENV_ENABLE_TRACING = "FOUNDRY_AGENTIC_CLI_ENABLE_TRACING"
ENV_MAX_DOWNLOAD_BYTES = "FOUNDRY_AGENTIC_CLI_MAX_DOWNLOAD_BYTES"
ENV_DOWNLOAD_PATH = "FOUNDRY_AGENTIC_CLI_DOWNLOAD_PATH"
ENV_SESSION_PATH = "FOUNDRY_AGENTIC_CLI_SESSION_PATH"

# Defaults
DEFAULT_TIMEOUT_S = 30
DEFAULT_MIN_TIMEOUT_S = 1
DEFAULT_MAX_TIMEOUT_S = 3600
DEFAULT_FORMAT = "auto"
DEFAULT_MAX_DOWNLOAD_BYTES = 1_572_864
DEFAULT_DOWNLOAD_PATH = Path(".foundry-data/downloads")
DEFAULT_SESSION_PATH = Path(".foundry-data/sessions")

# Exit code per ADR-001
EXIT_CONFIGURATION = 9

# Truthy boolean string set — kept consistent across property accessors
# and get_bool() so the parsing contract is identical everywhere.
_TRUTHY = ("true", "1", "yes", "on")

_E = TypeVar("_E", bound=Enum)


class ConfigLoader:
    """Loads configuration from .env file following ADR-006 search path.

    Search path order:
    1. Explicit: FOUNDRY_AGENTIC_CLI_ENV_FILE
    2. Git root: Walk up from CWD to find .git, load .env there
    3. Env vars only: No .env file, use shell environment

    Usage
    -----
    >>> cfg = ConfigLoader()
    >>> cfg.load()
    >>> token = cfg.token
    """

    def __init__(self) -> None:
        self._loaded_file: str | None = None

    def load(self) -> None:
        """Load configuration following ADR-006 search path."""
        # Order 1 — Explicit override
        explicit_path = os.environ.get(ENV_ENV_FILE)
        if explicit_path:
            path = Path(explicit_path)
            if not path.exists():
                raise ConfigurationError(
                    f"Explicit .env file not found: {explicit_path}"
                )
            load_dotenv(str(path), override=False)
            self._loaded_file = str(path)
            return

        # Order 2 — Git root .env
        git_root = self._find_git_root()
        if git_root:
            env_path = git_root / ".env"
            if env_path.exists():
                load_dotenv(str(env_path), override=False)
                self._loaded_file = str(env_path)
                return

        # Order 3 — CWD .env fallback
        cwd_env = Path.cwd() / ".env"
        if cwd_env.exists():
            load_dotenv(str(cwd_env), override=False)
            self._loaded_file = str(cwd_env)
            return

        # Order 4 — Env vars only
        # No error — credentials may be in shell environment

    def _find_git_root(self, max_depth: int = 20) -> Path | None:
        """Walk up from CWD to find .git directory.

        Parameters
        ----------
        max_depth : int
            Maximum directory levels to walk up.

        Returns
        -------
        Path or None
            Git root directory or None if not found.
        """
        current = Path.cwd()
        depth = 0
        while depth < max_depth:
            if (current / ".git").exists():
                return current
            parent = current.parent
            if parent == current:
                break
            current = parent
            depth += 1
        return None

    # --- Typed accessors (generic, follow story AC) ---

    def get_str(self, name: str, default: str | None = None) -> str | None:
        """Get string environment variable value.

        Parameters
        ----------
        name : str
            Environment variable name.
        default : str, optional
            Default value if not set.

        Returns
        -------
        str or None
            Environment variable value, stripped of surrounding whitespace,
            or ``default`` if unset.
        """
        val = os.environ.get(name)
        if val is None:
            return default
        return val.strip()

    def get_bool(self, name: str, default: bool = False) -> bool:
        """Get boolean environment variable value.

        Truthy values (case-insensitive): ``true``, ``1``, ``yes``, ``on``.
        Everything else (including unset) evaluates per ``default``.

        Parameters
        ----------
        name : str
            Environment variable name.
        default : bool
            Default value when the variable is unset.

        Returns
        -------
        bool
            Parsed boolean value.
        """
        val = os.environ.get(name)
        if val is None:
            return default
        return val.strip().lower() in _TRUTHY

    def get_int(self, name: str, default: int | None = None) -> int | None:
        """Get integer environment variable value.

        Parameters
        ----------
        name : str
            Environment variable name.
        default : int, optional
            Default value when unset or non-numeric.

        Returns
        -------
        int or None
            Parsed integer, or ``default`` when unset / unparsable.
        """
        val = os.environ.get(name)
        if val is None or val.strip() == "":
            return default
        try:
            return int(val.strip())
        except ValueError:
            return default

    def get_float(self, name: str, default: float | None = None) -> float | None:
        """Get float environment variable value.

        Parameters
        ----------
        name : str
            Environment variable name.
        default : float, optional
            Default value when unset or non-numeric.

        Returns
        -------
        float or None
            Parsed float, or ``default`` when unset / unparsable.
        """
        val = os.environ.get(name)
        if val is None or val.strip() == "":
            return default
        try:
            return float(val.strip())
        except ValueError:
            return default

    def get_enum(
        self, name: str, enum_cls: type[_E], default: _E | None = None
    ) -> _E | None:
        """Get enum environment variable value (case-insensitive).

        Parameters
        ----------
        name : str
            Environment variable name.
        enum_cls : Type[Enum]
            Enum class to coerce into.
        default : Enum member, optional
            Default value when unset or not a valid member.

        Returns
        -------
        Enum member or None
            Matching enum member, or ``default``.
        """
        val = os.environ.get(name)
        if val is None or val.strip() == "":
            return default
        key = val.strip()
        # Try name match first (case-insensitive)
        for member in enum_cls:
            if member.name.lower() == key.lower():
                return member
        # Fall back to value match (case-sensitive — values may be meaningful)
        for member in enum_cls:
            if member.value == key:
                return member
        return default

    # --- Legacy helper aliases (kept for backward compatibility) ---

    def get_env(self, name: str, default: str | None = None) -> str | None:
        """Get raw environment variable value (alias of :meth:`get_str`)."""
        return self.get_str(name, default)

    def get_env_bool(self, name: str, default: bool = False) -> bool:
        """Get boolean environment variable value (alias of :meth:`get_bool`)."""
        return self.get_bool(name, default)

    # --- Property accessors ---

    @property
    def token(self) -> str | None:
        """FOUNDRY_TOKEN value."""
        return os.environ.get(ENV_TOKEN)

    @property
    def hostname(self) -> str | None:
        """FOUNDRY_HOSTNAME value."""
        return os.environ.get(ENV_HOSTNAME)

    @property
    def timeout_s(self) -> int:
        """Call timeout in seconds (ADR-002)."""
        val = os.environ.get(ENV_TIMEOUT_S)
        if val is None:
            return DEFAULT_TIMEOUT_S
        try:
            timeout = int(val)
            return max(DEFAULT_MIN_TIMEOUT_S, min(DEFAULT_MAX_TIMEOUT_S, timeout))
        except ValueError:
            return DEFAULT_TIMEOUT_S

    @property
    def default_format(self) -> str:
        """Default output format."""
        return os.environ.get(ENV_DEFAULT_FORMAT, DEFAULT_FORMAT)

    @property
    def log_level(self) -> str:
        """Log level (ADR-005)."""
        return os.environ.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL)

    @property
    def global_readonly(self) -> bool:
        """Global READONLY flag."""
        return os.environ.get(ENV_READONLY, "").lower() in _TRUTHY

    @property
    def global_enabled(self) -> bool:
        """Global CLI enablement flag, enabled unless explicitly false."""
        return os.environ.get(ENV_ENABLED, "true").lower() not in (
            "false",
            "0",
            "no",
            "off",
        )

    @property
    def global_metadata_only(self) -> bool:
        """Global METADATA_ONLY flag."""
        return os.environ.get(ENV_METADATA_ONLY, "").lower() in _TRUTHY

    @property
    def enable_attribution(self) -> bool:
        """Attribution enabled flag."""
        return os.environ.get(ENV_ENABLE_ATTRIBUTION, "").lower() in _TRUTHY

    @property
    def attribution_rids(self) -> str | None:
        """Comma-separated attribution RIDs."""
        return os.environ.get(ENV_ATTRIBUTION_RIDS)

    @property
    def enable_tracing(self) -> bool:
        """Tracing enabled flag."""
        return os.environ.get(ENV_ENABLE_TRACING, "").lower() in _TRUTHY

    @property
    def max_download_bytes(self) -> int:
        """Maximum persisted binary response size in bytes."""
        raw = os.environ.get(ENV_MAX_DOWNLOAD_BYTES)
        if raw is None:
            return DEFAULT_MAX_DOWNLOAD_BYTES
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ConfigurationError(
                f"{ENV_MAX_DOWNLOAD_BYTES} must be a positive integer"
            ) from exc
        if value <= 0:
            raise ConfigurationError(
                f"{ENV_MAX_DOWNLOAD_BYTES} must be a positive integer"
            )
        return value

    @property
    def download_path(self) -> Path:
        """Validated base path for binary downloads."""
        return self._configured_path(ENV_DOWNLOAD_PATH, DEFAULT_DOWNLOAD_PATH)

    @property
    def session_path(self) -> Path:
        """Validated base path for persisted sessions."""
        return self._configured_path(ENV_SESSION_PATH, DEFAULT_SESSION_PATH)

    @staticmethod
    def _configured_path(name: str, default: Path) -> Path:
        raw = os.environ.get(name)
        if raw is None:
            return default
        value = raw.strip()
        if not value or "\x00" in value:
            raise ConfigurationError(f"{name} must be a non-empty filesystem path")
        return Path(value)

    @property
    def loaded_file(self) -> str | None:
        """Path to loaded .env file, or None."""
        return self._loaded_file


class ConfigurationError(Exception):
    """Configuration loading error (exit code 9 per ADR-001).

    Carries the canonical exit code so callers (CLI entrypoints, error
    serializers) can map the exception to the correct process exit status
    without maintaining a separate lookup table.
    """

    exit_code: int = EXIT_CONFIGURATION
