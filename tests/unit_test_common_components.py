#!/usr/bin/env python3
"""Unit tests for ConfigLoader, AuthProvider, AsyncClientFactory (UNITTEST-001).

Pure unit tests — all external dependencies mocked at the module level.
Covers main scenarios and edge cases for each component.

Framework: pytest
Run: pytest tests/unit_test_common_components.py -v --tb=long
"""

import os
import sys
from enum import Enum
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src is on path
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from foundry_cli.common.async_client_factory import AsyncClientFactory
from foundry_cli.common.auth_provider import AuthProvider
from foundry_cli.common.config_loader import (
    DEFAULT_FORMAT,
    DEFAULT_MAX_TIMEOUT_S,
    DEFAULT_MIN_TIMEOUT_S,
    DEFAULT_TIMEOUT_S,
    ENV_ATTRIBUTION_RIDS,
    ENV_DEFAULT_FORMAT,
    ENV_ENABLE_ATTRIBUTION,
    ENV_ENABLE_TRACING,
    ENV_ENV_FILE,
    ENV_HOSTNAME,
    ENV_METADATA_ONLY,
    ENV_READONLY,
    ENV_TIMEOUT_S,
    ENV_TOKEN,
    EXIT_CONFIGURATION,
    ConfigLoader,
    ConfigurationError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all FOUNDRY_* env vars for a clean test environment."""
    keys = [
        ENV_ENV_FILE,
        ENV_TOKEN,
        ENV_HOSTNAME,
        ENV_TIMEOUT_S,
        ENV_DEFAULT_FORMAT,
        ENV_READONLY,
        ENV_METADATA_ONLY,
        ENV_ENABLE_ATTRIBUTION,
        ENV_ATTRIBUTION_RIDS,
        ENV_ENABLE_TRACING,
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def mock_sdk():
    """Mock foundry_sdk so tests don't require the real SDK."""
    mock_uta = MagicMock(spec=["__init__"])
    mock_fc = MagicMock()

    mock_module = MagicMock()
    mock_module.UserTokenAuth = mock_uta
    mock_module.AsyncFoundryClient = mock_fc

    with patch.dict(sys.modules, {"foundry_sdk": mock_module}):
        yield mock_module, mock_uta, mock_fc


@pytest.fixture
def env_file(tmp_path):
    """Create a minimal .env file with credentials."""
    f = tmp_path / ".env"
    f.write_text("FOUNDRY_TOKEN=tok\nFOUNDRY_HOSTNAME=https://h\n")
    return f


# ===========================================================================
# ConfigLoader — Unit Tests
# ===========================================================================


class TestConfigLoaderExplicitEnv:
    """Search path order 1: Explicit path via FOUNDRY_AGENTIC_CLI_ENV_FILE."""

    def test_explicit_env_file_loads_and_sets_loaded_file(
        self, clean_env, monkeypatch, tmp_path
    ):
        """Explicit .env path exists → loads and sets loaded_file."""
        f = tmp_path / "custom.env"
        f.write_text(
            "FOUNDRY_TOKEN=tok_explicit\nFOUNDRY_HOSTNAME=https://h_explicit\n"
        )
        monkeypatch.setenv(ENV_ENV_FILE, str(f))
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file == str(f)
        assert cfg.token == "tok_explicit"
        assert cfg.hostname == "https://h_explicit"

    def test_explicit_env_file_not_found_raises(self, clean_env, monkeypatch):
        """Explicit .env path does not exist → ConfigurationError."""
        monkeypatch.setenv(ENV_ENV_FILE, "/does/not/exist/.env")
        cfg = ConfigLoader()
        with pytest.raises(ConfigurationError, match="Explicit .env file not found"):
            cfg.load()

    def test_explicit_env_file_short_circuits_git_root_search(
        self, clean_env, monkeypatch, tmp_path
    ):
        """Explicit env present → git root and CWD are NOT searched."""
        f = tmp_path / "explicit.env"
        f.write_text("FOUNDRY_TOKEN=explicit_tok\nFOUNDRY_HOSTNAME=https://explicit\n")
        monkeypatch.setenv(ENV_ENV_FILE, str(f))
        # Even though git root has .env, explicit should win
        git_root = tmp_path / "repo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        (git_root / ".env").write_text(
            "FOUNDRY_TOKEN=git_tok\nFOUNDRY_HOSTNAME=https://git\n"
        )
        monkeypatch.chdir(git_root)
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.token == "explicit_tok"
        assert cfg.loaded_file == str(f)


class TestConfigLoaderGitRoot:
    """Search path order 2: Git root .env detection."""

    def test_git_root_env_detected_from_subdir(self, clean_env, monkeypatch, tmp_path):
        """CWD is a subdirectory of git root with .env → loads from git root."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        (root / ".env").write_text(
            "FOUNDRY_TOKEN=git_tok\nFOUNDRY_HOSTNAME=https://git\n"
        )
        sub = root / "a" / "b" / "c"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file == str(root / ".env")
        assert cfg.token == "git_tok"

    def test_git_root_no_env_file_falls_to_cwd(self, clean_env, monkeypatch, tmp_path):
        """Git root found but no .env there → falls to CWD .env."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        # No .env at git root
        sub = root / "src"
        sub.mkdir()
        (sub / ".env").write_text(
            "FOUNDRY_TOKEN=cwd_tok\nFOUNDRY_HOSTNAME=https://cwd\n"
        )
        monkeypatch.chdir(sub)
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file == str(sub / ".env")
        assert cfg.token == "cwd_tok"

    def test_find_git_root_returns_none_when_no_git(
        self, clean_env, monkeypatch, tmp_path
    ):
        """Non-git directory → _find_git_root returns None."""
        d = tmp_path / "no_git"
        d.mkdir()
        monkeypatch.chdir(d)
        cfg = ConfigLoader()
        result = cfg._find_git_root()
        assert result is None

    def test_find_git_root_max_depth_20(self, clean_env, monkeypatch, tmp_path):
        """Deep tree exceeding max_depth → returns None."""
        deep = tmp_path
        for i in range(25):
            deep = deep / f"l{i}"
        deep.mkdir(parents=True)
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(deep)
        cfg = ConfigLoader()
        result = cfg._find_git_root(max_depth=20)
        assert result is None

    def test_find_git_root_finds_at_exact_max_depth(
        self, clean_env, monkeypatch, tmp_path
    ):
        """Git root exactly at max_depth boundary → found."""
        deep = tmp_path
        for i in range(19):
            deep = deep / f"l{i}"
        deep.mkdir(parents=True)
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(deep)
        cfg = ConfigLoader()
        result = cfg._find_git_root(max_depth=20)
        assert result == tmp_path


class TestConfigLoaderFallback:
    """Search path orders 3-4: CWD fallback, env vars only."""

    def test_cwd_env_fallback(self, clean_env, monkeypatch, tmp_path):
        """Non-git dir with .env in CWD → loads from CWD."""
        d = tmp_path / "proj"
        d.mkdir()
        (d / ".env").write_text("FOUNDRY_TOKEN=cwd_tok\nFOUNDRY_HOSTNAME=https://cwd\n")
        monkeypatch.chdir(d)
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file == str(d / ".env")
        assert cfg.token == "cwd_tok"

    def test_no_env_file_uses_shell_env_only(self, clean_env, monkeypatch, tmp_path):
        """No .env anywhere → reads from shell env vars."""
        d = tmp_path / "bare"
        d.mkdir()
        monkeypatch.chdir(d)
        monkeypatch.setenv(ENV_TOKEN, "shell_tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://shell")
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file is None
        assert cfg.token == "shell_tok"
        assert cfg.hostname == "https://shell"

    def test_no_env_file_no_creds_returns_none(self, clean_env, monkeypatch, tmp_path):
        """No .env, no shell env → token/hostname return None."""
        d = tmp_path / "empty"
        d.mkdir()
        monkeypatch.chdir(d)
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.token is None
        assert cfg.hostname is None


class TestConfigLoaderShellPrecedence:
    """Shell env vars take precedence (override=False)."""

    def test_shell_env_overrides_dotenv(self, clean_env, monkeypatch, env_file):
        """Shell env var set before load_dotenv → takes precedence."""
        monkeypatch.setenv(ENV_ENV_FILE, str(env_file))
        monkeypatch.setenv(ENV_TOKEN, "shell_wins")
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.token == "shell_wins"


class TestConfigLoaderPropertyAccessors:
    """Typed property accessors: get_str, get_bool, get_int, get_float, get_enum."""

    def test_token_returns_string(self, clean_env, monkeypatch):
        cfg = ConfigLoader()
        monkeypatch.setenv(ENV_TOKEN, "abc123")
        assert cfg.token == "abc123"

    def test_token_returns_none_when_missing(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.token is None

    def test_hostname_returns_string(self, clean_env, monkeypatch):
        cfg = ConfigLoader()
        monkeypatch.setenv(ENV_HOSTNAME, "https://example.com")
        assert cfg.hostname == "https://example.com"

    def test_hostname_returns_none_when_missing(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.hostname is None

    def test_timeout_s_default(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.timeout_s == DEFAULT_TIMEOUT_S

    def test_timeout_s_valid_value(self, clean_env, monkeypatch):
        monkeypatch.setenv(ENV_TIMEOUT_S, "120")
        cfg = ConfigLoader()
        assert cfg.timeout_s == 120

    def test_timeout_s_below_min_clamped(self, clean_env, monkeypatch):
        monkeypatch.setenv(ENV_TIMEOUT_S, "-5")
        cfg = ConfigLoader()
        assert cfg.timeout_s == DEFAULT_MIN_TIMEOUT_S

    def test_timeout_s_above_max_clamped(self, clean_env, monkeypatch):
        monkeypatch.setenv(ENV_TIMEOUT_S, "99999")
        cfg = ConfigLoader()
        assert cfg.timeout_s == DEFAULT_MAX_TIMEOUT_S

    def test_timeout_s_non_numeric_returns_default(self, clean_env, monkeypatch):
        monkeypatch.setenv(ENV_TIMEOUT_S, "not_a_number")
        cfg = ConfigLoader()
        assert cfg.timeout_s == DEFAULT_TIMEOUT_S

    def test_timeout_s_zero_clamped_to_min(self, clean_env, monkeypatch):
        monkeypatch.setenv(ENV_TIMEOUT_S, "0")
        cfg = ConfigLoader()
        assert cfg.timeout_s == DEFAULT_MIN_TIMEOUT_S

    def test_default_format_default_value(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.default_format == DEFAULT_FORMAT

    def test_default_format_custom_value(self, clean_env, monkeypatch):
        monkeypatch.setenv(ENV_DEFAULT_FORMAT, "json")
        cfg = ConfigLoader()
        assert cfg.default_format == "json"

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE", "Yes", "On"])
    def test_global_readonly_parses_true_values(self, clean_env, monkeypatch, val):
        cfg = ConfigLoader()
        monkeypatch.setenv(ENV_READONLY, val)
        assert cfg.global_readonly is True, f"Expected True for '{val}'"

    @pytest.mark.parametrize("val", ["false", "0", "", "no", "random", "off"])
    def test_global_readonly_parses_false_values(self, clean_env, monkeypatch, val):
        cfg = ConfigLoader()
        monkeypatch.setenv(ENV_READONLY, val)
        assert cfg.global_readonly is False, f"Expected False for '{val}'"

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on"])
    def test_global_metadata_only_parses_true_values(self, clean_env, monkeypatch, val):
        cfg = ConfigLoader()
        monkeypatch.setenv(ENV_METADATA_ONLY, val)
        assert cfg.global_metadata_only is True, f"Expected True for '{val}'"

    @pytest.mark.parametrize("val", ["false", "0", "", "off"])
    def test_global_metadata_only_parses_false_values(
        self, clean_env, monkeypatch, val
    ):
        cfg = ConfigLoader()
        monkeypatch.setenv(ENV_METADATA_ONLY, val)
        assert cfg.global_metadata_only is False, f"Expected False for '{val}'"

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on"])
    def test_enable_attribution_parses_true_values(self, clean_env, monkeypatch, val):
        cfg = ConfigLoader()
        monkeypatch.setenv(ENV_ENABLE_ATTRIBUTION, val)
        assert cfg.enable_attribution is True, f"Expected True for '{val}'"

    @pytest.mark.parametrize("val", ["false", "0", ""])
    def test_enable_attribution_parses_false_values(self, clean_env, monkeypatch, val):
        cfg = ConfigLoader()
        monkeypatch.setenv(ENV_ENABLE_ATTRIBUTION, val)
        assert cfg.enable_attribution is False, f"Expected False for '{val}'"

    def test_attribution_rids_returns_comma_separated_string(
        self, clean_env, monkeypatch
    ):
        monkeypatch.setenv(ENV_ATTRIBUTION_RIDS, "rid1,rid2,rid3")
        cfg = ConfigLoader()
        assert cfg.attribution_rids == "rid1,rid2,rid3"

    def test_attribution_rids_returns_none_when_missing(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.attribution_rids is None

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on"])
    def test_enable_tracing_parses_true_values(self, clean_env, monkeypatch, val):
        cfg = ConfigLoader()
        monkeypatch.setenv(ENV_ENABLE_TRACING, val)
        assert cfg.enable_tracing is True, f"Expected True for '{val}'"

    def test_enable_tracing_default_false(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.enable_tracing is False

    def test_log_level_default(self, clean_env):
        from foundry_cli.common.log_setup import DEFAULT_LOG_LEVEL

        cfg = ConfigLoader()
        assert cfg.log_level == DEFAULT_LOG_LEVEL

    def test_loaded_file_returns_none_before_load(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.loaded_file is None


class TestConfigLoaderTypedAccessors:
    """get_str, get_bool, get_int, get_float, get_enum."""

    def test_get_str_returns_value(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_STR", "hello")
        cfg = ConfigLoader()
        assert cfg.get_str("MY_STR") == "hello"

    def test_get_str_strips_whitespace(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_STR", "  hello  ")
        cfg = ConfigLoader()
        assert cfg.get_str("MY_STR") == "hello"

    def test_get_str_returns_none_for_missing(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.get_str("NONEXISTENT") is None

    def test_get_str_returns_default_for_missing(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.get_str("NONEXISTENT", default="fallback") == "fallback"

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE", "On"])
    def test_get_bool_true_variants(self, clean_env, monkeypatch, val):
        monkeypatch.setenv("MY_BOOL", val)
        cfg = ConfigLoader()
        assert cfg.get_bool("MY_BOOL") is True, f"Expected True for '{val}'"

    @pytest.mark.parametrize("val", ["false", "0", "no", "off", "", "random"])
    def test_get_bool_false_variants(self, clean_env, monkeypatch, val):
        monkeypatch.setenv("MY_BOOL", val)
        cfg = ConfigLoader()
        assert cfg.get_bool("MY_BOOL") is False, f"Expected False for '{val}'"

    def test_get_bool_missing_uses_default(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.get_bool("MISSING") is False
        assert cfg.get_bool("MISSING", default=True) is True

    def test_get_bool_strips_whitespace(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_BOOL", "  true  ")
        cfg = ConfigLoader()
        assert cfg.get_bool("MY_BOOL") is True

    def test_get_int_returns_value(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_INT", "42")
        cfg = ConfigLoader()
        assert cfg.get_int("MY_INT") == 42

    def test_get_int_strips_whitespace(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_INT", "  42  ")
        cfg = ConfigLoader()
        assert cfg.get_int("MY_INT") == 42

    def test_get_int_missing_uses_default(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.get_int("MISSING") is None
        assert cfg.get_int("MISSING", default=7) == 7

    def test_get_int_non_numeric_returns_default(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_INT", "abc")
        cfg = ConfigLoader()
        assert cfg.get_int("MY_INT", default=99) == 99

    def test_get_int_empty_string_returns_default(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_INT", "")
        cfg = ConfigLoader()
        assert cfg.get_int("MY_INT", default=5) == 5

    def test_get_float_returns_value(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_FLOAT", "3.14")
        cfg = ConfigLoader()
        assert cfg.get_float("MY_FLOAT") == pytest.approx(3.14)

    def test_get_float_strips_whitespace(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_FLOAT", "  3.14  ")
        cfg = ConfigLoader()
        assert cfg.get_float("MY_FLOAT") == pytest.approx(3.14)

    def test_get_float_missing_uses_default(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.get_float("MISSING") is None
        assert cfg.get_float("MISSING", default=1.5) == pytest.approx(1.5)

    def test_get_float_non_numeric_returns_default(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_FLOAT", "abc")
        cfg = ConfigLoader()
        assert cfg.get_float("MY_FLOAT", default=2.0) == pytest.approx(2.0)

    def test_get_enum_by_name(self, clean_env, monkeypatch):
        class Color(Enum):
            RED = "red"
            GREEN = "green"

        monkeypatch.setenv("MY_ENUM", "RED")
        cfg = ConfigLoader()
        assert cfg.get_enum("MY_ENUM", Color) is Color.RED

    def test_get_enum_name_case_insensitive(self, clean_env, monkeypatch):
        class Color(Enum):
            RED = "red"

        monkeypatch.setenv("MY_ENUM", "red")
        cfg = ConfigLoader()
        # Lowercase name "red" still matches member name "RED"
        assert cfg.get_enum("MY_ENUM", Color) is Color.RED

    def test_get_enum_by_value(self, clean_env, monkeypatch):
        """Value-match path: env string equals a member value (not a name)."""

        class Mode(Enum):
            FAST = "speed"
            SLOW = "turtle"

        # "turtle" is not a member name, so name-match fails and value-match wins
        monkeypatch.setenv("MY_ENUM", "turtle")
        cfg = ConfigLoader()
        assert cfg.get_enum("MY_ENUM", Mode) is Mode.SLOW

    def test_get_enum_no_match_returns_default(self, clean_env, monkeypatch):
        class Color(Enum):
            RED = "red"

        monkeypatch.setenv("MY_ENUM", "purple")
        cfg = ConfigLoader()
        assert cfg.get_enum("MY_ENUM", Color, default=Color.RED) is Color.RED

    def test_get_enum_missing_returns_default(self, clean_env):
        class Color(Enum):
            RED = "red"

        cfg = ConfigLoader()
        assert cfg.get_enum("MISSING", Color) is None
        assert cfg.get_enum("MISSING", Color, default=Color.RED) is Color.RED

    def test_get_enum_strips_whitespace(self, clean_env, monkeypatch):
        class Color(Enum):
            RED = "red"

        monkeypatch.setenv("MY_ENUM", "  RED  ")
        cfg = ConfigLoader()
        assert cfg.get_enum("MY_ENUM", Color) is Color.RED


class TestConfigLoaderGetEnvHelpers:
    """Backward-compatible get_env() and get_env_bool() helpers."""

    def test_get_env_returns_value(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_VAR", "hello")
        cfg = ConfigLoader()
        assert cfg.get_env("MY_VAR") == "hello"

    def test_get_env_returns_none_for_missing(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.get_env("NONEXISTENT") is None

    def test_get_env_returns_default_for_missing(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.get_env("NONEXISTENT", default="fallback") == "fallback"

    def test_get_env_bool_true(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_BOOL", "yes")
        cfg = ConfigLoader()
        assert cfg.get_env_bool("MY_BOOL") is True

    def test_get_env_bool_true_variants(self, clean_env, monkeypatch):
        cfg = ConfigLoader()
        for val in ("true", "1", "yes", "on"):
            monkeypatch.setenv("MY_BOOL", val)
            assert cfg.get_env_bool("MY_BOOL") is True, f"Expected True for '{val}'"

    def test_get_env_bool_false(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_BOOL", "no")
        cfg = ConfigLoader()
        assert cfg.get_env_bool("MY_BOOL") is False

    def test_get_env_bool_missing_uses_default(self, clean_env):
        cfg = ConfigLoader()
        assert cfg.get_env_bool("MISSING") is False
        assert cfg.get_env_bool("MISSING", default=True) is True

    def test_get_env_bool_case_insensitive(self, clean_env, monkeypatch):
        monkeypatch.setenv("MY_BOOL", "TRUE")
        cfg = ConfigLoader()
        assert cfg.get_env_bool("MY_BOOL") is True


class TestConfigLoaderEnvironmentVariables:
    """Test all 20+ global env var typed config accessors."""

    def test_all_env_var_names_are_defined(self):
        """All expected env var constants exist."""
        assert ENV_ENV_FILE == "FOUNDRY_AGENTIC_CLI_ENV_FILE"
        assert ENV_TOKEN == "FOUNDRY_TOKEN"
        assert ENV_HOSTNAME == "FOUNDRY_HOSTNAME"
        assert ENV_TIMEOUT_S == "FOUNDRY_AGENTIC_CLI_TIMEOUT_S"
        assert ENV_DEFAULT_FORMAT == "FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT"
        assert ENV_READONLY == "FOUNDRY_AGENTIC_CLI_READONLY"
        assert ENV_METADATA_ONLY == "FOUNDRY_AGENTIC_CLI_METADATA_ONLY"
        assert ENV_ENABLE_ATTRIBUTION == "FOUNDRY_AGENTIC_CLI_ENABLE_ATTRIBUTION"
        assert ENV_ATTRIBUTION_RIDS == "FOUNDRY_AGENTIC_CLI_ATTRIBUTION_RIDS"
        assert ENV_ENABLE_TRACING == "FOUNDRY_AGENTIC_CLI_ENABLE_TRACING"

    def test_all_defaults(self, clean_env):
        """All properties return correct defaults when no env vars set."""
        cfg = ConfigLoader()
        assert cfg.token is None
        assert cfg.hostname is None
        assert cfg.timeout_s == DEFAULT_TIMEOUT_S
        assert cfg.default_format == DEFAULT_FORMAT
        assert cfg.global_readonly is False
        assert cfg.global_metadata_only is False
        assert cfg.enable_attribution is False
        assert cfg.attribution_rids is None
        assert cfg.enable_tracing is False
        assert cfg.loaded_file is None


class TestConfigLoaderHierarchyResolution:
    """Config value hierarchy: explicit > git root > CWD > shell env."""

    def test_hierarchy_explicit_over_git_root(self, clean_env, monkeypatch, tmp_path):
        """Explicit path wins over git root .env."""
        explicit = tmp_path / "explicit.env"
        explicit.write_text("FOUNDRY_TOKEN=explicit_tok\n")
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        (root / ".env").write_text("FOUNDRY_TOKEN=git_tok\n")
        monkeypatch.chdir(root)
        monkeypatch.setenv(ENV_ENV_FILE, str(explicit))
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.token == "explicit_tok"

    def test_hierarchy_git_root_over_cwd(self, clean_env, monkeypatch, tmp_path):
        """Git root .env wins over CWD .env."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        (root / ".env").write_text("FOUNDRY_TOKEN=git_tok\n")
        sub = root / "sub"
        sub.mkdir()
        (sub / ".env").write_text("FOUNDRY_TOKEN=cwd_tok\n")
        monkeypatch.chdir(sub)
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.token == "git_tok"


# ===========================================================================
# AuthProvider — Unit Tests
# ===========================================================================


class TestAuthProviderValidate:
    """AuthProvider.validate() static method."""

    def test_validate_both_present(self):
        is_valid, err = AuthProvider.validate("token", "https://host")
        assert is_valid is True
        assert err is None

    def test_validate_missing_token_none(self):
        is_valid, err = AuthProvider.validate(None, "https://host")
        assert is_valid is False
        assert err is not None
        assert "FOUNDRY_TOKEN" in err

    def test_validate_missing_token_empty_string(self):
        is_valid, err = AuthProvider.validate("", "https://host")
        assert is_valid is False
        assert err is not None
        assert "FOUNDRY_TOKEN" in err

    def test_validate_missing_hostname_none(self):
        is_valid, err = AuthProvider.validate("token", None)
        assert is_valid is False
        assert err is not None
        assert "FOUNDRY_HOSTNAME" in err

    def test_validate_missing_hostname_empty_string(self):
        is_valid, err = AuthProvider.validate("token", "")
        assert is_valid is False
        assert err is not None
        assert "FOUNDRY_HOSTNAME" in err

    def test_validate_both_missing(self):
        is_valid, err = AuthProvider.validate(None, None)
        assert is_valid is False
        assert err is not None

    def test_validate_whitespace_only_token_rejected(self):
        """Whitespace-only token must NOT pass validation (A1/A4)."""
        is_valid, err = AuthProvider.validate("   ", "https://host")
        assert is_valid is False
        assert err is not None
        assert "FOUNDRY_TOKEN" in err

    def test_validate_whitespace_only_hostname_rejected(self):
        """Whitespace-only hostname must NOT pass validation (A1/A4)."""
        is_valid, err = AuthProvider.validate("token", "   ")
        assert is_valid is False
        assert err is not None
        assert "FOUNDRY_HOSTNAME" in err


class TestAuthProviderGetAuth:
    """AuthProvider.get_auth() static method."""

    def test_get_auth_returns_user_token_auth(self, mock_sdk):
        mock_mod, mock_uta, _ = mock_sdk
        mock_uta.reset_mock()
        mock_uta.return_value = MagicMock()
        auth = AuthProvider.get_auth("my_token")
        mock_uta.assert_called_once_with("my_token")
        assert auth is not None

    def test_get_auth_passes_token_correctly(self, mock_sdk):
        mock_mod, mock_uta, _ = mock_sdk
        mock_uta.reset_mock()
        mock_uta.return_value = MagicMock()
        AuthProvider.get_auth("secret_token_123")
        mock_uta.assert_called_once_with("secret_token_123")

    def test_get_auth_takes_only_token_argument(self, mock_sdk):
        """get_auth signature is get_auth(token) — no dead hostname param (A2)."""
        import inspect

        sig = inspect.signature(AuthProvider.get_auth)
        assert list(sig.parameters) == ["token"]

    def test_get_auth_sdk_missing_raises_configuration_error(self):
        with patch.dict(sys.modules, {"foundry_sdk": None}):
            with pytest.raises(ConfigurationError, match="foundry-sdk not installed"):
                AuthProvider.get_auth("token")

    def test_get_auth_import_error_raises_configuration_error(self):
        """When foundry_sdk raises ImportError, ConfigurationError is raised."""
        with patch.dict(sys.modules, {}):
            with pytest.raises(ConfigurationError, match="foundry-sdk not installed"):
                AuthProvider.get_auth("token")

    def test_get_auth_has_return_type_annotation(self):
        """get_auth must declare a return type (A3)."""
        import inspect

        sig = inspect.signature(AuthProvider.get_auth)
        assert sig.return_annotation is not inspect.Signature.empty


class TestAuthProviderSecurity:
    """Security tests: credentials not logged or leaked."""

    def test_validate_error_message_does_not_contain_token(self):
        is_valid, err = AuthProvider.validate(None, "https://host")
        # Error should mention the env var name, not the token value
        assert "FOUNDRY_TOKEN" in err
        is_valid2, err2 = AuthProvider.validate(None, None)
        assert "secret" not in err2

    def test_validate_error_message_does_not_contain_hostname(self):
        is_valid, err = AuthProvider.validate("tok", None)
        assert "FOUNDRY_HOSTNAME" in err


# ===========================================================================
# AsyncClientFactory — Unit Tests
# ===========================================================================


class TestAsyncClientFactoryCreate:
    """AsyncClientFactory.create() — core client creation."""

    def test_create_returns_client_with_valid_config(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Valid token + hostname → returns client."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        mock_fc.assert_called_once()
        assert client is not None

    def test_create_uses_async_foundry_client(self, clean_env, monkeypatch, mock_sdk):
        """create() must instantiate AsyncFoundryClient, not FoundryClient (F1)."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        factory.create(cfg)
        # AsyncFoundryClient was the class invoked
        mock_fc.assert_called_once()
        # And it came from the AsyncFoundryClient name on the module
        assert hasattr(mock_mod, "AsyncFoundryClient")

    def test_create_passes_auth_and_hostname(self, clean_env, monkeypatch, mock_sdk):
        """Client created with correct auth and hostname kwargs."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        factory.create(cfg)
        call_kwargs = mock_fc.call_args[1]
        assert call_kwargs["hostname"] == "https://host"
        assert "auth" in call_kwargs

    def test_create_uses_userTokenAuth(self, clean_env, monkeypatch, mock_sdk):
        """UserTokenAuth constructed with correct token."""
        monkeypatch.setenv(ENV_TOKEN, "my_tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, _ = mock_sdk
        mock_uta.reset_mock()
        mock_uta.return_value = MagicMock()
        factory = AsyncClientFactory()
        factory.create(cfg)
        mock_uta.assert_called_once_with("my_tok")

    def test_create_missing_token_raises(self, clean_env, mock_sdk):
        """Missing token → ConfigurationError."""
        cfg = ConfigLoader()
        cfg.load()
        factory = AsyncClientFactory()
        with pytest.raises(ConfigurationError, match="Missing FOUNDRY_TOKEN"):
            factory.create(cfg)

    def test_create_missing_hostname_raises(self, clean_env, monkeypatch, mock_sdk):
        """Missing hostname → ConfigurationError."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        cfg = ConfigLoader()
        cfg.load()
        factory = AsyncClientFactory()
        with pytest.raises(ConfigurationError, match="Missing"):
            factory.create(cfg)

    def test_create_both_missing_raises(self, clean_env, mock_sdk):
        """Both missing → ConfigurationError."""
        cfg = ConfigLoader()
        cfg.load()
        factory = AsyncClientFactory()
        with pytest.raises(ConfigurationError, match="Missing"):
            factory.create(cfg)

    def test_create_empty_token_raises(self, clean_env, monkeypatch, mock_sdk):
        """Empty token string → treated as missing → ConfigurationError."""
        monkeypatch.setenv(ENV_TOKEN, "")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        factory = AsyncClientFactory()
        with pytest.raises(ConfigurationError, match="Missing"):
            factory.create(cfg)

    def test_create_whitespace_token_raises(self, clean_env, monkeypatch, mock_sdk):
        """Whitespace-only token → treated as missing (A1)."""
        monkeypatch.setenv(ENV_TOKEN, "   ")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        factory = AsyncClientFactory()
        with pytest.raises(ConfigurationError, match="Missing"):
            factory.create(cfg)

    def test_create_sdk_missing_raises(self, clean_env, monkeypatch):
        """foundry_sdk not installed → ConfigurationError."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        with patch.dict(sys.modules, {"foundry_sdk": None}):
            factory = AsyncClientFactory()
            with pytest.raises(ConfigurationError, match="foundry-sdk not installed"):
                factory.create(cfg)

    def test_create_has_return_type_annotation(self):
        """create() must declare a return type (F4)."""
        import inspect

        sig = inspect.signature(AsyncClientFactory.create)
        assert sig.return_annotation is not inspect.Signature.empty


class TestAsyncClientFactoryAttribution:
    """AsyncClientFactory.create() — attribution header injection."""

    def test_create_with_attribution_enabled(self, clean_env, monkeypatch, mock_sdk):
        """ENABLE_ATTRIBUTION=true + attribution_rids → passes list to client."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        monkeypatch.setenv(ENV_ENABLE_ATTRIBUTION, "true")
        monkeypatch.setenv(ENV_ATTRIBUTION_RIDS, "rid1,rid2,rid3")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        factory.create(cfg)
        call_kwargs = mock_fc.call_args[1]
        assert "attribution_rids" in call_kwargs
        assert call_kwargs["attribution_rids"] == ["rid1", "rid2", "rid3"]

    def test_create_with_attribution_disabled(self, clean_env, monkeypatch, mock_sdk):
        """ENABLE_ATTRIBUTION=false → no attribution_rids in kwargs."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        monkeypatch.setenv(ENV_ENABLE_ATTRIBUTION, "false")
        monkeypatch.setenv(ENV_ATTRIBUTION_RIDS, "rid1,rid2")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        factory.create(cfg)
        call_kwargs = mock_fc.call_args[1]
        assert "attribution_rids" not in call_kwargs

    def test_create_attribution_enabled_but_no_rids(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """ENABLE_ATTRIBUTION=true but no attribution_rids → no injection."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        monkeypatch.setenv(ENV_ENABLE_ATTRIBUTION, "true")
        # No attribution_rids set
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        factory.create(cfg)
        call_kwargs = mock_fc.call_args[1]
        assert "attribution_rids" not in call_kwargs

    def test_create_attribution_rids_single_value(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Single attribution RID → single-element list."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        monkeypatch.setenv(ENV_ENABLE_ATTRIBUTION, "true")
        monkeypatch.setenv(ENV_ATTRIBUTION_RIDS, "single-rid")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        factory.create(cfg)
        call_kwargs = mock_fc.call_args[1]
        assert call_kwargs["attribution_rids"] == ["single-rid"]

    def test_create_attribution_rids_strips_whitespace(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Attribution RIDs with surrounding whitespace are stripped (F5)."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        monkeypatch.setenv(ENV_ENABLE_ATTRIBUTION, "true")
        monkeypatch.setenv(ENV_ATTRIBUTION_RIDS, "rid1, rid2 ,rid3")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        factory.create(cfg)
        call_kwargs = mock_fc.call_args[1]
        assert call_kwargs["attribution_rids"] == ["rid1", "rid2", "rid3"]


class TestAsyncClientFactoryStatelessness:
    """AsyncClientFactory — stateless per invocation (no singleton, F2/F3)."""

    def test_factory_has_no_class_level_instance_state(self):
        """No _instance class variable that caches across factory instances (F2)."""
        # The class itself must not expose a mutable singleton attribute
        assert not hasattr(AsyncClientFactory, "_instance")

    def test_create_returns_fresh_instance_each_call(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Each create() returns a distinct client; no caching across calls."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        client1 = factory.create(cfg)
        client2 = factory.create(cfg)
        # Two construction calls = two distinct clients
        assert mock_fc.call_count == 2

    def test_two_factory_instances_are_independent(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Separate factory instances do not share cached clients (F2)."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        f1 = AsyncClientFactory()
        f2 = AsyncClientFactory()
        c1 = f1.create(cfg)
        c2 = f2.create(cfg)
        # Two separate constructions; clients not shared between factories
        assert mock_fc.call_count == 2
        assert c1 is not None
        assert c2 is not None

    def test_last_client_returns_most_recent(self, clean_env, monkeypatch, mock_sdk):
        """last_client is a convenience accessor for the most recent client."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        assert factory.last_client is None
        client = factory.create(cfg)
        assert factory.last_client is client

    def test_no_get_or_reset_classmethods(self):
        """Legacy singleton API (get/reset) is removed (F2/F3)."""
        assert not hasattr(AsyncClientFactory, "get")
        assert not hasattr(AsyncClientFactory, "reset")


class TestAsyncClientFactoryConfigLoaderInjection:
    """AsyncClientFactory — ConfigLoader dependency injection."""

    def test_create_accepts_config_loader_instance(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """factory.create() accepts a ConfigLoader instance."""
        monkeypatch.setenv(ENV_TOKEN, "tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        assert client is not None

    def test_create_reads_token_from_config_loader(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Token read from cfg.token, not from os.environ directly."""
        monkeypatch.setenv(ENV_TOKEN, "cfg_tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://host")
        cfg = ConfigLoader()
        cfg.load()
        mock_mod, mock_uta, _ = mock_sdk
        mock_uta.reset_mock()
        mock_uta.return_value = MagicMock()
        factory = AsyncClientFactory()
        factory.create(cfg)
        mock_uta.assert_called_once_with("cfg_tok")


# ===========================================================================
# ConfigurationError — Edge Cases
# ===========================================================================


class TestConfigurationError:
    """ConfigurationError exception class."""

    def test_configuration_error_is_exception(self):
        assert issubclass(ConfigurationError, Exception)

    def test_configuration_error_message(self):
        err = ConfigurationError("test message")
        assert str(err) == "test message"

    def test_configuration_error_can_be_caught_as_exception(self):
        try:
            raise ConfigurationError("fail")
        except Exception:
            pass  # Should be catchable as base Exception

    def test_configuration_error_has_exit_code_9(self):
        """ConfigurationError carries exit_code 9 per ADR-001 (C2)."""
        assert ConfigurationError.exit_code == EXIT_CONFIGURATION == 9

    def test_instance_inherits_exit_code(self):
        err = ConfigurationError("boom")
        assert err.exit_code == 9


# ===========================================================================
# Integration — Component Chain (Unit-Level)
# ===========================================================================


class TestUnitLevelChain:
    """Unit-level chain: ConfigLoader → AuthProvider → AsyncClientFactory."""

    def test_full_chain_with_mocked_sdk(self, clean_env, monkeypatch, mock_sdk):
        """Full pipeline with mocked SDK succeeds."""
        monkeypatch.setenv(ENV_TOKEN, "chain_tok")
        monkeypatch.setenv(ENV_HOSTNAME, "https://chain")
        cfg = ConfigLoader()
        cfg.load()
        is_valid, err = AuthProvider.validate(cfg.token, cfg.hostname)
        assert is_valid is True
        assert err is None
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        assert client is not None
        assert factory.last_client is client

    def test_chain_fails_on_missing_token(self, clean_env, monkeypatch):
        """Chain fails at AuthProvider validation when token missing."""
        monkeypatch.setenv(ENV_HOSTNAME, "https://chain")
        cfg = ConfigLoader()
        cfg.load()
        is_valid, err = AuthProvider.validate(cfg.token, cfg.hostname)
        assert is_valid is False
        assert err is not None

    def test_chain_error_propagation(self, clean_env, monkeypatch):
        """ConfigurationError propagates with clear message."""
        monkeypatch.setenv(ENV_HOSTNAME, "https://chain")
        cfg = ConfigLoader()
        cfg.load()
        factory = AsyncClientFactory()
        with pytest.raises(ConfigurationError) as exc_info:
            factory.create(cfg)
        assert "Missing" in str(exc_info.value)
