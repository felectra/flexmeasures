# labems-t2t — belief retention (documented policy + ready tool, NOT scheduled)

The continuous ingester (labems-t2t) writes raw telemetry to FlexMeasures without bound.
This file documents the growth, proposes a retention policy, and provides a **ready, parameterized
prune tool** — it does **not** run, schedule, or delete anything.
Scheduling a prune (a systemd timer or a cron) is an **owner / YellowHeron decision and a separate
bead**; this file only records the policy and the mechanism.

## Measured growth

| Quantity | Value |
|---|---|
| Ingest rate | ~6631 beliefs/hour ≈ **159 k/day** |
| Row size | ~272 B/row including indexes |
| Disk rate | **~42 MB/day** |
| Free space | 3.5 GB on a 14.5 GB eMMC |
| Runway | **~2.7 months** to fill at the current rate |

Fine for the ≥24 h sc9 run and near-term work, but the eMMC fills in a few months, so a retention
policy is needed before then.

## Proposed policy (default)

- **Keep raw `mqtt-ingest` beliefs for 90 days**, then delete them.
- 90 days of raw telemetry is ~14 M rows ≈ ~3.8 GB — larger than the current free space, so the real
  cutoff the owner chooses will likely be shorter (e.g. 30 days ≈ ~1.3 GB), or paired with
  downsampling; the number below is a **parameter to set**, not a fixed recommendation.
- **Alternative to plain deletion: downsample** older raw beliefs to a coarser resolution (e.g. one
  value per minute or per 15 minutes per sensor) before deleting the fine-grained rows, keeping a long
  history at a fraction of the size. Downsampling is more work and is left as an option for the bead
  that implements retention.

## Scope and safety — READ BEFORE RUNNING ANYTHING

- The prune below is scoped to the **`mqtt-ingest` source** (raw telemetry) **and to account 1** (the
  pilot's own sensors), via an `IN` subselect on `data_source` plus a join to `generic_asset`. The
  source `name,type` is not unique, so the `IN` (not `=`) tolerates zero, one, or several matching
  rows, and the account join means another account's data on a same-named source is never touched.
- It does **not** touch the **StorageScheduler** schedule beliefs (labems-sc9 output — a different
  source), nor any scheduler/forecast data.
- It **does** include the earliest `mqtt-ingest` rows, which are the labems-3oh acceptance samples;
  deleting raw telemetry older than the cutoff is the intended behaviour, but be aware those samples
  share the `mqtt-ingest` source. If they must be preserved, exclude their date range explicitly.
- **This deletes data irreversibly.** Always run the dry-run count first, take a backup if in doubt
  (`flexmeasures db-ops dump`), and never run it during the sc9 stability window without owner sign-off.
- Never run it against any source but `mqtt-ingest`.

## Dry-run first — count what a cutoff would delete (read-only)

Set the cutoff once and reuse it. This SELECT changes nothing:

Choose ONE cutoff instant and reuse the SAME literal in the count and the delete, so nothing crosses
the boundary between the two commands:

```bash
CUTOFF="$(date -u -d '90 days ago' +%Y-%m-%dT%H:%M:%SZ)"   # the retention window; adjust as chosen

podman exec -i rock3a_db_1 psql -U fm_pilot -d fm_pilot -c "
  SELECT count(*) AS rows_to_delete, min(tb.event_start) AS oldest, max(tb.event_start) AS newest_deleted
  FROM timed_belief tb
  JOIN sensor s ON s.id = tb.sensor_id
  JOIN generic_asset ga ON ga.id = s.generic_asset_id
  WHERE ga.account_id = 1
    AND tb.source_id IN (SELECT id FROM data_source WHERE name = 'mqtt-ingest' AND type = 'mqtt')
    AND tb.event_start < TIMESTAMPTZ '$CUTOFF';"
```

Re-run the count (changing `$CUTOFF`) until it matches the intent.

## The prune (parameterized; DELETES DATA — do NOT run here)

Only after the dry-run count matches the intent, and only with owner authorization:

```bash
# DANGER: deletes rows irreversibly. Account-1 mqtt-ingest telemetry only. Reuse the SAME $CUTOFF.
# Not during the sc9 window without sign-off.
podman exec -i rock3a_db_1 psql -U fm_pilot -d fm_pilot -c "
  DELETE FROM timed_belief tb
  USING sensor s, generic_asset ga
  WHERE s.id = tb.sensor_id AND ga.id = s.generic_asset_id
    AND ga.account_id = 1
    AND tb.source_id IN (SELECT id FROM data_source WHERE name = 'mqtt-ingest' AND type = 'mqtt')
    AND tb.event_start < TIMESTAMPTZ '$CUTOFF';"
```

The `IN (...)` matches only the `mqtt-ingest` source rows, and handles zero rows (a safe no-op),
one row, or several without error, while the `account_id = 1` join further bounds the delete to the
pilot's own sensors, so another account's data on a same-named source is never touched.

Reclaiming eMMC space needs a rewrite, not a plain `VACUUM`: ordinary `VACUUM` only makes the freed
space reusable inside PostgreSQL and does **not** return it to the filesystem (`df` will not change);
`VACUUM FULL timed_belief` does return space, but takes an exclusive table lock and needs free space
for the rewrite, so weigh it against the disk situation and the sc9 window before running it.

## Not scheduled

There is deliberately no systemd timer or cron here.
Turning this into an automatic job — interval, cutoff, downsample-vs-delete, whether to `VACUUM`, and
how it interacts with the sc9 window — is an owner / YellowHeron decision and a separate bead.
This file provides the policy and the vetted SQL; it runs nothing.
