# labems-sc9 — shadow storage scheduler (rubіж G2)

Compute-only, **retrospective** shadow scheduling for the ROCK 3A pilot.
FlexMeasures' `StorageScheduler` computes a battery schedule that shaves the peak of the **derived real
site load** over an observed past window, quantifies the peak reduction, journals the run
reproducibly, and **nothing is executed**: no broker publish, no command to the bridge/inverter/BMS,
no hardware change.
This is the G2 rung of `lab-ems-program.md` §6 ("EMS обчислює рішення, але не виконує їх").

## Files

| File | Role |
|---|---|
| `sc9_core.py` | Pure, stdlib-only logic (window math, continuous-run rule, P_load derivation, peak metric, reproducibility). Unit-tested. |
| `shadow_schedule.py` | The FM shell: derives the load, writes it, schedules, quantifies, journals. Compute-only unless `--commit`. |
| `shadow-g2-config.json` | Versioned objective + constraints + sign convention. |
| `seed-shadow-sensor.py` | Idempotent seed for the PT15M schedule **output** sensor on `BatteryBank` (id 45). Needs `--i-have-approval`. |
| `seed-site-load-sensor.py` | Idempotent seed for the derived **site-load** sensor on `Office` (id 5). Needs `--i-have-approval`. |
| `test_sc9_core.py` | Self-contained regression tests (`pytest deploy/rock3a/test_sc9_core.py`). |

## Why a derived load — the circularity finding (methodological evidence)

Peak-shaving directly against the behind-meter `deye/ac/total_grid_power` is **circular**: that signal
is the *net* grid exchange, which already nets whatever the battery does, so asking the scheduler to
shave it against itself yields **coverage ~0.01 and a near-trivial (essentially zero) schedule** — the
objective would be theater.
The honest inflexible load is the site's own consumption, independent of the battery's dispatch.

## Derived load and sign convention

- `deye/ac/total_grid_power` (sensor 7): **positive = import** from the grid.
- `deye/battery/power` (sensor 17): **positive = discharge**, negative = charge (verified: labems-3oh
  read −78 W while charging).
- PV is ~0 at this bench.
- **Energy balance:** `P_load = P_grid + P_batt` — e.g. import 540 + battery −78 (charging) = 462 W
  real load.

Both inputs are allowed `deye/*` sources (only `deye/battery/soc` and `deye/bms/*` are forbidden).
Only slots where **both** grid and battery have a value are derived (the intersection), so a battery
dropout lowers coverage instead of silently assuming a zero battery contribution.
The derived `P_load` is written to a dedicated **site-load sensor** on `Office` (kW, PT15M, marker
`sc9-site-load`, no `source_topic`), from a dedicated `sc9-derived` DataSource (never `mqtt-ingest`),
because FlexMeasures reads the scheduler's `inflexible-consumption` from a sensor.
The tool pins these identities and units at runtime (site-load on Office 5, `sc9-site-load`, kW, PT15M;
grid and battery in W), so a mis-set config cannot write onto the wrong sensor or misread the scale.

## Retrospective (backcast) window

There is no future load forecast (the net grid can't self-forecast), so the schedule is computed over
the **observed past**:

- `end = floor(now, resolution)`.
- `start` = the start of the **continuous t2t run** within the horizon — the earliest slot of the most
  recent unbroken run of grid data (gaps ≤ 2 resolutions), capped at `end − horizon`. This excludes
  the sparse pre-t2t labems-3oh samples, so the window is real, continuous data.
- The window **grows** from short (a few hours now) to the full 24 h as t2t accumulates telemetry.
- `expected_steps` is derived from the **actual** window `(end − start) / resolution`, so coverage is
  correct for a growing backcast (a fully-covered window → coverage ~1.0, the gate passes).

**Perfect-hindsight caveat (honest):** the shadow knows the whole window's load in advance, so the
reported peak reduction is a **potential upper bound**, not what a real-time EMS without a forecast
could guarantee.

## Value metric — the point

Over the window (all in kW):

- `peak_kw_before` = max observed grid import = `max(grid[t])`.
- shadow grid under the scheduled dispatch = `P_load[t] − batt_scheduled_discharge[t]` (discharging
  lowers import; the scheduler's output is consumption-positive, so discharge = −output).
- `peak_kw_after` = max shadow grid.
- `kw_shaved = peak_kw_before − peak_kw_after`; `pct_shaved = 100 · kw_shaved / peak_kw_before`.

These land in the per-run JSONL log (`peak_shaving`) and the stdout summary.

## Declared parameters — NOT measurements

Versioned in `shadow-g2-config.json`, echoed into every log record:

- **Bank capacity `18.0 kWh`** (nameplate/calc; `19.7 kWh` sensitivity; SOH unknown).
- **SOC-at-start `50 %`** — no trustworthy live SOC (`deye/battery/soc` forbidden; strings diverge).
- **SOC band `10–100 %`** — a modelling choice; the hard voltage window `44–57 V` is never widened.
- **Efficiency `95 % / 95 %`** — a declared round-trip efficiency, not a measurement.
- **Prices / peak nudge** — flat `50 EUR/MWh` and the `site-peak-consumption` baseline/price are
  internal nudges, not a tariff or a contracted limit.

## Safety design

- **Compute-only by default.** No MQTT client; startup provisioning disabled; on the non-commit path
  the session (including the flushed derived load) is rolled back, so it writes nothing.
- **Derived load is flush-then-decide.** `P_load` is written to the session and flushed so the
  scheduler reads it in-session; it is **committed only under `--commit`**, otherwise rolled back.
- **Deny-list enforced.** The inflexible sensor (site-load) and both derivation inputs (grid, battery)
  are refused if bound to `deye/battery/soc` or `deye/bms/*`.
- **`--commit` strictly gated.** Refuses unless the target is exactly `target_output_sensor_id` (45),
  on `BatteryBank` (asset 6), at `PT15M`, with the `sc9-shadow-schedule` marker, no `source_topic`,
  and coverage ≥ `min_input_coverage`. `--commit` cannot be combined with `--target-sensor`, so it can
  never write a measured sensor. A failed `--prove-reproducible` also blocks the commit.
- **Atomic write.** Under `--commit` the derived load and the schedule are persisted in **one
  transaction** (the derived load is flushed, then both are committed together), so a failure leaves
  neither behind. The schedule is stored from the already-computed series (consumption-positive, in
  the output sensor's unit), which deliberately avoids `make_schedule`'s separate commit (that would
  break atomicity with the derived-load write) and its `persist_flex_model` side effect (writing
  BatteryBank's SOC state).
- **Honest outcome.** `commit_attempted` vs `commit_succeeded`; `committed` is true only on a real write.

## How to run

Seed the two sensors once (owner-authorized), set their ids in the config, then run:

```bash
# One-time, owner-authorized (writes the FM DB):
podman exec -i rock3a_server_1 python - --i-have-approval < deploy/rock3a/seed-site-load-sensor.py
podman exec -i rock3a_server_1 python - --i-have-approval < deploy/rock3a/seed-shadow-sensor.py
# then set site_load_sensor_id (and confirm target_output_sensor_id=45) in shadow-g2-config.json.

# Compute-only (writes nothing):
podman cp deploy/rock3a/sc9_core.py rock3a_server_1:/tmp/sc9_core.py
podman cp deploy/rock3a/shadow_schedule.py rock3a_server_1:/tmp/shadow_schedule.py
podman cp deploy/rock3a/shadow-g2-config.json rock3a_server_1:/tmp/shadow-g2-config.json
podman exec -i rock3a_server_1 python /tmp/shadow_schedule.py \
    --config /tmp/shadow-g2-config.json --prove-reproducible --code-sha <repo SHA>

# Committing cycle (persists the derived load + the schedule):
podman exec -i rock3a_server_1 python /tmp/shadow_schedule.py \
    --config /tmp/shadow-g2-config.json --commit --prove-reproducible --code-sha <repo SHA>
```

- `--start`/`--belief-time` must be timezone-aware; a naive value is rejected.
- `--prove-reproducible` computes twice on the same window and **exits non-zero** unless the two
  schedules are identical and a real proof (non-empty, not all-NaN, exactly the **actual** window steps).

## Reproducibility method

`belief_time` is recorded on every run (default now; pin with `--belief-time`).
FlexMeasures reads only beliefs known as of `belief_time`, and the window is captured once, so the two
computes read the same inputs and the deterministic HiGHS solver returns an identical schedule.

## Bucketing the instantaneous inputs

The grid (7) and battery (17) sensors are `event_resolution = 0` (instantaneous), and FlexMeasures'
resolution-resample returns **nothing** for a 0-resolution sensor.
So the tool fetches the raw beliefs (pinned to the `mqtt-ingest` source, one deterministic belief per
event) and buckets them into PT15M slots in Python, taking the **mean power** per slot — the natural
kW-over-a-slot value.
`P_load` is then the intersection of the grid and battery slot means.

## Coverage gate

`input_coverage` is **slot occupancy**: the fraction of window slots that carry at least one bucketed
sample (not sampling density).
Below `min_input_coverage` (0.9) the run is `insufficient_input` and `--commit` is refused.
Because a small **recent** outage could stay under that threshold, a separate `stale_tail` guard also
refuses `--commit` when the newest derived slot is older than `end − 2·resolution`.
With continuous t2t ingestion the retrospective window is fully covered (~1.0) and the tail is fresh,
so both gates pass.

## Open items for YellowHeron / owner

- **Sensor boundary** — both new sensors live on existing assets (site-load on Office 5, schedule
  output on BatteryBank 6), within the "no new assets" precedent; flagged for confirmation.
- **Asset SOC state** — the commit path deliberately self-persists the schedule instead of calling
  `make_schedule`, so it does **not** run `persist_flex_model` and never writes BatteryBank's SOC
  state; a committed run touches only the derived-load and schedule beliefs on their dedicated sensors.
  This deviates from the "use make_schedule" wording in the spec, for atomicity — flagged for confirmation.

## Validation

`pytest deploy/rock3a/test_sc9_core.py` covers the window math (retrospective cap to earliest and to
horizon, actual `expected_steps`), the continuous-run rule (excludes sparse old samples), the P_load
sign/derivation, the peak metric (before/after/shaved), and the reproducibility check over the actual
window; fail-first was verified for each.
The FlexMeasures integration (the flush/commit path, the scheduler output sign, `make_schedule`) is
validated by the owner's live run, since the running service must not be disturbed to build this.
