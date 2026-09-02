# labems-sc9 — shadow storage scheduler (rubіж G2)

Compute-only shadow scheduling for the ROCK 3A pilot.
FlexMeasures' `StorageScheduler` computes a battery schedule for `BatteryBank` on the live
labems-3oh sensors, the run is journaled reproducibly, and **nothing is executed**: no broker
publish, no command to the bridge/inverter/BMS, no hardware change.
This is the G2 rung of `lab-ems-program.md` §6 ("EMS обчислює рішення, але не виконує їх").

## Files

| File | Role |
|---|---|
| `shadow_schedule.py` | The wrapper: snapshots inputs, pins `belief_time`, calls the scheduler, journals the run. Compute-only unless `--commit`. |
| `shadow-g2-config.json` | Versioned objective + constraints (capacity, SOC, peak nudge, resolution, horizon). |
| `seed-shadow-sensor.py` | Idempotent seed for the PT15M output sensor on `BatteryBank`. **Not run yet** (gated on YellowHeron). |

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
- **Prices** — flat `50 EUR/MWh` consumption = production is an internal nudge, not a tariff; the peak
  baseline (`0.3 kW`) and price (`120 EUR/MW`) are an internal peak-shaving nudge, not a contracted
  limit.
- **Grid signal caveat** — `deye/ac/total_grid_power` already nets the battery (behind-the-meter), so
  wiring it as the inflexible load makes this a shadow illustration, not a true dispatch.

## How to run

Copy the wrapper and config into the server container, then run (read-only broker; writes the FM DB
only with `--commit`):

```bash
podman cp deploy/rock3a/shadow_schedule.py rock3a_server_1:/tmp/shadow_schedule.py
podman cp deploy/rock3a/shadow-g2-config.json rock3a_server_1:/tmp/shadow-g2-config.json
podman exec -i rock3a_server_1 python /tmp/shadow_schedule.py \
    --config /tmp/shadow-g2-config.json \
    --start 2026-09-01T19:00:00+00:00 --belief-time 2026-09-02T08:00:00+00:00 \
    --prove-reproducible
```

- `--commit` persists the schedule to the output sensor (writes the FM DB); it is **off by default**
  and refuses to run until `target_output_sensor_id` is set in the config (i.e. after the output
  sensor is created).
- `--prove-reproducible` computes twice on the pinned inputs and asserts the two schedules are
  identical.
- Schedule values are in the target sensor's unit; the reproducibility log records them verbatim.

## Reproducibility method

`belief_time` is pinned explicitly on every run.
FlexMeasures reads only beliefs known as of `belief_time`, so a fixed `(window, belief_time)` reads
exactly the same inputs, and the deterministic HiGHS solver then returns an identical schedule.
The log stores the config's `sha256`, the window, the `belief_time`, the input values with their
belief times, the code version (FlexMeasures + `StorageScheduler` version), and an explicit
`broker_publishes=0 hardware_commands=0` line — one JSON record per run.

## Open prerequisite — continuous ingestion

A meaningful **≥24 h** shadow cycle needs the inflexible sensor to be **continuously** populated.
The labems-3oh bridge (`mqtt_ingest.py`) is a **one-shot** capture, so the DB currently holds only a
few grid-power beliefs; over any real horizon the load is "assumed zero" and there is no genuine
peak to shave.
Standing up a continuous ingestion path is a separate step and a **host-service change**, which is
deferred while the Plan D observation window is open (until `2026-09-03T07:29:47Z`).

## Validation performed (read-only, dry-run)

On the running image (`0.1.dev1983+g9aa86e789`, FlexMeasures v1.0.0):

- The scheduler loads and the config deserializes.
- A 24 h PT15M schedule (96 steps) computes cleanly end-to-end.
- **Reproducible**: two computes on the same pinned inputs return an identical schedule.
- **Responds to the objective**: `site-peak-consumption` `0.3 kW` → all 96 steps charge into the free
  headroom; `0 kW` → the battery idles. The objective is wired correctly.
- Zero writes (`committed=False`), zero broker publishes, zero commands.
