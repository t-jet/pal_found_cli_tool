# Palantir Foundry CLI

Palantir Foundry CLI provides command-line access to Foundry resource operations with shared configuration, authentication, retry handling, access-control guards, and structured output helpers.

## Package

- Python support: 3.11 and 3.12.
- Distribution: `pal_found_cli`.
- Public commands use the `pal-found-` prefix, for example `pal-found-datasets`.
- Runtime dependencies: `foundry-platform-sdk>=1.0.0` and `python-dotenv>=1.0.0`.
- PyPI long description source: this `README.md`.

## Install and use

Install the latest verified release from PyPI:

```bash
python -m pip install pal_found_cli
pal-found-datasets --help
```

Upgrade an existing installation with `python -m pip install --upgrade pal_found_cli`.
Each release is built from a `vX.Y.Z` tag, staged on Test PyPI, installed into a
clean environment, and smoke-checked before PyPI publication.

The conda recipe uses the same tag-derived version. After a channel release,
install with `conda install -c t-jet pal_found_cli`.

## Authentication and configuration

The CLI authenticates to Foundry with an API token and a target hostname, read
from a `.env` file or the environment. Before running any `pal-found-*` command,
configure credentials:

```bash
# Copy the template and fill in your values
cp .env.example .env
```

Set at minimum:

```dotenv
FOUNDRY_TOKEN=your_foundry_api_token_here
FOUNDRY_HOSTNAME=https://foundry.example.com
```

`FOUNDRY_TOKEN` is your Foundry API token (personal or service account) and
`FOUNDRY_HOSTNAME` is the URL of the Foundry instance you target. The `.env`
file is searched per ADR-006 (explicit path override via
`FOUNDRY_AGENTIC_CLI_ENV_FILE`, otherwise the repository root). Do not commit
`.env` to version control. Optional settings cover request timeout, output
format, access guards, retry, pagination, and tracing; see `.env.example` for
the full list.

Then verify the setup:

```bash
pal-found-datasets --help
```

This repository contains the installable CLI. The project is split into three
independently versioned repositories:

- [design and documentation](https://github.com/t-jet/pal_found_cli)
- [CLI tool](https://github.com/t-jet/pal_found_cli_tool)
- [skills](https://github.com/t-jet/pal_found_cli_skills)

Repository-split and rename design records live in the
[design repository](https://github.com/t-jet/pal_found_cli).

## Development

Install the project with development dependencies:

```bash
python -m pip install -e ".[dev]"
```

Run the main validation commands:

```bash
ruff check src tests
mypy src
pytest tests --cov=pal_found_cli --cov-report=term-missing --cov-report=xml
python -m build
twine check dist/*
```

The repository coverage gate is 80% branch coverage.
