#!/bin/bash
# Start the demo: builds the data file if it is missing, then serves the app on the port and
# path prefix in .streamlit/config.toml (default http://localhost:8602/budget-oracle).
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "No .venv here. Create it with:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
[ -f .env ] || { echo "No .env — copy .env.example to .env first."; exit 1; }
[ -f data/gf_budgets.sqlite ] || python data/build_gf_budgets.py
python refresh_schema.py
exec streamlit run app.py
