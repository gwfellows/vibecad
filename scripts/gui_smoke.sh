#!/usr/bin/env bash
# Browser tests of the GUI against throwaway copies of examples/:
#   tests/gui/smoke.js        main flows (open, edit, undo, sketch mode, rollback, dialogs, new part)
#   tests/gui/agent_panel.js  the agent panel, driven by a scripted agent (tests/gui/fake_agent_app.py; no model)
#   tests/gui/sketch_editor.js  drawing, constraining, dragging and dimensioning in a sketch (same server)
#   tests/gui/modeling.js     a part modelled by hand from an empty file: sketch, extrude, sketch on face, cut, revolve
#   scripts/gui_smoke.sh [screenshot_dir]      (ONLY=modeling,smoke to run a subset)
# Needs node with playwright (npm i -g playwright) and a Chromium it can find.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$PWD
WORK=$(mktemp -d)
SHOTS=${1:-$WORK/shots}
PORT=${PORT:-8791}
mkdir -p "$WORK/root" "$WORK/agent_root" "$SHOTS"
cp examples/*.vcad.json "$WORK/root/"

uv run vibecad-app --root "$WORK/root" --port "$PORT" >"$WORK/server.log" 2>&1 &
S1=$!
uv run python tests/gui/fake_agent_app.py --root "$WORK/agent_root" --port $((PORT + 1)) >"$WORK/agent_server.log" 2>&1 &
S2=$!
trap 'kill $S1 $S2 2>/dev/null; rm -rf "$WORK/root" "$WORK/agent_root"' EXIT
for p in "$PORT" $((PORT + 1)); do
  for _ in $(seq 1 60); do curl -sf -o /dev/null "http://127.0.0.1:$p/api/parts" && break; sleep 1; done
done

THREE=""
if ! curl -sf -o /dev/null https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js; then
  (cd "$WORK" && npm pack --silent three@0.170.0 >/dev/null && tar xzf three-0.170.0.tgz)  # CDN blocked: serve it locally
  THREE="$WORK/package"
fi

export NODE_PATH=$(npm root -g)
status=0
run() {  # ONLY=modeling,sketch_editor runs a subset
  [[ -z "${ONLY:-}" || ",$ONLY," == *",$1,"* ]] || return 0
  node "$REPO/tests/gui/$1.js" "http://127.0.0.1:$2" "$SHOTS" $THREE || status=1
}
run smoke "$PORT"
run modeling "$PORT"
run agent_panel $((PORT + 1))
run sketch_editor $((PORT + 1))
if [[ ",${ONLY:-}," == *",readme_shots,"* ]]; then  # README screenshots: ONLY=readme_shots scripts/gui_smoke.sh docs/img
  node "$REPO/tests/gui/readme_shots.js" "http://127.0.0.1:$PORT" "http://127.0.0.1:$((PORT + 1))" "$SHOTS" $THREE || status=1
fi
echo "screenshots: $SHOTS   server logs: $WORK/*.log"
exit $status
