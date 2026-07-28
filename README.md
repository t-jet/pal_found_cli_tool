# Foundry CLI

Foundry CLI provides command-line access to Foundry resource operations with shared configuration, authentication, retry handling, access-control guards, and structured output helpers.

## Package

- Python support: 3.11 and 3.12.
- Runtime dependencies: `foundry-platform-sdk>=1.0.0` and `python-dotenv>=1.0.0`.
- PyPI long description source: this `README.md`.

## Development

Install the project with development dependencies:

```bash
python -m pip install -e ".[dev]"
```

Run the main validation commands:

```bash
ruff check src tests
mypy src
pytest tests --cov=foundry_cli --cov-report=term-missing --cov-report=xml
python -m build
twine check dist/*
```

The repository coverage gate is 80% branch coverage. Current DEVOPS-002 validation measured 81.65% with 262 tests passing.
