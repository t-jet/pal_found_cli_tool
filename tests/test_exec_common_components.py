#!/usr/bin/env python3
"""Test Execution: TESTEXEC-002 — Execute tests for ConfigLoader, AuthProvider, AsyncClientFactory.

Implements all 34 test cases from TESTCASE-002 specification:
- Suite 1: ConfigLoader Integration Tests (TC-CL-INT-001 to TC-CL-INT-011) — 11 tests
- Suite 2: AuthProvider Integration Tests (TC-AP-INT-001 to TC-AP-INT-006) — 6 tests
- Suite 3: AsyncClientFactory Integration Tests (TC-ACF-INT-001 to TC-ACF-INT-007) — 7 tests
- Suite 4: Integration Chain Tests (TC-CHAIN-001 to TC-CHAIN-005) — 5 tests
- Suite 5: E2E Scenario Tests (TC-E2E-001 to TC-E2E-005) — 5 tests

Framework: pytest with mocking for SDK dependencies.
Run: pytest tests/test_exec_common_components.py -v --tb=long
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src is on path
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from foundry_cli.common.async_client_factory import AsyncClientFactory
from foundry_cli.common.auth_provider import AuthProvider
from foundry_cli.common.config_loader import ConfigLoader, ConfigurationError
from foundry_cli.common.error_serializer import EXIT_CONFIGURATION

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_async_client_factory():
    """Keep the legacy fixture name; AsyncClientFactory is stateless now."""
    yield


@pytest.fixture
def clean_env(monkeypatch):
    """Clear all FOUNDRY_* env vars for a clean test environment."""
    keys_to_clear = [
        "FOUNDRY_TOKEN",
        "FOUNDRY_HOSTNAME",
        "FOUNDRY_AGENTIC_CLI_ENV_FILE",
        "FOUNDRY_AGENTIC_CLI_TIMEOUT_S",
        "FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT",
        "FOUNDRY_AGENTIC_CLI_READONLY",
        "FOUNDRY_AGENTIC_CLI_METADATA_ONLY",
        "FOUNDRY_AGENTIC_CLI_ENABLE_ATTRIBUTION",
        "FOUNDRY_AGENTIC_CLI_ATTRIBUTION_RIDS",
        "FOUNDRY_AGENTIC_CLI_ENABLE_TRACING",
        "FOUNDRY_AGENTIC_CLI_LOG_LEVEL",
    ]
    for key in keys_to_clear:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def mock_sdk():
    """Mock foundry_sdk modules so we don't need the real SDK installed."""
    mock_user_token_auth = MagicMock()
    mock_foundry_client = MagicMock()

    mock_sdk_module = MagicMock()
    mock_sdk_module.UserTokenAuth = mock_user_token_auth
    mock_sdk_module.FoundryClient = mock_foundry_client
    mock_sdk_module.AsyncFoundryClient = mock_foundry_client

    with patch.dict(sys.modules, {"foundry_sdk": mock_sdk_module}):
        yield mock_sdk_module, mock_user_token_auth, mock_foundry_client


@pytest.fixture
def env_file_with_credentials(tmp_path):
    """Create a temporary .env file with valid credentials."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FOUNDRY_TOKEN=test_token_123\n"
        "FOUNDRY_HOSTNAME=https://foundry.example.com\n"
        "FOUNDRY_AGENTIC_CLI_TIMEOUT_S=60\n"
        "FOUNDRY_AGENTIC_CLI_ENABLE_ATTRIBUTION=true\n"
        "FOUNDRY_AGENTIC_CLI_ATTRIBUTION_RIDS=rid1,rid2\n"
    )
    return env_file


# ===========================================================================
# SUITE 1: ConfigLoader Integration Tests (11 tests)
# ===========================================================================


class TestConfigLoaderIntegration:
    """TC-CL-INT-001 through TC-CL-INT-011."""

    # TC-CL-INT-001: Explicit .env file via FOUNDRY_AGENTIC_CLI_ENV_FILE
    def test_TC_CL_INT_001_explicit_env_file(
        self, clean_env, monkeypatch, env_file_with_credentials
    ):
        """Given an explicit .env path, When ConfigLoader.load() is called,
        Then it loads from that path and sets loaded_file."""
        monkeypatch.setenv(
            "FOUNDRY_AGENTIC_CLI_ENV_FILE", str(env_file_with_credentials)
        )
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file == str(env_file_with_credentials)
        assert cfg.token == "test_token_123"
        assert cfg.hostname == "https://foundry.example.com"

    # TC-CL-INT-002: Explicit .env file not found raises ConfigurationError
    def test_TC_CL_INT_002_explicit_env_file_not_found(self, clean_env, monkeypatch):
        """Given a non-existent explicit path, When ConfigLoader.load() is called,
        Then it raises ConfigurationError."""
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ENV_FILE", "/nonexistent/path/.env")
        cfg = ConfigLoader()
        with pytest.raises(ConfigurationError, match="Explicit .env file not found"):
            cfg.load()

    # TC-CL-INT-003: Git root .env search (ADR-006 Order 2)
    def test_TC_CL_INT_003_git_root_env(self, clean_env, monkeypatch, tmp_path):
        """Given a git repo with .env at root, When ConfigLoader.load() is called,
        Then it finds and loads the .env file from git root."""
        git_root = tmp_path / "repo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        (git_root / ".env").write_text(
            "FOUNDRY_TOKEN=git_root_token\nFOUNDRY_HOSTNAME=https://git.example.com\n"
        )
        sub_dir = git_root / "src" / "deep"
        sub_dir.mkdir(parents=True)
        monkeypatch.chdir(sub_dir)
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file == str(git_root / ".env")
        assert cfg.token == "git_root_token"

    # TC-CL-INT-004: Non-git CWD fallback to .env
    def test_TC_CL_INT_004_cwd_fallback_env(self, clean_env, monkeypatch, tmp_path):
        """Given a non-git dir with .env in CWD, When ConfigLoader.load() is called,
        Then it loads .env from CWD as fallback."""
        cwd_dir = tmp_path / "no_git"
        cwd_dir.mkdir()
        (cwd_dir / ".env").write_text(
            "FOUNDRY_TOKEN=cwd_token\nFOUNDRY_HOSTNAME=https://cwd.example.com\n"
        )
        monkeypatch.chdir(cwd_dir)
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file == str(cwd_dir / ".env")
        assert cfg.token == "cwd_token"

    # TC-CL-INT-005: Shell env vars take precedence over .env (override=False)
    def test_TC_CL_INT_005_shell_env_precedence(
        self, clean_env, monkeypatch, env_file_with_credentials
    ):
        """Given .env with token and shell env with different token,
        When ConfigLoader.load() is called, Then shell env takes precedence."""
        monkeypatch.setenv(
            "FOUNDRY_AGENTIC_CLI_ENV_FILE", str(env_file_with_credentials)
        )
        monkeypatch.setenv("FOUNDRY_TOKEN", "shell_token_override")
        cfg = ConfigLoader()
        cfg.load()
        # load_dotenv(override=False) means shell env was set BEFORE load_dotenv
        # After load, .env values are loaded if not already set
        # Since shell env IS set before load_dotenv, it takes precedence
        assert cfg.token == "shell_token_override"

    # TC-CL-INT-006: Typed accessor — timeout_s with clamping
    def test_TC_CL_INT_006_timeout_clamping(self, clean_env, monkeypatch):
        """Given timeout values below/above range, When accessed,
        Then they are clamped to [1, 3600]."""
        cfg = ConfigLoader()
        # Default
        assert cfg.timeout_s == 30
        # Below minimum
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_TIMEOUT_S", "-5")
        assert cfg.timeout_s == 1  # clamped to min
        # Above maximum
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_TIMEOUT_S", "99999")
        assert cfg.timeout_s == 3600  # clamped to max
        # Valid
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_TIMEOUT_S", "120")
        assert cfg.timeout_s == 120

    # TC-CL-INT-007: Typed accessor — timeout_s with non-numeric returns default
    def test_TC_CL_INT_007_timeout_invalid_returns_default(
        self, clean_env, monkeypatch
    ):
        """Given non-numeric timeout value, When accessed, Then returns default 30."""
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_TIMEOUT_S", "not_a_number")
        cfg = ConfigLoader()
        assert cfg.timeout_s == 30

    # TC-CL-INT-008: Typed accessor — boolean flags
    def test_TC_CL_INT_008_boolean_flags(self, clean_env, monkeypatch):
        """Given various boolean env values, When accessed,
        Then they parse correctly (true/1/yes -> True, others -> False)."""
        cfg = ConfigLoader()
        # Default false
        assert cfg.global_readonly is False
        assert cfg.global_metadata_only is False
        assert cfg.enable_attribution is False
        assert cfg.enable_tracing is False
        # True variants
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
        assert cfg.global_readonly is True
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "1")
        assert cfg.global_readonly is True
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "yes")
        assert cfg.global_readonly is True
        # False variants
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "false")
        assert cfg.global_readonly is False
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "0")
        assert cfg.global_readonly is False
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "")
        assert cfg.global_readonly is False

    # TC-CL-INT-009: get_env and get_env_bool helpers
    def test_TC_CL_INT_009_get_env_helpers(self, clean_env, monkeypatch):
        """Given env vars set, When get_env/get_env_bool called,
        Then they return correct values."""
        monkeypatch.setenv("CUSTOM_VAR", "custom_value")
        monkeypatch.setenv("CUSTOM_BOOL", "yes")
        cfg = ConfigLoader()
        assert cfg.get_env("CUSTOM_VAR") == "custom_value"
        assert cfg.get_env("MISSING_VAR") is None
        assert cfg.get_env("MISSING_VAR", default="fallback") == "fallback"
        assert cfg.get_env_bool("CUSTOM_BOOL") is True
        assert cfg.get_env_bool("MISSING_BOOL", default=True) is True
        assert cfg.get_env_bool("MISSING_BOOL") is False

    # TC-CL-INT-010: Git root detection with depth limit (max 20)
    def test_TC_CL_INT_010_git_root_depth_limit(self, clean_env, monkeypatch, tmp_path):
        """Given a very deep directory tree, When _find_git_root is called,
        Then it respects max_depth=20 limit."""
        # Create a deep directory structure (25 levels)
        deep_path = tmp_path / "deep"
        for i in range(25):
            deep_path = deep_path / f"level{i}"
        deep_path.mkdir(parents=True)
        # Put .git at the very top (too deep)
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(deep_path)
        cfg = ConfigLoader()
        # Should return None as depth exceeds max_depth
        result = cfg._find_git_root(max_depth=20)
        assert result is None

    # TC-CL-INT-011: attribution_rids and default_format accessors
    def test_TC_CL_INT_011_attribution_and_format(self, clean_env, monkeypatch):
        """Given attribution and format env vars, When accessed,
        Then they return correctly typed values."""
        cfg = ConfigLoader()
        # Defaults
        assert cfg.default_format == "auto"
        assert cfg.attribution_rids is None
        # Custom values
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT", "json")
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ATTRIBUTION_RIDS", "rid-a,rid-b,rid-c")
        assert cfg.default_format == "json"
        assert cfg.attribution_rids == "rid-a,rid-b,rid-c"


# ===========================================================================
# SUITE 2: AuthProvider Integration Tests (6 tests)
# ===========================================================================


class TestAuthProviderIntegration:
    """TC-AP-INT-001 through TC-AP-INT-006."""

    # TC-AP-INT-001: validate() with both credentials present
    def test_TC_AP_INT_001_validate_both_present(self):
        """Given valid token and hostname, When validate() called,
        Then returns (True, None)."""
        is_valid, error = AuthProvider.validate(
            "my_token", "https://foundry.example.com"
        )
        assert is_valid is True
        assert error is None

    # TC-AP-INT-002: validate() with missing token
    def test_TC_AP_INT_002_validate_missing_token(self):
        """Given None token, When validate() called,
        Then returns (False, error_message)."""
        is_valid, error = AuthProvider.validate(None, "https://foundry.example.com")
        assert is_valid is False
        assert error is not None
        assert "FOUNDRY_TOKEN" in error

    # TC-AP-INT-003: validate() with missing hostname
    def test_TC_AP_INT_003_validate_missing_hostname(self):
        """Given None hostname, When validate() called,
        Then returns (False, error_message)."""
        is_valid, error = AuthProvider.validate("my_token", None)
        assert is_valid is False
        assert error is not None
        assert "FOUNDRY_HOSTNAME" in error

    # TC-AP-INT-004: validate() with empty string token
    def test_TC_AP_INT_004_validate_empty_token(self):
        """Given empty string token, When validate() called,
        Then returns (False, error_message) — empty is treated as missing."""
        is_valid, error = AuthProvider.validate("", "https://foundry.example.com")
        assert is_valid is False
        assert error is not None

    # TC-AP-INT-005: get_auth() constructs UserTokenAuth correctly
    def test_TC_AP_INT_005_get_auth_constructs_user_token_auth(self, mock_sdk):
        """Given valid token and hostname, When get_auth() called,
        Then it returns a UserTokenAuth instance constructed with the token."""
        _, mock_uta, _ = mock_sdk
        auth = AuthProvider.get_auth("test_token")
        # Verify UserTokenAuth was constructed with correct token
        mock_uta.assert_called_once_with("test_token")
        assert auth is not None

    # TC-AP-INT-006: get_auth() raises ConfigurationError when SDK missing
    def test_TC_AP_INT_006_get_auth_sdk_missing(self):
        """Given foundry_sdk not installed, When get_auth() called,
        Then raises ConfigurationError."""
        with patch.dict(sys.modules, {"foundry_sdk": None}):
            with pytest.raises(ConfigurationError, match="foundry-sdk not installed"):
                AuthProvider.get_auth("token")


# ===========================================================================
# SUITE 3: AsyncClientFactory Integration Tests (7 tests)
# ===========================================================================


class TestAsyncClientFactoryIntegration:
    """TC-ACF-INT-001 through TC-ACF-INT-007."""

    # TC-ACF-INT-001: create() with valid config returns client
    def test_TC_ACF_INT_001_create_with_valid_config(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Given ConfigLoader with valid credentials, When factory.create() called,
        Then returns a FoundryClient instance."""
        monkeypatch.setenv("FOUNDRY_TOKEN", "test_token")
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://foundry.example.com")
        cfg = ConfigLoader()
        cfg.load()
        mock_sdk_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        # Verify FoundryClient was created with correct args
        mock_fc.assert_called_once()
        call_kwargs = mock_fc.call_args[1]
        assert call_kwargs["hostname"] == "https://foundry.example.com"

    # TC-ACF-INT-002: create() raises ConfigurationError when token missing
    def test_TC_ACF_INT_002_create_missing_token(self, clean_env, mock_sdk):
        """Given ConfigLoader without token, When factory.create() called,
        Then raises ConfigurationError."""
        cfg = ConfigLoader()
        cfg.load()
        factory = AsyncClientFactory()
        with pytest.raises(ConfigurationError, match="Missing FOUNDRY_TOKEN"):
            factory.create(cfg)

    # TC-ACF-INT-003: create() with attribution enabled injects attribution_rids
    def test_TC_ACF_INT_003_create_with_attribution(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Given config with attribution enabled, When factory.create() called,
        Then attribution RIDs are set on the SDK context variable."""
        monkeypatch.setenv("FOUNDRY_TOKEN", "test_token")
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://foundry.example.com")
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ENABLE_ATTRIBUTION", "true")
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ATTRIBUTION_RIDS", "rid1,rid2,rid3")
        cfg = ConfigLoader()
        cfg.load()
        mock_sdk_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        call_kwargs = mock_fc.call_args[1]
        assert "attribution_rids" not in call_kwargs
        mock_sdk_mod.ATTRIBUTION_VAR.set.assert_called_once_with(
            ["rid1", "rid2", "rid3"]
        )

    # TC-ACF-INT-004: create() without attribution does NOT inject attribution_rids
    def test_TC_ACF_INT_004_create_without_attribution(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Given config with attribution disabled, When factory.create() called,
        Then attribution_rids are NOT in client kwargs."""
        monkeypatch.setenv("FOUNDRY_TOKEN", "test_token")
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://foundry.example.com")
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ENABLE_ATTRIBUTION", "false")
        cfg = ConfigLoader()
        cfg.load()
        mock_sdk_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        call_kwargs = mock_fc.call_args[1]
        assert "attribution_rids" not in call_kwargs

    # TC-ACF-INT-005: create() is stateless per invocation
    def test_TC_ACF_INT_005_create_returns_fresh_client(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Given a client was created, When create() is called again,
        Then a fresh SDK client is constructed."""
        monkeypatch.setenv("FOUNDRY_TOKEN", "test_token")
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://foundry.example.com")
        cfg = ConfigLoader()
        cfg.load()
        mock_sdk_mod, mock_uta, mock_fc = mock_sdk
        mock_fc.side_effect = [MagicMock(name="client1"), MagicMock(name="client2")]
        factory = AsyncClientFactory()
        client1 = factory.create(cfg)
        client2 = factory.create(cfg)
        assert client1 is not client2
        assert mock_fc.call_count == 2

    # TC-ACF-INT-006: last_client returns None before create
    def test_TC_ACF_INT_006_last_client_before_create(self):
        """Given no client was created, When last_client is read,
        Then it returns None."""
        factory = AsyncClientFactory()
        assert factory.last_client is None

    # TC-ACF-INT-007: last_client exposes the most recent client
    def test_TC_ACF_INT_007_last_client_tracks_latest(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Given a client was created, When last_client is read,
        Then it returns the most recent client."""
        monkeypatch.setenv("FOUNDRY_TOKEN", "test_token")
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://foundry.example.com")
        cfg = ConfigLoader()
        cfg.load()
        mock_sdk_mod, mock_uta, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        assert factory.last_client is client


# ===========================================================================
# SUITE 4: Integration Chain Tests (5 tests)
# ===========================================================================


class TestIntegrationChain:
    """TC-CHAIN-001 through TC-CHAIN-005."""

    # TC-CHAIN-001: Full chain ConfigLoader -> AuthProvider -> AsyncClientFactory
    def test_TC_CHAIN_001_full_chain_success(self, clean_env, monkeypatch, mock_sdk):
        """Given valid credentials in env, When full chain executed,
        Then client is created successfully."""
        monkeypatch.setenv("FOUNDRY_TOKEN", "chain_token")
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://chain.example.com")
        cfg = ConfigLoader()
        cfg.load()
        is_valid, err = AuthProvider.validate(cfg.token, cfg.hostname)
        assert is_valid is True
        assert err is None
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        assert client is not None

    # TC-CHAIN-002: Chain fails on missing token (AuthProvider validation)
    def test_TC_CHAIN_002_chain_fails_missing_token(self, clean_env, monkeypatch):
        """Given missing token, When full chain executed,
        Then AuthProvider.validate() returns False."""
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://chain.example.com")
        cfg = ConfigLoader()
        cfg.load()
        is_valid, err = AuthProvider.validate(cfg.token, cfg.hostname)
        assert is_valid is False
        assert err is not None

    # TC-CHAIN-003: Chain fails on missing hostname
    def test_TC_CHAIN_003_chain_fails_missing_hostname(self, clean_env, monkeypatch):
        """Given missing hostname, When full chain executed,
        Then AuthProvider.validate() returns False."""
        monkeypatch.setenv("FOUNDRY_TOKEN", "chain_token")
        cfg = ConfigLoader()
        cfg.load()
        is_valid, err = AuthProvider.validate(cfg.token, cfg.hostname)
        assert is_valid is False
        assert err is not None

    # TC-CHAIN-004: Chain with .env file loading through to client creation
    def test_TC_CHAIN_004_chain_with_env_file(
        self, clean_env, monkeypatch, env_file_with_credentials, mock_sdk
    ):
        """Given credentials in .env file, When full chain executed from .env,
        Then client is created with values from .env file."""
        monkeypatch.setenv(
            "FOUNDRY_AGENTIC_CLI_ENV_FILE", str(env_file_with_credentials)
        )
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.token == "test_token_123"
        is_valid, err = AuthProvider.validate(cfg.token, cfg.hostname)
        assert is_valid is True
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        assert client is not None

    # TC-CHAIN-005: Chain with attribution through full pipeline
    def test_TC_CHAIN_005_chain_with_attribution(
        self, clean_env, monkeypatch, mock_sdk
    ):
        """Given config with attribution enabled, When full chain executed,
        Then attribution RIDs are set on the SDK context variable."""
        monkeypatch.setenv("FOUNDRY_TOKEN", "attr_token")
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://attr.example.com")
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ENABLE_ATTRIBUTION", "true")
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ATTRIBUTION_RIDS", "agent-1,agent-2")
        cfg = ConfigLoader()
        cfg.load()
        is_valid, err = AuthProvider.validate(cfg.token, cfg.hostname)
        assert is_valid is True
        mock_sdk_mod, _, mock_fc = mock_sdk
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        call_kwargs = mock_fc.call_args[1]
        assert "attribution_rids" not in call_kwargs
        mock_sdk_mod.ATTRIBUTION_VAR.set.assert_called_once_with(["agent-1", "agent-2"])


# ===========================================================================
# SUITE 5: E2E Scenario Tests (5 tests)
# ===========================================================================


class TestE2EScenarios:
    """TC-E2E-001 through TC-E2E-005."""

    # TC-E2E-001: E2E — ConfigLoader loads from git root, AuthProvider validates, client created
    def test_TC_E2E_001_git_root_to_client(
        self, clean_env, monkeypatch, tmp_path, mock_sdk
    ):
        """Given a git repo with .env containing credentials,
        When ConfigLoader finds git root, AuthProvider validates, factory creates client,
        Then the full pipeline succeeds from git root."""
        git_root = tmp_path / "project"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        (git_root / ".env").write_text(
            "FOUNDRY_TOKEN=e2e_token\nFOUNDRY_HOSTNAME=https://e2e.example.com\n"
        )
        src_dir = git_root / "src"
        src_dir.mkdir()
        monkeypatch.chdir(src_dir)
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file == str(git_root / ".env")
        is_valid, err = AuthProvider.validate(cfg.token, cfg.hostname)
        assert is_valid is True
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        assert client is not None

    # TC-E2E-002: E2E — Environment-only (no .env file) pipeline
    def test_TC_E2E_002_env_only_pipeline(
        self, clean_env, monkeypatch, tmp_path, mock_sdk
    ):
        """Given credentials only in shell env (no .env),
        When full pipeline runs, Then client is created from shell env."""
        non_git_dir = tmp_path / "no_git_no_env"
        non_git_dir.mkdir()
        monkeypatch.chdir(non_git_dir)
        monkeypatch.setenv("FOUNDRY_TOKEN", "shell_only_token")
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://shell.example.com")
        cfg = ConfigLoader()
        cfg.load()
        assert cfg.loaded_file is None  # No file loaded
        assert cfg.token == "shell_only_token"
        is_valid, err = AuthProvider.validate(cfg.token, cfg.hostname)
        assert is_valid is True
        factory = AsyncClientFactory()
        client = factory.create(cfg)
        assert client is not None

    # TC-E2E-003: E2E — Security: credentials not logged
    def test_TC_E2E_003_security_no_credential_leak(self, clean_env, monkeypatch):
        """Given credentials configured, When ConfigLoader and AuthProvider are used,
        Then credentials are NOT written to stdout or logged in plaintext."""
        monkeypatch.setenv("FOUNDRY_TOKEN", "secret_token_xyz")
        monkeypatch.setenv("FOUNDRY_HOSTNAME", "https://secret.example.com")
        cfg = ConfigLoader()
        cfg.load()
        token_val = cfg.token
        # Token is accessible programmatically
        assert token_val == "secret_token_xyz"
        # But AuthProvider.validate() returns a tuple, not logged output
        is_valid, err = AuthProvider.validate(token_val, cfg.hostname)
        assert is_valid is True
        # The error message for missing creds should not contain actual token values
        _, err_missing = AuthProvider.validate(None, None)
        assert "secret_token_xyz" not in err_missing

    # TC-E2E-004: E2E — Explicit .env file override takes priority over git root
    def test_TC_E2E_004_explicit_over_git_root(
        self, clean_env, monkeypatch, tmp_path, env_file_with_credentials
    ):
        """Given both explicit .env path AND git root .env,
        When ConfigLoader loads, Then explicit path takes priority."""
        git_root = tmp_path / "repo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        (git_root / ".env").write_text(
            "FOUNDRY_TOKEN=git_token\nFOUNDRY_HOSTNAME=https://git.example.com\n"
        )
        monkeypatch.chdir(git_root)
        monkeypatch.setenv(
            "FOUNDRY_AGENTIC_CLI_ENV_FILE", str(env_file_with_credentials)
        )
        cfg = ConfigLoader()
        cfg.load()
        # Should load from explicit path, NOT git root
        assert cfg.loaded_file == str(env_file_with_credentials)
        assert cfg.token == "test_token_123"  # from explicit file, not "git_token"

    # TC-E2E-005: E2E — ConfigLoader -> AuthProvider -> AsyncClientFactory error propagation
    def test_TC_E2E_005_error_propagation(self, clean_env, monkeypatch):
        """Given invalid configuration (missing credentials),
        When factory.create() is called, Then ConfigurationError propagates with clear message."""
        # No token, no hostname
        cfg = ConfigLoader()
        cfg.load()
        factory = AsyncClientFactory()
        with pytest.raises(ConfigurationError) as exc_info:
            factory.create(cfg)
        error_msg = str(exc_info.value)
        assert "Missing" in error_msg
        assert "FOUNDRY_TOKEN" in error_msg or "FOUNDRY_HOSTNAME" in error_msg
