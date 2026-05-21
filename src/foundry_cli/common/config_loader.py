#!/usr/bin/env python3
"""ConfigLoader — .env file search path (ADR-006).

Loads configuration from .env file following ADR-006 search path order:
1. Explicit path via FOUNDRY_AGENTIC_CLI_ENV_FILE
2. Git root .env (walk up from CWD to find .git)
3. CWD .env fallback (non-git deployment)
4. Environment variables only (no .env file)

Never searches home directory.
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from foundry_cli.common.log_setup import DEFAULT_LOG_LEVEL, ENV_LOG_LEVEL


# Environment variable names
ENV_ENV_FILE = "FOUNDRY_AGENTIC_CLI_ENV_FILE"
ENV_TOKEN = "FOUNDRY_TOKEN"
ENV_HOSTNAME = "FOUNDRY_HOSTNAME"
ENV_TIMEOUT_S = "FOUNDRY_AGENTIC_CLI_TIMEOUT_S"
ENV_DEFAULT_FORMAT = "FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT"
ENV_READONLY = "FOUNDRY_AGENTIC_CLI_READONLY"
ENV_METADATA_ONLY = "FOUNDRY_AGENTIC_CLI_METADATA_ONLY"
ENV_ENABLE_ATTRIBUTION = "FOUNDRY_AGENTIC_CLI_ENABLE_ATTRIBUTION"
ENV_ATTRIBUTION_RIDS = "FOUNDRY_AGENTIC_CLI_ATTRIBUTION_RIDS"
ENV_ENABLE_TRACING = "FOUNDRY_AGENTIC_CLI_ENABLE_TRACING"

# Defaults
DEFAULT_TIMEOUT_S = 30
DEFAULT_MIN_TIMEOUT_S = 1
DEFAULT_MAX_TIMEOUT_S = 3600
DEFAULT_FORMAT = "auto"


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
        self._loaded_file: Optional[str] = None

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

    def _find_git_root(self, max_depth: int = 20) -> Optional[Path]:
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

    # --- Property accessors ---

    @property
    def token(self) -> Optional[str]:
        """FOUNDRY_TOKEN value."""
        return os.environ.get(ENV_TOKEN)

    @property
    def hostname(self) -> Optional[str]:
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
        return os.environ.get(ENV_READONLY, "").lower() in ("true", "1", "yes")

    @property
    def global_metadata_only(self) -> bool:
        """Global METADATA_ONLY flag."""
        return os.environ.get(ENV_METADATA_ONLY, "").lower() in ("true", "1", "yes")

    @property
    def enable_attribution(self) -> bool:
        """Attribution enabled flag."""
        return os.environ.get(ENV_ENABLE_ATTRIBUTION, "").lower() in ("true", "1", "yes")

    @property
    def attribution_rids(self) -> Optional[str]:
        """Comma-separated attribution RIDs."""
        return os.environ.get(ENV_ATTRIBUTION_RIDS)

    @property
    def enable_tracing(self) -> bool:
        """Tracing enabled flag."""
        return os.environ.get(ENV_ENABLE_TRACING, "").lower() in ("true", "1", "yes")

    @property
    def loaded_file(self) -> Optional[str]:
        """Path to loaded .env file, or None."""
        return self._loaded_file

    def get_env(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """Get environment variable value.

        Parameters
        ----------
        name : str
            Environment variable name.
        default : str, optional
            Default value if not set.

        Returns
        -------
        str or None
            Environment variable value.
        """
        return os.environ.get(name, default)

    def get_env_bool(self, name: str, default: bool = False) -> bool:
        """Get boolean environment variable value.

        Parameters
        ----------
        name : str
            Environment variable name.
        default : bool
            Default value if not set.

        Returns
        -------
        bool
            Parsed boolean value.
        """
        val = os.environ.get(name)
        if val is None:
            return default
        return val.lower() in ("true", "1", "yes", "on")


class ConfigurationError(Exception):
    """Configuration loading error (exit code 9)."""
    pass
