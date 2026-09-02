# labems-sc9 — shadow storage scheduler (rubіж G2)

Compute-only shadow scheduling for the ROCK 3A pilot.
FlexMeasures' `StorageScheduler` computes a battery schedule for `BatteryBank` on the live
labems-3oh sensors, the run is journaled reproducibly, and **nothing is executed**: no broker
publish, no command to the bridge/inverter/BMS, no hardware change.
This is the G2 rung of `lab-ems-program.md` §6 ("EMS обчислює рішення, але не виконує їх").

## Files

| File | Role |
|---|---|
| `shadow_schedule.py` | The wrapper: enforces the input allowlist, snapshots inputs, records `belief_time`, computes the schedule, journals the run. Compute-only unless `--commit`. |
| `shadow-g2-config.json` | Versioned objective + constraints (capacity, SOC, efficiency, peak nudge, resolution, horizon, coverage gate). |
| `seed-shadow-sensor.py` | Idempotent seed for the PT15M output sensor on `BatteryBank`. **Not run yet** (gated on YellowHeron; needs `--i-have-approval`). |

## Objective

Shadow peak-shaving of grid import (`deye/ac/total_grid_power`, Office), expressed in this
FlexMeasures version as a priced `site-peak-consumption` commitment plus the Office grid sensor as
the site inflexible load.
The priority is **reproducibility, not optimality** (per the directive).

## Declared parameters — NOT measurements

Everything below is an explicit assumption, versioned in `shadow-g2-config.json` and echoed into
every log record:

- **Bank capacity `18.0 kWh`** (nameplate/calc; `19.7 kWh` kept as a sensitivity value; SOH unknown).
- **SOC-at-start `50 %`** — there is no trustworthy live SOC: `deye/battery/soc` is forbidden (reads
  full at ~1/3 charge) and the two strings diverge (53 % vs 24 %).
- **SOC band `10–100 %`** — a modelling choice; the hard voltage window `44–57 V` is never widened
  (GM 21V-650 fire-recall pattern).
- **Efficiency `95 % / 95 %`** charging/discharging — a declared round-trip efficiency, not a
  measurement.
- **Prices** — flat `50 EUR/MWh` consumption = production is an internal nudge, not a tariff; the peak
  baseline (`0.3 kW`) and price (`120 EUR/MW`) are an internal peak-shaving nudge, not a contracted
  limit.
- **Grid signal caveat** — `deye/ac/total_grid_power` already nets the battery (behind-the-meter), so
  wiring it as the inflexible load makes this a shadow illustration, not a true dispatch.

## Safety design

- **Compute-only by default.** No MQTT client is ever instantiated; template-asset provisioning is
  disabled at startup, and the non-commit path rolls back the session, so it writes nothing.
- **The input allowlist is enforced.** Any inflexible sensor bound to `deye/battery/soc` or
  `deye/bms/*` is rejected, and the inflexible set must equal the configured expected ids (grid
  sensor 7).
- **`--commit` is strictly gated.** It refuses unless the target is exactly
  `target_output_sensor_id`, on `BatteryBank` (asset 6), at `PT15M`, carrying the
  `labems=sc9-shadow-schedule` marker, with no `source_topic`, and only when input coverage clears
  the threshold. `--commit` cannot be combined with `--target-sensor`, so it can never write a
  measured sensor (e.g. sensor 17).
- **Honest outcome.** The log records `commit_attempted` vs `commit_succeeded`; `committed` is true
  only if the write actually succeeded.

## How to run

Copy the wrapper and config into the server container, then run (read-only broker; writes the FM DB
only with `--commit`):

```bash
podman cp deploy/rock3a/shadow_schedule.py rock3a_server_1:/tmp/shadow_schedule.py
podman cp deploy/rock3a/shadow-g2-config.json rock3a_server_1:/tmp/shadow-g2-config.json
podman exec -i rock3a_server_1 python /tmp/shadow_schedule.py \
    --config /tmp/shadow-g2-config.json \
    --start 2026-09-01T19:00:00+00:00 --belief-time 2026-09-02T08:00:00+00:00 \
    --prove-reproducible --code-sha <repo SHA>
```

- `--start` and `--belief-time` must be timezone-aware; a naive value is rejected.
- `--prove-reproducible` computes twice on the pinned inputs and **exits non-zero** unless the two
  schedules are identical and a real proof (non-empty, not all-NaN, exactly 96 PT15M steps for 24 h).
- `--commit` is off by default and additionally refused until `target_output_sensor_id` is set and
  the target passes every gate above.
- Schedule values are in the target sensor's unit; the log records them verbatim.

## Reproducibility method

`belief_time` is **recorded** on every run (default: now; pin it with `--belief-time`).
FlexMeasures reads only beliefs known as of `belief_time`, so re-running with the **same** recorded
`belief_time` reads exactly the same inputs, and the deterministic HiGHS solver then returns an
identical schedule.
The log stores the config `sha256`, the window, the `belief_time`, the raw and resampled inputs (with
each input's event time, belief time, and source id), the solver actually used, the code version and
`--code-sha`, and an explicit `broker_publishes=0 hardware_commands=0` line — one JSON record per run.

## Completeness gate — the honest guard for one-shot ingestion

`input_coverage` is the fraction of PT15M slots that carry a real belief; the scheduler zero-fills
the rest, so a low coverage means the schedule is dominated by invented zeros.
When coverage is below `min_input_coverage` (0.9) the run is marked `insufficient_input` and
`--commit` is refused — a schedule built mostly on zeros is never silently presented as real.

A meaningful **≥24 h** shadow cycle therefore needs the inflexible sensor to be **continuously**
populated.
The labems-3oh bridge (`mqtt_ingest.py`) is a **one-shot** capture, so the DB currently holds only a
few grid-power beliefs and coverage is far below the gate.
Standing up a continuous ingestion path is a separate step and a **host-service change**, deferred
while the Plan D observation window is open (until `2026-09-03T07:29:47Z`).

## Open items for YellowHeron / owner

- **Output-sensor boundary** — confirm a scheduling output sensor on the existing `BatteryBank` is
  within "no new assets" (asked in the acceptance thread).
- **Continuous ingestion** — a real ≥24 h run needs it; it is a host-service change, currently
  blocked by the Plan D freeze.
- **`persist_flex_model` side effect** — on a non-dry-run, `make_schedule` calls
  `persist_flex_model`, which updates `BatteryBank`'s stored `soc_datetime` / `soc_in_mwh`
  (storage.py). That means a committed shadow run writes the asset's SOC state, not only the schedule
  beliefs. Confirm whether writing asset SOC state is acceptable for a shadow run, or whether the
  commit path should be adjusted to avoid it.

## Validation performed (read-only, dry-run)

On the running image (`0.1.dev1983+g9aa86e789`, FlexMeasures v1.0.0):

- The scheduler loads and the config deserializes.
- A 24 h PT15M schedule (96 steps) computes cleanly end-to-end.
- **Reproducible**: two computes on the same pinned inputs return an identical schedule (the
  enforcing check passes).
- **Responds to the objective**: `site-peak-consumption` `0.3 kW` → the 96 steps charge into the free
  headroom; `0 kW` → the battery idles. The objective is wired correctly.
- **No incidental writes**: `generic_asset` / `sensor` / `timed_belief` row counts are unchanged
  across a compute-only run.
- Zero writes (`committed=False`), zero broker publishes, zero commands.
