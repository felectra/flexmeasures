# Bench assets: modelling the Mykolaiv stand in FlexMeasures

What to create after [step 5 of the pilot recipe](README.md#5-one-time-data-initialisation),
and — more importantly — which of it has data behind it today and which does not.

The physical installation is described in a separate repository, `../deye-imex`. Read
`docs/installation.md` there before creating anything; this file only carries what bears on
the asset model.

## The asset tree

```
Office (Mykolaiv lab)          site, grid connection point
└── Bank                       the battery as the inverter sees it
    ├── String A               14S3P, own JK BMS
    └── String B               14S3P, own JK BMS
Generator                      7 kW via ATS
```

Why this shape rather than one flat battery asset:

**Office** is where grid exchange is measured, and it is the only point where the inverter's
numbers describe the whole site.

**Bank** is what the inverter reports. It cannot see the two strings separately — it measures
one pair of terminals and nothing else.

**String A / String B** exist as separate assets because they are separate batteries in every
way that matters: independent BMS, different cycling history (about 34 % more charge through
one than the other), and coulomb counters that disagree with each other by twenty-one
percentage points at identical terminal voltage. Modelling them as one asset would bake in an
assumption the hardware contradicts.

**Generator** is a sibling of Office, not a child of Bank.

## What actually has data today

This is the part worth reading twice.

| Asset | Source | State |
|---|---|---|
| Office | `deye/ac/*` over MQTT | **live** |
| Bank | `deye/battery/*` over MQTT | **live**, with caveats below |
| String A / B | none yet | **placeholder** |
| Generator | none at all | **placeholder** |

Per-string telemetry requires an ESP32 reading both BMS over Bluetooth. The config exists
(`../deye-imex/bench/jk-bms-ble.yaml`) but is **not deployed**, and it has an open
prerequisite: the broker does not listen on the site LAN, which is where the ESP32 would be.

The generator is not in service and there is **no "running" signal anywhere** — MQTT included.
An EMS cannot see it start, so nothing can be scheduled around it.

Create all four, but do not let the empty two look like they are merely quiet.

## Reaching the MQTT broker

The bridge and the broker both run on the ROCK 3A. From a workstation:

```bash
ssh -t -i ~/.ssh/id_ed25519_gitlab sd@100.75.41.122
mosquitto_sub -h 127.0.0.1 -t 'deye/#' -v -W 85 | sort
```

`100.75.41.122` is the board's Tailscale address. `-W 85` covers one full poll cycle plus
margin — the bridge polls every 60 s.

`mosquitto` listens on `127.0.0.1:1883` for the bridge and on `100.75.41.122:1883` for the
tailnet. **It does not listen on the site LAN**, deliberately.

**From inside a container this matters.** `127.0.0.1` in the FlexMeasures container is the
container, not the board. Use the podman host gateway or the tailnet address, and remember
the broker currently has **no authentication** — anything that can reach it can publish to it.

## Facts about the data that will bite you

All of these are established in `../deye-imex/docs/installation.md`.

**Payloads are bare numbers with no timestamp.** The bridge publishes a value per topic and
nothing else. Whoever ingests must stamp arrival time. Measured skew between reading and
arrival is under one second against a 60 s cycle, so this is acceptable — but it is an
assumption, not a guarantee, and there is no way to detect a delayed message after the fact.

**Sign convention:** positive `ac/total_grid_power` is **import**, negative is export.
Verified by observation, not assumed from the manual.

**`battery/soc` is not usable.** The inverter estimates it itself and gets it badly wrong: on
2026-08-26 it reported 100 % at 3.50 V per cell, roughly a third of charge. It is not merely
imprecise, it appears pinned near full.

**The `deye/bms/*` group reads zeros**, because the BMS are not connected to the inverter.
This is worse than a missing value: a consumer seeing `bms/1/soc = 0.0` concludes the pack is
empty when it is not. Treat the whole group as absent until the CAN link exists.

**Do not poll the inverter's logger directly.** It accepts exactly one TCP connection, the
bridge holds it, and a second connection breaks collection for everyone. Everything goes
through MQTT.

## Read-only, and not by accident

The bridge runs in read mode; command topics do not exist on the broker. FlexMeasures may
compute schedules against this data, but **nothing here executes them**, and the path to
executing them has its own preconditions — staged rollout, allowlisted registers, command TTL,
single-writer lease, read-back — set out in `../deye-imex/docs/scenarios.md`.

Two specific traps for a scheduler built on this bank:

The manufacturer of these cells documented a fire risk aggravated by *routinely charging to
full after substantial depletion* (GM Part 573 recall report 21V-650). The inverter is
currently configured so that neither extreme is reached. A schedule that widens the voltage
window would walk into exactly that pattern.

And this is an uncooled NMC pack of unknown state of health, with never-measured capacity.
Every capacity figure available — roughly 18 to 19.7 kWh for the bank — is a nameplate or a
calculation, not a measurement. Treat capacity as a parameter to be calibrated, not a constant.
