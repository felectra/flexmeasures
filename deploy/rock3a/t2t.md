# labems-t2t — continuous MQTT → FlexMeasures ingestion service

The one-shot `mqtt_ingest.py` (labems-3oh) captures a short window and exits.
`labems-t2t` turns that into a **continuous, reboot-surviving service** that feeds the 38 bound
sensors with no gaps while the stack is healthy, so the shadow scheduler (labems-sc9) and any later
work have real, continuously-updated inputs.

**Subscribe-only.**
The Python ingester instantiates no MQTT client; it reads one JSON object per line from a persistent
host-side `mosquitto_sub -F '%j'` and writes only the FlexMeasures database.
Nothing is ever published to the broker, and no command reaches the bridge, inverter, or BMS boards.

## Files (versioned in `deploy/rock3a/`)

| File | sha256 (short) | Role |
|---|---|---|
| `t2t_core.py` | `0d6efe4e10dd` | Pure, standard-library-only decision logic (framing, deny-list, staleness gate, line assembler, salvage, stamp, DB-error class). Unit-tested. |
| `continuous_ingest.py` | `c884163cc0a0` | Thin shell: owns the app context + DB, runs the loop, imports `t2t_core`. Self-hashes at startup. |
| `flexmeasures-ingest.sh` | `ad5c2e61bb6d` | Wrapper: sets a private empty XDG config, waits for the container and broker, copies both modules in, runs the subscribe→ingest pipeline. |
| `flexmeasures-ingest.service` | `ae7e5e0a976f` | systemd **user** unit `flexmeasures-ingest.service`: `Restart=always`, start-limit disabled, ordered after the compose stack. |
| `test_t2t_core.py` | — | Self-contained regression tests for `t2t_core` (`pytest deploy/rock3a/test_t2t_core.py`). |

Authoritative hashes: `sha256sum deploy/rock3a/t2t_core.py deploy/rock3a/continuous_ingest.py
deploy/rock3a/flexmeasures-ingest.sh deploy/rock3a/flexmeasures-ingest.service`; the ingester also
logs its own hash on every start.

## Design

- **JSON framing.** `mosquitto_sub -F '%j'` emits one JSON object per message (`topic`, `payload`,
  `retain`, `tst`); a newline inside a payload is escaped within the JSON string, so a payload can
  never be read as a second record and **forge a topic that slips past the deny-list**.
  A missing or malformed `retain` flag fails closed (treated as retained, so it is skipped and can
  never open the staleness gate).
- **Config-independent subscriber.** `mosquitto_sub` auto-reads `$XDG_CONFIG_HOME/mosquitto_sub`
  (else `~/.config/mosquitto_sub`), which could smuggle in `--remove-retained` (a PUBLISH),
  `--will-topic`, or `--pretty` (breaking the one-JSON-per-line framing). The wrapper points
  `XDG_CONFIG_HOME` at a private, user-owned directory — the per-user runtime dir (mode 0700, tmpfs),
  else one under `$HOME`, never `/tmp` and never `~/.config` — locks its mode to 0700, and removes any
  stray `mosquitto_sub` file in it, so no inherited config is ever read and the subscribe-only and
  framing guarantees do not depend on the host's config.
- **Deny-list (no exceptions).** `deye/battery/soc` and the whole `deye/bms/*` group are dropped
  before mapping, independent of what the DB binds (`skipped_forbidden`).
- **Account-scoped map.** Only the account-1 sensors' `source_topic`s are mapped; a duplicate topic
  within scope is logged and skipped, never a hard exit (a hard exit would be a permanent restart
  loop for a live service).
- **Timestamp on arrival, monotonic across restarts.** `event_start = belief_time = arrival time`
  (UTC); the per-sensor stamp is seeded from the newest stored belief for the `mqtt-ingest` source,
  so a restart or a backward clock step cannot collide with a stored belief on the primary key.
- **Batched commits, transient vs row vs systematic.** Beliefs commit every ~200 lines or ~10 s. A
  deterministic row error salvages the batch **row by row** (`commit_row_errors`), and a row that
  cannot be stored is counted as lost (`lost_beliefs`), never silently dropped. A transient
  DB/connection error — including a DBAPIError whose connection was invalidated — **keeps the batch**
  and, after a few backed-off retries, exits so systemd restarts it, so an outage never triggers
  row-by-row deletion of good data. If a whole batch is rejected deterministically (a **systematic**
  fault — a missing table or column, a permission error), the service **exits non-zero** instead of
  clearing it and reporting success, so the fault surfaces at once rather than as silent loss.
- **Circuit breaker.** One malformed line is contained, but after 100 *unexpected* per-line
  exceptions the service exits non-zero, so a systematic bug can't leave it "active" while rejecting
  every line.
- **Robust framing.** stdin is read byte by byte with its own line buffer; partial lines never block
  the timers, invalid UTF-8 is replaced, and an oversized unterminated frame is discarded up to the
  next delimiter.
- **Heartbeat.** Every 60 s the counters print to stderr → the journal. `committed` is the durable
  count; `ingested` is messages accepted for writing before the commit lands.

## The `data_age` staleness rule (elapsed-time, fail-closed)

Each JK-BMS publishes `jkbms/string_<x>/sensor/data_age/state`, the age of the last BLE read.
The gate stores, per string, the **last reported age and the monotonic time it was received**, and a
string is stale — its readings skipped (`skipped_stale`) — when any of these hold:

- no live (non-retained) `data_age` has been seen yet (fail closed on startup), or
- the **effective age** = reported age + seconds elapsed since it was received exceeds 60 s, or
- the last `data_age` observation is itself older than a 90 s horizon.

Because the gate ages with elapsed time, a **BLE dropout closes it even though `data_age` stops
arriving** — the exact failure the raw last-value latch missed.
Retained `data_age` messages are ignored (their original time is unknown), so a retained replay after
a reboot can never re-open a string on stale values; the string opens only on a fresh live reading.
An unparsable, `nan`, or negative `data_age` marks the string stale immediately.
Deye topics have no string prefix and are never gated.

## Reconnect and backoff (the hlj broker-down window)

The `labems-hlj` startup guard holds the broker unavailable for **~60–90 s after each reboot** (it
waits for the `.22` WiFi address and Tailscale before `mosquitto` binds).

- **Initial connect after a reboot:** `mosquitto_sub` exits if its *first* connect is refused, so the
  wrapper first waits for `127.0.0.1:1883` with a bare TCP reachability check (open+close a socket —
  it never publishes), retrying every 3 s. During the window the wrapper *blocks patiently* rather
  than churning restarts.
- **Mid-stream broker blips:** once connected, `mosquitto_sub` (libmosquitto `loop_forever`) **auto-
  reconnects** on a dropped broker, so a transient broker restart does **not** tear down the pipeline
  — Python simply sees a quiet gap and resumes. So "broker loss" is normally handled inside
  `mosquitto_sub`, not by a systemd restart.
- **Container / `podman exec` death:** if the server container or the exec dies, the pipeline exits
  and systemd restarts it; `StartLimitIntervalSec=0` (start-limit disabled) plus `Restart=always`,
  `RestartSec=5` mean the expected post-reboot restarts never trip systemd's default 5-in-10 s limit
  and permanently fail the unit.

**Recovery target ≤ 10 min per reboot** (in practice seconds-to-a-minute: the stack + hlj guard
settle in ~60–90 s, then the wrapper connects on its next 3 s poll and flow resumes).

## Reboot survival and ordering

`flexmeasures-ingest.service` is `Wants=`/`After=flexmeasures-pilot.service` (the rootless compose
stack, README §7).
Because that unit is `oneshot` (`RemainAfterExit`), it "completes" while containers are still
starting, so the wrapper additionally **waits for `rock3a_server_1` to be running**.
Lingering (already enabled) autostarts user services at boot without a login.

## Stop and data-loss semantics (honest)

On `systemctl stop`, systemd terminates the cgroup: `mosquitto_sub` dies, its pipe closes, and the
ingester reaches stdin EOF (SIGTERM is also caught, and both paths run the same final flush).
The pending batch is then committed — **unless the database is unavailable at that moment**, in which
case the in-flight batch is lost: the MQTT stream is QoS 0 and is **not** replayed, so those readings
do not come back on restart.
That in-flight batch is counted (`lost_beliefs`) and the process exits non-zero.
Be precise about a **prolonged** outage, though: while the database is down the service cannot store
anything, and because the stream is QoS 0, **every reading published during the outage is lost**, not
only the in-flight batch — `lost_beliefs` counts just the batch held at exit, not the whole outage.
Steady-state operation (database healthy) loses nothing.
`TimeoutStopSec=20` bounds the stop.

## Belief-storage growth — a known risk (owner decision)

At the bench cadence (deye ~1/60 s × 18 sensors, jkbms ~1/10–13 s × 20 sensors) the service writes
roughly **179 k beliefs/day ≈ 5.4 M/month**, order **~1 GB/month** including indexes, on the board's
**14.5 GB eMMC** (already partly used by the stack and the DB).
That is fine for the ≥24 h sc9 run and near-term work, but **unbounded growth will fill the eMMC in a
few months**.
Downsampling / retention is **not implemented here** (out of scope for build-only and unnecessary for
sc9); it should be an **owner decision and a likely follow-up bead** — e.g. a retention window on raw
beliefs, periodic downsampling to a coarser resolution, or moving the DB volume to larger storage.

## Logging and rotation

Logs go to the **user journal** (`SyslogIdentifier=fm-ingest`; the unit is
`flexmeasures-ingest.service`).
Read them by unit — `journalctl --user -u flexmeasures-ingest` (add `-f`) — or by identifier,
`journalctl --user -t fm-ingest`.
Rotation is journald's; the volume is tiny (~1 heartbeat line/minute).
On the small eMMC, bound the journal by setting `SystemMaxUse=` (e.g. `200M`) in
`/etc/systemd/journald.conf` (a one-time host change the owner makes).

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
loginctl show-user sd -p Linger        # expect Linger=yes
```

## Verify ≥15 min of continuous flow

```bash
# 1. Watch the heartbeat (every 60 s): ingested/committed climb, skipped_forbidden climbs (deny-list
#    active), skipped_stale rises only during a BLE dropout.
journalctl --user -u flexmeasures-ingest -f

# 2. After ~15 min: how many of the 38 account-1 sensors got fresh beliefs, from the mqtt-ingest source.
podman exec -i rock3a_db_1 psql -U fm_pilot -d fm_pilot -At -F '|' -c \
 "SELECT count(*) beliefs, count(DISTINCT tb.sensor_id) sensors, min(tb.event_start), max(tb.event_start)
  FROM timed_belief tb JOIN sensor s ON s.id=tb.sensor_id JOIN generic_asset ga ON ga.id=s.generic_asset_id
  JOIN data_source d ON d.id=tb.source_id
  WHERE ga.account_id=1 AND d.name='mqtt-ingest' AND tb.event_start > now() - interval '15 min';"
#    Expect sensors=38; 28 if one JK string is legitimately stale (its 10 sensors drop out), 18 if
#    both — cross-check against skipped_stale in the heartbeat.

# 3. Per-sensor largest gap (valid Postgres: lag() in a CTE, then max() in the outer aggregate).
#    Gaps should be within the polling period (deye ~60 s, jkbms 10-13 s).
podman exec -i rock3a_db_1 psql -U fm_pilot -d fm_pilot -At -F '|' -c \
 "WITH pts AS (
     SELECT tb.sensor_id, tb.event_start,
            tb.event_start - lag(tb.event_start) OVER (PARTITION BY tb.sensor_id ORDER BY tb.event_start) AS gap
     FROM timed_belief tb
     JOIN data_source d ON d.id=tb.source_id
     JOIN sensor s ON s.id=tb.sensor_id
     JOIN generic_asset ga ON ga.id=s.generic_asset_id
     WHERE ga.account_id=1 AND d.name='mqtt-ingest' AND tb.event_start > now() - interval '15 min')
  SELECT s.id, s.name, count(*) n, max(EXTRACT(EPOCH FROM pts.gap)) max_gap_s
  FROM pts JOIN sensor s ON s.id=pts.sensor_id GROUP BY s.id, s.name ORDER BY max_gap_s DESC NULLS LAST;"

# 4. Deny-list assertion (executable): no account-1 belief may exist on a sensor bound to a forbidden source.
podman exec -i rock3a_db_1 psql -U fm_pilot -d fm_pilot -At -c \
 "SELECT count(*) AS forbidden_beliefs
  FROM timed_belief tb
  JOIN sensor s ON s.id=tb.sensor_id
  JOIN generic_asset ga ON ga.id=s.generic_asset_id
  WHERE ga.account_id=1 AND (s.attributes->>'source_topic') ~ '^(deye/battery/soc|deye/bms/)';"
#    Must be 0 — an invariant (no account-1 sensor is bound to a forbidden topic). Also confirm
#    skipped_forbidden > 0 in the heartbeat, to see the deny-list actively dropping soc/bms lines.
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
 "SELECT min(tb.event_start) FROM timed_belief tb
  JOIN data_source d ON d.id=tb.source_id
  JOIN sensor s ON s.id=tb.sensor_id
  JOIN generic_asset ga ON ga.id=s.generic_asset_id
  WHERE ga.account_id=1 AND d.name='mqtt-ingest' AND tb.event_start > timestamptz '$(uptime -s)';"
```

## Tests

`pytest deploy/rock3a/test_t2t_core.py` runs the self-contained regression tests (import only
`t2t_core`, no app or DB): JSON framing incl. embedded-newline payload safety, the deny-list,
elapsed-time staleness (stale from elapsed time with no new `data_age`), retained-skip, monotonic-stamp
seeding, and DB-error classification.
Fail-first was verified by breaking the elapsed-expiry line, which turns
`test_stale_from_elapsed_time_without_new_data_age` red.

## STOP conditions

- **Pause:** `systemctl --user stop flexmeasures-ingest` — the subscriber is killed, Python sees EOF
  and commits its pending batch, then exits.
- **Disable permanently:** `systemctl --user disable --now flexmeasures-ingest`.
- **Do not apply during the Plan D window** (host change); wait until the coordinator/owner clears it.
- The service self-throttles when the stack or broker is down: the wrapper waits for both, and
  `Restart=always` retries — it never busy-spins, never publishes, and never writes anything but FM
  beliefs.

## Open decisions for YellowHeron / owner

- **Belief retention / downsampling** (above) — the main follow-up: unbounded growth fills the 14.5 GB
  eMMC in a few months.
- **Quadlet vs user unit** — a plain user unit is used (matches `flexmeasures-pilot.service`, no new
  tooling); a Quadlet adds no benefit, since the ingester runs *inside* the existing server container.
- **Script delivery** — the wrapper `podman cp`s both modules in at each start (idempotent, no image
  change, freeze-safe); a future refinement could mount `deploy/rock3a` via the compose file, but that
  needs a container recreation.
- **Journal cap** — setting `SystemMaxUse=` is a one-time host change to make alongside enabling the
  service.
