#!/usr/bin/env bash
# labems-sc9 shadow peak-shaving cycle — one retrospective derived-load run.
#
# Computes and writes to the FlexMeasures database only: the derived site load and the battery
# schedule, atomically, on a retrospective 24 h window.
# It instantiates no MQTT client and sends nothing to the broker, the bridge, the inverter, or the
# BMS boards. Run by flexmeasures-shadow.timer.
set -uo pipefail

SERVER=rock3a_server_1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/flexmeasures-shadow"
mkdir -p "$LOGDIR"

# The compose unit is oneshot, so the container may still be starting after a boot.
# Wait for the FM server container before feeding it, rather than fail the run.
until [ "$(podman inspect -f '{{.State.Running}}' "$SERVER" 2>/dev/null || true)" = "true" ]; do
  echo "[sc9] waiting for $SERVER to be running ..." >&2
  sleep 3
done

# Record the repo commit the code came from (the container has no git; the host checkout does).
SHA="$(git -C "$HERE/../.." rev-parse HEAD 2>/dev/null || echo unknown)"

# Copy the versioned code and config into the container (idempotent; ephemeral, no image change).
podman cp "$HERE/sc9_core.py" "$SERVER:/tmp/sc9_core.py"
podman cp "$HERE/shadow_schedule.py" "$SERVER:/tmp/shadow_schedule.py"
podman cp "$HERE/shadow-g2-config.json" "$SERVER:/tmp/shadow-g2-config.json"

# Run one cycle; tee the summary to a durable host log (each run exits, so its output flushes).
podman exec -i "$SERVER" python /tmp/shadow_schedule.py \
  --config /tmp/shadow-g2-config.json --commit --prove-reproducible \
  --code-sha "$SHA" --log /tmp/shadow-g2-log.jsonl 2>&1 | tee -a "$LOGDIR/cycle.log"
rc=${PIPESTATUS[0]}

# Preserve the full JSONL run record durably too — the container /tmp is ephemeral across restarts.
podman exec "$SERVER" tail -n 1 /tmp/shadow-g2-log.jsonl >> "$LOGDIR/runs.jsonl" 2>/dev/null || true

exit "$rc"
