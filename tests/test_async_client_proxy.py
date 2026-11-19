"""Proxy environment handling at the shared SDK client boundary."""

import asyncio
from unittest.mock import patch

import pytest

from pal_found_cli.common.async_client_factory import AsyncClientFactory
from pal_found_cli.common.config_loader import ConfigLoader, ConfigurationError


PROXY_KEYS = (
    "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "REQUEST_METHOD",
)


def _config(monkeypatch: pytest.MonkeyPatch, hostname: str = "unresolvable.invalid") -> ConfigLoader:
    for key in PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FOUNDRY_HOSTNAME", hostname)
    monkeypatch.setenv("FOUNDRY_TOKEN", "offline-token")
    cfg = ConfigLoader()
    cfg.load()
    return cfg


def _sdk_config(cfg: ConfigLoader):
    with patch("foundry_sdk.AsyncFoundryClient") as constructor:
        AsyncClientFactory().create(cfg)
    return constructor.call_args.kwargs.get("config")


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"HTTPS_PROXY": "http://upper.test:80"}, "http://upper.test:80"),
        ({"HTTPS_PROXY": "http://upper.test:80", "https_proxy": "http://lower.test:80"}, "http://lower.test:80"),
        ({"ALL_PROXY": "http://fallback.test:80"}, "http://fallback.test:80"),
        ({"https_proxy": "proxy.test:80"}, "http://proxy.test:80"),
    ],
)
def test_proxy_selection(monkeypatch, environment, expected):
    cfg = _config(monkeypatch)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert _sdk_config(cfg).proxies["https"] == expected


@pytest.mark.parametrize(
    ("hostname", "bypass", "expected"),
    [
        ("example.com", "example.com", True),
        ("sub.example.com", ".example.com", True),
        ("badexample.com", "example.com", False),
        ("example.com:8443", "example.com:8443", True),
        ("example.com:8443", "example.com:443", False),
        ("127.0.0.1", "127.0.0.1", True),
        ("127.0.0.2", "127.0.0.1", False),
        ("example.com", "*", True),
    ],
)
def test_no_proxy_host_rules(monkeypatch, hostname, bypass, expected):
    cfg = _config(monkeypatch, hostname)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:80")
    monkeypatch.setenv("NO_PROXY", bypass)
    config = _sdk_config(cfg)
    assert (config is None) is expected


def test_lowercase_no_proxy_wins(monkeypatch):
    cfg = _config(monkeypatch, "example.com")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:80")
    monkeypatch.setenv("NO_PROXY", "different.test")
    monkeypatch.setenv("no_proxy", "example.com")
    assert _sdk_config(cfg) is None


def test_no_proxy_retains_direct_client(monkeypatch):
    cfg = _config(monkeypatch)
    assert _sdk_config(cfg) is None


def test_http_proxy_only_does_not_proxy_https(monkeypatch):
    cfg = _config(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:80")
    assert _sdk_config(cfg).proxies == {"http": "http://proxy.test:80"}


def test_cgi_ignores_uppercase_http_proxy(monkeypatch):
    cfg = _config(monkeypatch)
    monkeypatch.setenv("REQUEST_METHOD", "GET")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:80")
    assert _sdk_config(cfg) is None


def test_invalid_proxy_does_not_reveal_credentials(monkeypatch):
    cfg = _config(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "ftp://user:secret@proxy.test:80")
    with pytest.raises(ConfigurationError, match="Invalid HTTPS proxy URL") as exc:
        AsyncClientFactory().create(cfg)
    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_https_request_reaches_local_proxy_without_target_dns(monkeypatch):
    cfg = _config(monkeypatch)
    seen = []

    async def handle(reader, writer):
        seen.append(await reader.readline())
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    try:
        client = AsyncClientFactory().create(cfg)
        with pytest.raises(Exception):
            await asyncio.wait_for(client.admin.User.get_current(), timeout=3)
    finally:
        server.close()
        await server.wait_closed()
    assert seen
    assert seen[0] == b"CONNECT unresolvable.invalid:443 HTTP/1.1\r\n"


@pytest.mark.asyncio
async def test_no_proxy_uses_direct_path(monkeypatch):
    cfg = _config(monkeypatch)
    seen = []

    async def handle(reader, writer):
        seen.append(await reader.readline())
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NO_PROXY", "unresolvable.invalid")
    try:
        client = AsyncClientFactory().create(cfg)
        with pytest.raises(Exception):
            await asyncio.wait_for(client.admin.User.get_current(), timeout=3)
    finally:
        server.close()
        await server.wait_closed()
    assert seen == []
