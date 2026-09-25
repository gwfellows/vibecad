#!/usr/bin/env bash
# One-time setup on macOS (Apple Silicon or Intel).
# planegcs has no macOS wheel, so uv compiles it from source (~30-60 s);
# Eigen and Boost are the only native dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
brew install eigen boost cmake uv

export CMAKE_PREFIX_PATH="$(brew --prefix)"
uv sync

uv run python -c "import build123d, planegcs; print('imports ok')"
uv run python core/spike.py
echo
echo "Setup done. To see the part:"
echo "  terminal 1: uv run python -m ocp_viewer      (open http://127.0.0.1:3939/viewer)"
echo "  terminal 2: uv run python core/spike.py --show --width 90"
