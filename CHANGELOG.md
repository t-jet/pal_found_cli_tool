# Changelog

All notable changes to `pal_found_cli` are recorded here.

## [0.1.6] - 2026-11-19

### Fixed

- Honor standard proxy environment variables and bypass rules in SDK clients.
- Correct shared output parsing, timeout, retry and preview handling, including Data Health preview requests.
- Preserve required search filters across pages and keep Functions stream consumption within retry scope.
- Decode Filesystem positional JSON batch bodies and forward SQL query text through Arrow downloads.
- Correct Models config-version paths, Third Party Applications SDK parent routing and legacy Geotemporal SDK operations.
- Keep Conda build and test versions aligned with package metadata.

### Security

- Bound three temporary scanner-only filelock exceptions to the CI environment and expiry date.

## [0.1.5] - 2026-10-14

### Fixed

- Corrected Admin and Filesystem pagination and error handling.
- Validated Ontology inputs and clarified paginated response metadata.
- Corrected Functions attribution parsing and shared pagination behavior.

## [0.1.4] - 2026-10-06

### Added

- Added bounded Datasets pagination and related command coverage.
