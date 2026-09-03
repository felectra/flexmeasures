#!/usr/bin/env bash
# Continuous MQTT -> FlexMeasures ingestion pipeline (labems-t2t).
#
# Subscribe-only: mosquitto_sub reads the local broker; the in-container python writes only the
# FlexMeasures database. Nothing is ever published to the broker.
#
# This script is the ExecStart of flexmeasures-ingest.service. It waits for the FM server container,
# copies the ingester in (no image change), then runs the subscribe -> ingest pipeline. `pipefail`
# makes a broker or exec failure exit non-zero, so systemd Restart=always brings the pipeline back.
set -euo pipefail

SERVER=rock3a_server_1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The compose service is oneshot (RemainAfterExit), so it "completes" while containers are still
# starting. Wait until the FM server container is actually running before feeding it.
until [ "$(podman inspect -f '{{.State.Running}}' "$SERVER" 2>/dev/null || true)" = "true" ]; do
  echo "[t2t] waiting for $SERVER to be running ..." >&2
  sleep 3
done

# Reconnect with backoff, do NOT fail fast. The hlj startup guard can hold the broker unavailable for
# ~60-90 s after a reboot (it waits for the .22 WiFi address and Tailscale before mosquitto binds).
# Wait for the broker port here rather than let mosquitto_sub exit on connection-refused and force a
# restart-loop through the whole window. This is a bare TCP reachability check: it opens and closes a
# socket, it never publishes.
BROKER_HOST=127.0.0.1
BROKER_PORT=1883
until (exec 3<>"/dev/tcp/${BROKER_HOST}/${BROKER_PORT}") 2>/dev/null; do
  echo "[t2t] waiting for broker ${BROKER_HOST}:${BROKER_PORT} (hlj may hold it ~60-90 s post-reboot) ..." >&2
  sleep 3
done
exec 3>&- 2>/dev/null || true

# Copy the versioned ingester and its pure-logic module into the container (idempotent; ephemeral, no
# image change). continuous_ingest.py imports t2t_core, and both land in /tmp so the import resolves.
podman cp "$HERE/t2t_core.py" "$SERVER:/tmp/t2t_core.py"
podman cp "$HERE/continuous_ingest.py" "$SERVER:/tmp/continuous_ingest.py"

# Subscribe-only stream -> in-container ingester. `-F '%j'` emits one JSON object per message, so a
# newline inside a payload cannot forge a topic; `python -u` keeps the heartbeat unbuffered.
mosquitto_sub -h 127.0.0.1 -t 'deye/#' -t 'jkbms/#' -F '%j' \
  | podman exec -i "$SERVER" python -u /tmp/continuous_ingest.py
