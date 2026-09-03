# labems-t2t — continuous MQTT → FlexMeasures ingestion service

The one-shot `mqtt_ingest.py` (labems-3oh) captures a short window and exits.
`labems-t2t` turns that into a **continuous, reboot-surviving service** that feeds the 38 bound
sensors without gaps, so the shadow scheduler (labems-sc9) and any later work have real,
continuously-updated inputs.

**Subscribe-only.**
The Python ingester instantiates no MQTT client; it reads `topic payload` lines from a persistent
host-side `mosquitto_sub -v` and writes only the FlexMeasures database.
Nothing is ever published to the broker, and no command reaches the bridge, inverter, or BMS boards.

## Files (versioned in `deploy/rock3a/`)

| File | sha256 (short) | Role |
|---|---|---|
| `continuous_ingest.py` | `b24b6a9a5e16` | The continuous ingester (runs inside `rock3a_server_1`). It self-hashes at startup and logs `sha256=…`. |
| `flexmeasures-ingest.sh` | `77de5c48eaba` | Wrapper: waits for the container and the broker, copies the ingester in, runs the subscribe→ingest pipeline. |
| `flexmeasures-ingest.service` | `ae7e5e0a976f` | systemd **user** unit `flexmeasures-ingest.service`: `Restart=always`, start-limit disabled, ordered after the compose stack. |

Authoritative hashes: `sha256sum deploy/rock3a/continuous_ingest.py deploy/rock3a/flexmeasures-ingest.sh
deploy/rock3a/flexmeasures-ingest.service`; the ingester also prints its own hash to the journal on
every start.

## Design

- **Reuses the 3oh guards.** Topic→sensor map from `attributes["source_topic"]`, one `mqtt-ingest`
  DataSource (shared with 3oh), the forbidden-source deny-list, the `math.isfinite` drop, and a
  per-sensor strictly-increasing arrival stamp.
- **Deny-list (no exceptions).** `deye/battery/soc` and the whole `deye/bms/*` group are dropped in
  the ingester before mapping or parsing, independent of what the DB binds (`skipped_forbidden`).
- **Timestamp on arrival.** Payloads carry no timestamp; `event_start = belief_time = arrival time`
  (UTC), matching the instantaneous (`event_resolution = 0`) sensors.
- **Monotonic stamp survives restarts.** On start, the per-sensor stamp is seeded from the newest
  stored belief for the `mqtt-ingest` source, so a restart or a backward clock step cannot produce a
  timestamp that collides with a stored belief on the `TimedBelief` primary key.
- **Batched commits with salvage.** Beliefs commit roughly every 200 lines or 10 s; a failed batch is
  rolled back and retried **row by row** (`commit_row_errors`), so one poisoned row never loses the
  rest, and nothing crashes the service.
- **Robust framing.** stdin is read byte by byte with its own line buffer, so partial lines never
  block the commit/heartbeat timers, invalid UTF-8 is replaced (never crashes), and an over-long
  unterminated blob is dropped rather than growing without bound.
- **Heartbeat.** Every 60 s the ingester prints its counters to stderr → the journal. `committed` is
  the durable count; `ingested` is lines accepted for writing (before the commit lands).

## The `data_age > 60 s` staleness rule (fail-closed)

Each JK-BMS publishes `jkbms/string_<x>/sensor/data_age/state`, the age of the last BLE read.
The ingester tracks the latest `data_age` per string and **fails closed**: a string is stale — its
readings skipped (`skipped_stale`) — until it reports a fresh `data_age ≤ 60`, and again whenever the
last-seen `data_age` exceeds 60 s, or is missing, `nan`, or negative.
The latch is updated from every `data_age` line before the gate and before the mapping step, so it is
correct even if messages arrive out of order (retained topics) and even if the `data_age` sensor were
unmapped.
This is a second line of defence beyond the `nan` drop (a dropped string also emits `nan`, which
`math.isfinite` rejects).
Deye topics have no string prefix and are never gated by this rule.

## Reconnect and backoff (the hlj broker-down window)

The `labems-hlj` startup guard holds the broker **unavailable for ~60–90 s after each reboot** (it
waits for the `.22` WiFi address and Tailscale before `mosquitto` binds).
The service must therefore reconnect with backoff, not fail after one attempt.
Three mechanisms make that explicit:

- **The wrapper waits for the broker.** Before starting the pipeline it polls `127.0.0.1:1883` with a
  bare TCP reachability check (open+close a socket — it never publishes), retrying every 3 s until the
  broker binds. So during the post-reboot window the wrapper *blocks patiently* rather than exiting.
- **The unit disables the start rate limit.** `StartLimitIntervalSec=0` in `[Unit]` means the expected
  restarts during the broker-down window can never trip systemd's default 5-in-10 s limit and
  permanently fail the unit. `Restart=always` with `RestartSec=5` paces retries (5 s apart) so they
  back off rather than hammer.
- **Broker death mid-stream is a clean exit.** If the broker drops while running, `mosquitto_sub`
  exits, `set -o pipefail` fails the pipeline, the wrapper exits, and systemd restarts it — which then
  waits for the broker again and resumes.

**Recovery target ≤ 10 min per reboot** (in practice seconds-to-a-minute: the stack + hlj guard settle
in ~60–90 s, then the wrapper connects on its next 3 s poll and flow resumes).

Note on prompt restart: if the *Python* side dies while `mosquitto_sub` stays connected, `bash`
notices the broken pipe only on the subscriber's next write; because the bench always publishes within
one polling period (deye ~60 s, jkbms ~10–13 s), that is detected within ≤ ~60 s — well inside the
recovery ceiling.

## Reboot survival and ordering

The pilot runs the whole compose project as the user service `flexmeasures-pilot.service` with
lingering enabled (README §7).
`flexmeasures-ingest.service` is `Wants=`/`After=flexmeasures-pilot.service`, so it starts after the
stack.
Because the compose unit is `oneshot` (`RemainAfterExit`), it "completes" while the containers are
still starting, so the wrapper additionally **waits for `rock3a_server_1` to be running** before
feeding it.
Lingering (already enabled) starts user services at boot without a login, so the ingester returns on
its own after a reboot.

## Stop semantics

The unit's main process is `bash`; the Python ingester runs inside the container via `podman exec`, so
systemd's `SIGTERM` is not guaranteed to reach it directly.
The **reliable** graceful path is stdin EOF: on stop, `mosquitto_sub` is killed, its pipe closes,
Python reaches EOF and commits its pending batch in the `finally` block.
Worst case (an abrupt kill mid-batch) loses at most one ≤10 s / ≤200-belief batch — re-populated by the
stream on restart — so no meaningful data is lost.
`TimeoutStopSec=20` bounds the stop.

## Logging and rotation

Logs go to the **user journal** (`SyslogIdentifier=fm-ingest`; the unit is
`flexmeasures-ingest.service`).
Read them by unit — `journalctl --user -u flexmeasures-ingest` (add `-f` to follow) — or by identifier,
`journalctl --user -t fm-ingest`.
Rotation is journald's: it caps its own store and vacuums old entries.
The volume is tiny — one heartbeat line per minute (~1.5 k lines/day) plus occasional error lines.
On the small eMMC, bound the journal by setting `SystemMaxUse=` (e.g. `200M`) in
`/etc/systemd/journald.conf` and running `journalctl --vacuum-size=200M` (a host-wide change the owner
makes once; not part of this service).

## Apply steps — RUN LATER, not executed here

Prerequisites: `mosquitto-clients` on the host (`command -v mosquitto_sub`), the branch pulled to
`~/flexmeasures`, and — because starting the service is a host change — **not during the Plan D
observation window** (apply only once the coordinator/owner clears it).

```bash
# On the board, as user sd:
cd ~/flexmeasures && git pull --ff-only
chmod +x deploy/rock3a/flexmeasures-ingest.sh
mkdir -p ~/.config/systemd/user
cp deploy/rock3a/flexmeasures-ingest.service ~/.config/systemd/user/flexmeasures-ingest.service
systemctl --user daemon-reload
systemctl --user enable --now flexmeasures-ingest.service
systemctl --user status flexmeasures-ingest.service
loginctl show-user sd -p Linger        # expect Linger=yes (already enabled in README §0)
```

## Verify ≥15 min of continuous flow

```bash
# 1. Watch the heartbeat (every 60 s); `ingested`/`committed` climb, skipped_forbidden climbs
#    (deny-list active), skipped_stale rises only during a BLE dropout.
journalctl --user -u flexmeasures-ingest -f

# 2. After ~15 min: all 38 account-1 sensors, from the mqtt-ingest source, have fresh beliefs.
podman exec -i rock3a_db_1 psql -U fm_pilot -d fm_pilot -At -F '|' -c \
 "SELECT count(*) beliefs, count(DISTINCT tb.sensor_id) sensors, min(tb.event_start), max(tb.event_start)
  FROM timed_belief tb JOIN sensor s ON s.id=tb.sensor_id JOIN generic_asset ga ON ga.id=s.generic_asset_id
  JOIN data_source d ON d.id=tb.source_id
  WHERE ga.account_id=1 AND d.name='mqtt-ingest' AND tb.event_start > now() - interval '15 min';"
#    Expect sensors=38 (or 36 if a string is legitimately stale — cross-check skipped_stale).

# 3. Per-sensor largest gap over the window (should be within the polling period: deye ~60 s, jkbms 10-13 s).
#    Sensors with 0-1 rows show as NULL and are themselves a red flag (a bound sensor got no data).
podman exec -i rock3a_db_1 psql -U fm_pilot -d fm_pilot -At -F '|' -c \
 "WITH w AS (SELECT tb.sensor_id, tb.event_start FROM timed_belief tb JOIN data_source d ON d.id=tb.source_id
             WHERE d.name='mqtt-ingest' AND tb.event_start > now() - interval '15 min')
  SELECT s.id, s.name,
         EXTRACT(EPOCH FROM max(w.event_start) OVER (PARTITION BY w.sensor_id)) IS NOT NULL AS has_data,
         max(EXTRACT(EPOCH FROM w.event_start - lag(w.event_start) OVER (PARTITION BY w.sensor_id ORDER BY w.event_start))) max_gap_s
  FROM w JOIN sensor s ON s.id=w.sensor_id GROUP BY s.id, s.name ORDER BY max_gap_s DESC NULLS FIRST;"

# 4. Deny-list holds: no belief exists on any forbidden topic (there is no such sensor; this is 0 by construction).
#    Confirm from the heartbeat that skipped_forbidden > 0 (soc + bms lines are arriving and being dropped).
```

## Prove autostart after a reboot (recovery time)

```bash
date -u +%Y-%m-%dT%H:%M:%SZ                                   # note the time, coordinate the window, then reboot
sudo reboot
# after it returns:
uptime -s                                                    # boot time
systemctl --user show flexmeasures-ingest -p ActiveEnterTimestamp
journalctl --user -u flexmeasures-ingest --since "$(uptime -s)" | grep '\[t2t\] started'   # first start after boot
# recovery time = first belief after boot vs boot time (target ≤ 10 min):
podman exec -i rock3a_db_1 psql -U fm_pilot -d fm_pilot -At -c \
 "SELECT min(tb.event_start) FROM timed_belief tb JOIN data_source d ON d.id=tb.source_id
  WHERE d.name='mqtt-ingest' AND tb.event_start > timestamptz '$(uptime -s)';"
```

## STOP conditions

- **Pause:** `systemctl --user stop flexmeasures-ingest` — the subscriber is killed, Python sees EOF and
  commits its pending batch, then exits.
- **Disable permanently:** `systemctl --user disable --now flexmeasures-ingest`.
- **Do not apply during the Plan D window** (host change); wait until the coordinator/owner clears it.
- The service self-throttles when the stack or broker is down: the wrapper waits for both, and
  `Restart=always` retries — it never busy-spins, never publishes, and never writes anything but FM
  beliefs.

## Open decisions for YellowHeron / owner

- **Quadlet vs user unit.** A plain user unit is used (matches the existing `flexmeasures-pilot.service`
  pattern, needs no new tooling); a Quadlet adds no benefit here, since the ingester runs *inside* the
  existing server container rather than as its own container.
- **Script delivery.** The wrapper `podman cp`s the ingester into the container at each start
  (idempotent, no image change, no container recreation — safe during a freeze). A future refinement
  could mount `deploy/rock3a` into the container via the compose file, but that needs a container
  recreation and is out of scope for build-only.
- **Journal cap on the small eMMC.** Setting `SystemMaxUse=` is a one-time host change the owner may
  want to make alongside enabling the service.
