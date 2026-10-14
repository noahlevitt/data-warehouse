#!/usr/bin/env bash
# Run Customer Records on this computer without Docker (macOS / Linux). Needs Python 3.11+ and PostgreSQL.
#
# Usage:  ./run_local.sh            then open http://localhost:8501   (stop with Ctrl+C)
#         PORT=8600 ./run_local.sh  to use another port
# PostgreSQL on a Mac, once:  brew install postgresql@17 && brew services start postgresql@17
#   The app then creates its database (customer_records) by itself; DATABASE_URL in .env points elsewhere.
# First run: creates a virtual environment in .venv/ and installs requirements.txt into it (a minute or two),
# and copies .env.example to .env. Later runs start straight away, and install requirements.txt again only when
# it changed since the last install (e.g. after an update).
# App data (templates, import jobs, each import's files) is stored in ./data next to this script; the records,
# businesses, services and tags are in PostgreSQL.

# Stop on the first error, on unset variables and on failures inside pipes.
set -euo pipefail
# Work from the project folder, wherever the script is started from.
cd "$(dirname "$0")"
# One-time setup of an isolated Python environment for this project.
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
fi
# (Re)install the packages when requirements.txt is newer than the last install.
if [ ! -f .venv/.requirements-installed ] || [ requirements.txt -nt .venv/.requirements-installed ]; then
  .venv/bin/pip install -r requirements.txt
  touch .venv/.requirements-installed
fi
# Settings file: created from the template on first run (edit it for DATABASE_URL, the Claude key, …).
[ -f .env ] || cp .env.example .env
# Replace this shell with the web app so Ctrl+C stops it cleanly.
exec .venv/bin/streamlit run app.py --server.port "${PORT:-8501}"
