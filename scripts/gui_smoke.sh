#!/usr/bin/env bash
# Browser smoke test of the GUI (tests/gui/smoke.js) against a throwaway copy of examples/.
#   scripts/gui_smoke.sh [screenshot_dir]
# Needs node with playwright (npm i -g playwright) and a Chromium it can find.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$PWD
WORK=$(mktemp -d)
SHOTS=${1:-$WORK/shots}
PORT=${PORT:-8791}
mkdir -p "$WORK/root" "$SHOTS"
cp examples/*.vcad.json "$WORK/root/"

uv run vibecad-app --root "$WORK/root" --port "$PORT" >"$WORK/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null; rm -rf "$WORK/root"' EXIT
for _ in $(seq 1 60); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/parts" && break; sleep 1; done

THREE=""
if ! curl -sf -o /dev/null https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js; then
  (cd "$WORK" && npm pack --silent three@0.170.0 >/dev/null && tar xzf three-0.170.0.tgz)  # CDN blocked: serve it locally
  THREE="$WORK/package"
fi

NODE_PATH=$(npm root -g) node "$REPO/tests/gui/smoke.js" "http://127.0.0.1:$PORT" "$SHOTS" $THREE
echo "screenshots: $SHOTS   server log: $WORK/server.log"
