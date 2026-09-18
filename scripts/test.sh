#!/bin/bash
# Run the test suite the way it has to be run on this machine: on a copy of the tree WITHOUT the
# production .env. python-dotenv loads .env at import and the fixtures then point at the Azure
# tables instead of the demo database (27 fixture errors at HEAD). Any pytest arguments pass through.
set -uo pipefail
cd "$(dirname "$0")/.."
here=$(pwd)
tmp=$(mktemp -d)
rsync -a --exclude .env --exclude .venv --exclude logs --exclude .git --exclude __pycache__ \
      --exclude .pytest_cache --exclude .claude ./ "$tmp/"
cd "$tmp" && "$here/.venv/bin/python" -m pytest -q "$@"
status=$?
rm -rf "$tmp"
exit $status
