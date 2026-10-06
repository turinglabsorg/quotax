#!/usr/bin/env bash
# Unit tests for the Python backend (parsers, accounts, catalog). Standard library only.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -B -m unittest discover -s tests "$@"
